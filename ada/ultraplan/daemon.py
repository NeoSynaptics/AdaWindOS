"""
UltraPlan daemon — overnight batch planning loop.

Runs as a background process (or systemd unit) during overnight hours.
Pulls tasks from the queue, runs multi-pass planning, persists results.

Lifecycle:
  1. Check time window (23:00 - 07:00 by default)
  2. Verify GPUs are free (no Whisper/Qwen 14B running)
  3. Load planning model (Qwen 72B across both GPUs)
  4. Loop: pull task → plan → persist → next task
  5. At end of window: unload model, yield GPUs back

Based on Anthropic's ULTRAPLAN concept but fully local:
  - No cloud, no API cost
  - Multi-pass with self-critique (vs single planning pass)
  - 8-hour budget (vs 30 minutes)
  - Dual GPU utilization (vs single cloud instance)

TODO: Implement the daemon loop and GPU management.
"""

import asyncio
import logging
from datetime import datetime

import httpx

from .config import UltraPlanConfig
from .queue import PlanQueue, PlanStatus
from .planner import UltraPlanner

log = logging.getLogger(__name__)


class UltraPlanDaemon:
    """Overnight planning daemon.

    TODO: Implement all methods.
    """

    def __init__(self, store, config: UltraPlanConfig | None = None):
        self.config = config or UltraPlanConfig()
        self.queue = PlanQueue(store)
        self.planner = UltraPlanner(self.config, self.queue)
        self._running = False
        self._current_task_id: str | None = None

    async def run(self) -> None:
        """Main daemon loop."""
        self._running = True
        log.info("UltraPlan daemon started")

        while self._running:
            # Wait until overnight window
            if not self._in_overnight_window():
                log.debug("Outside overnight window, sleeping 5 min")
                await asyncio.sleep(300)
                continue

            # Load planning model
            model_ready = await self._ensure_model_loaded()
            if not model_ready:
                log.warning("Planning model not available, retrying in 5 min")
                await asyncio.sleep(300)
                continue

            # Process queue
            completed = 0
            while self._in_overnight_window() and completed < self.config.max_tasks_per_night:
                tasks = await self.queue.get_pending(limit=1)
                if not tasks:
                    log.info("Queue empty, sleeping 10 min")
                    await asyncio.sleep(600)
                    continue

                task = tasks[0]
                self._current_task_id = task.task_id
                log.info(f"Planning: {task.title} ({task.domain})")

                try:
                    await self.queue.claim(task.task_id)
                    result = await self.planner.plan(task)
                    await self.queue.complete(task.task_id, result.final_plan)
                    completed += 1
                    log.info(f"Plan complete: {task.task_id} ({result.total_tokens} tokens, {result.total_duration_sec:.0f}s)")
                except Exception as e:
                    log.error(f"Planning failed for {task.task_id}: {e}")
                    await self.queue.fail(task.task_id, str(e))

                self._current_task_id = None
                await asyncio.sleep(self.config.cooldown_between_tasks_sec)

            # End of window or max tasks reached
            await self._release_gpus()
            if completed > 0:
                await self._notify_ada(completed)
                log.info(f"Overnight session done: {completed} plans completed")

            # Sleep until next window check
            await asyncio.sleep(300)

    async def submit(self, title: str, brief: str, domain: str = "general",
                     priority: int = 0) -> str:
        """Submit a planning task to the overnight queue.

        Called by Ada Executive when:
        - User says "plan this overnight"
        - Ada detects a complex task better suited for deep planning
        - User queues batch work before bed

        Returns the plan task_id.
        """
        task = await self.queue.submit(title, brief, domain, priority)
        return task.task_id

    async def get_morning_briefing(self) -> list[dict]:
        """Get completed plans for Ada's morning greeting."""
        completed = await self.queue.get_completed_unreviewed()
        return [
            {
                "task_id": t.task_id,
                "title": t.title,
                "domain": t.domain,
                "summary": (t.final_plan or "")[:500],
            }
            for t in completed
        ]

    def _in_overnight_window(self) -> bool:
        """Check if current time is within the planning window."""
        hour = datetime.now().hour
        if self.config.overnight_start_hour > self.config.overnight_end_hour:
            # Crosses midnight (e.g., 23:00 - 07:00)
            return hour >= self.config.overnight_start_hour or hour < self.config.overnight_end_hour
        else:
            return self.config.overnight_start_hour <= hour < self.config.overnight_end_hour

    async def _ensure_model_loaded(self) -> bool:
        """Pre-load planning model into Ollama."""
        model = self.config.planning_model
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                # Generate a short response to force model load
                resp = await client.post(
                    f"{self.config.ollama_base_url}/api/generate",
                    json={"model": model, "prompt": "Hello", "stream": False},
                )
                if resp.status_code == 200:
                    log.info(f"Planning model loaded: {model}")
                    return True

                # Fallback to smaller model
                log.warning(f"{model} unavailable, trying fallback: {self.config.fallback_model}")
                resp = await client.post(
                    f"{self.config.ollama_base_url}/api/generate",
                    json={"model": self.config.fallback_model, "prompt": "Hello", "stream": False},
                )
                if resp.status_code == 200:
                    self.planner.model = self.config.fallback_model
                    log.info(f"Fallback model loaded: {self.config.fallback_model}")
                    return True

        except Exception as e:
            log.error(f"Model loading failed: {e}")
        return False

    async def _release_gpus(self) -> None:
        """Unload planning model so voice pipeline can reclaim GPUs."""
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(
                    f"{self.config.ollama_base_url}/api/generate",
                    json={"model": self.config.planning_model, "prompt": "", "keep_alive": 0},
                )
                log.info("Planning model unloaded, GPUs released")
        except Exception as e:
            log.warning(f"GPU release failed (non-critical): {e}")

    async def _notify_ada(self, completed_count: int) -> None:
        """Record overnight results for Ada's morning briefing."""
        # Write to journal — Ada checks journal on morning greeting
        from ..journal.logger import Journal
        # Use a lightweight approach: write to PostgreSQL directly
        await self.queue.store.pool.execute(
            """INSERT INTO journal (timestamp, action_type, action_summary, band, details)
               VALUES (NOW(), $1, $2, $3, $4)""",
            "ultraplan_complete",
            f"Overnight planning done: {completed_count} plans completed",
            1,
            f'{{"completed": {completed_count}}}',
        )


async def main():
    """Entry point for standalone daemon execution.

    Can be run as:
      python -m ada.ultraplan.daemon
    Or managed by systemd alongside adaos.service.
    """
    from ..config import AdaConfig
    from ..memory.store import Store

    config = AdaConfig()
    store = Store(config.database)
    await store.connect()
    try:
        daemon = UltraPlanDaemon(store)
        await daemon.run()
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())


# TODO: Systemd unit file: ultraplan.service (runs alongside adaos.service)
# TODO: CLI: `ada plan queue "title" --domain sportwave --priority 2`
# TODO: CLI: `ada plan list` — show queue status
# TODO: CLI: `ada plan review` — interactive morning review
# TODO: Signal handling — graceful shutdown mid-plan (persist progress)
# TODO: Heartbeat logging — daemon alive check for Ada health monitor
