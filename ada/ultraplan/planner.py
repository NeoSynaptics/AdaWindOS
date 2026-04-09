"""
UltraPlan multi-pass planner.

Orchestrates the three planning passes against Ollama:
  Pass 1: Decompose (with optional Aristotle preamble)
  Pass 2: Critique the decomposition
  Pass 3: Synthesize final plan from decomposition + critique

Each pass is a full LLM generation with the planning model (default: Qwen 72B).
Intermediate outputs are persisted to the queue for inspection.
"""

import logging
import time
from dataclasses import dataclass

import httpx

from .config import UltraPlanConfig
from .queue import PlanTask, PlanQueue
from .templates import (
    PASS_1_DECOMPOSE,
    PASS_2_CRITIQUE,
    PASS_3_SYNTHESIZE,
    DOMAIN_CONTEXT,
    ARISTOTLE_PREAMBLE,
)

log = logging.getLogger(__name__)


@dataclass
class PlanResult:
    task_id: str
    pass_outputs: dict[int, str]  # {1: decomposition, 2: critique, 3: synthesis}
    final_plan: str
    total_tokens: int
    total_duration_sec: float
    model_used: str


class UltraPlanner:
    """Multi-pass planning engine."""

    def __init__(self, config: UltraPlanConfig, queue: PlanQueue):
        self.config = config
        self.queue = queue
        self.model = config.planning_model

    async def plan(self, task: PlanTask, use_aristotle: bool = False) -> PlanResult:
        """Execute full multi-pass planning for a task.

        Args:
            task: The planning task from the queue.
            use_aristotle: Prepend first-principles preamble to Pass 1.

        Returns:
            PlanResult with all intermediate and final outputs.
        """
        start = time.time()
        total_tokens = 0
        outputs: dict[int, str] = {}

        domain_ctx = self._build_domain_context(task.domain)

        if not use_aristotle:
            use_aristotle = self._should_use_aristotle(task)

        # Pass 1: Decompose
        p1_prompt = PASS_1_DECOMPOSE.format(brief=task.brief, domain_context=domain_ctx)
        if use_aristotle:
            p1_prompt = ARISTOTLE_PREAMBLE + "\n\n" + p1_prompt

        log.info(f"[{task.task_id}] Pass 1: Decompose (aristotle={use_aristotle})")
        p1_text, p1_tokens = await self._generate(p1_prompt)
        outputs[1] = p1_text
        total_tokens += p1_tokens
        await self.queue.update_progress(task.task_id, 1, p1_text)

        # Pass 2: Critique
        p2_prompt = PASS_2_CRITIQUE.format(brief=task.brief, previous_output=p1_text)
        log.info(f"[{task.task_id}] Pass 2: Critique")
        p2_text, p2_tokens = await self._generate(p2_prompt)
        outputs[2] = p2_text
        total_tokens += p2_tokens
        await self.queue.update_progress(task.task_id, 2, p2_text)

        # Pass 3: Synthesize
        p3_prompt = PASS_3_SYNTHESIZE.format(
            brief=task.brief, pass_1_output=p1_text, pass_2_output=p2_text,
        )
        log.info(f"[{task.task_id}] Pass 3: Synthesize")
        p3_text, p3_tokens = await self._generate(p3_prompt)
        outputs[3] = p3_text
        total_tokens += p3_tokens
        await self.queue.update_progress(task.task_id, 3, p3_text)

        # Optional extra refinement passes (critique → re-synthesize cycles)
        latest_synthesis = p3_text
        for i in range(self.config.extra_refinement_passes):
            pass_num = 4 + (i * 2)

            log.info(f"[{task.task_id}] Refinement pass {i + 1}: extra critique")
            ec_prompt = PASS_2_CRITIQUE.format(brief=task.brief, previous_output=latest_synthesis)
            ec_text, ec_tokens = await self._generate(ec_prompt)
            outputs[pass_num] = ec_text
            total_tokens += ec_tokens
            await self.queue.update_progress(task.task_id, pass_num, ec_text)

            log.info(f"[{task.task_id}] Refinement pass {i + 1}: re-synthesis")
            es_prompt = PASS_3_SYNTHESIZE.format(
                brief=task.brief, pass_1_output=latest_synthesis, pass_2_output=ec_text,
            )
            latest_synthesis, es_tokens = await self._generate(es_prompt)
            outputs[pass_num + 1] = latest_synthesis
            total_tokens += es_tokens
            await self.queue.update_progress(task.task_id, pass_num + 1, latest_synthesis)

        duration = time.time() - start
        log.info(
            f"[{task.task_id}] Planning complete: {len(outputs)} passes, "
            f"{total_tokens} tokens, {duration:.0f}s"
        )

        return PlanResult(
            task_id=task.task_id,
            pass_outputs=outputs,
            final_plan=latest_synthesis,
            total_tokens=total_tokens,
            total_duration_sec=duration,
            model_used=self.model,
        )

    async def _generate(self, prompt: str, system: str = "") -> tuple[str, int]:
        """Call Ollama /api/generate with the planning model.

        Returns (response_text, token_count).
        Uses stream=False for simplicity; the daemon provides progress via
        queue.update_progress() between passes.
        """
        timeout = self.config.max_minutes_per_task * 60
        payload: dict = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.6, "num_predict": 8192},
        }
        if system:
            payload["system"] = system

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{self.config.ollama_host}/api/generate",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        text = data.get("response", "")
        tokens = data.get("eval_count", 0) + data.get("prompt_eval_count", 0)
        return text, tokens

    def _build_domain_context(self, domain: str) -> str:
        """Get domain-specific context for the planning prompts."""
        return DOMAIN_CONTEXT.get(domain, DOMAIN_CONTEXT["general"])

    def _should_use_aristotle(self, task: PlanTask) -> bool:
        """Heuristic: when to auto-enable first-principles mode.

        Triggers on tasks that benefit from stripping assumptions:
        new domains, redesigns, or explicit first-principles language.
        """
        triggers = (
            "from scratch", "rethink", "fundamental", "redesign",
            "first principles", "ground up", "why do we", "new approach",
            "rearchitect", "rebuild",
        )
        brief_lower = task.brief.lower()
        if any(t in brief_lower for t in triggers):
            return True
        # Unfamiliar domain — no specific context available
        if task.domain not in DOMAIN_CONTEXT or task.domain == "general":
            return True
        return False
