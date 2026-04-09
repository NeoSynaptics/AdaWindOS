"""Outbox worker — restart-safe side effect delivery.

Reads pending outbox events from PostgreSQL, executes them, marks processed.
If Ada crashes between writing a task and dispatching, the outbox worker
picks up unprocessed events on restart. Nothing lost, nothing doubled.
"""

import asyncio
import logging
from typing import Any, Callable, Awaitable

from ..memory.store import Store
from ..journal.logger import Journal

log = logging.getLogger(__name__)

# Type for delivery functions: takes payload dict, returns None
Deliverer = Callable[[dict[str, Any]], Awaitable[None]]


class OutboxWorker:
    def __init__(self, store: Store, journal: Journal):
        self.store = store
        self.journal = journal
        self.deliverers: dict[str, Deliverer] = {}
        self._running = False

    def register(self, event_type: str, deliverer: Deliverer) -> None:
        """Register a delivery function for an event type."""
        self.deliverers[event_type] = deliverer

    async def start(self) -> None:
        """Start the outbox processing loop."""
        self._running = True
        log.info("Outbox worker started")
        while self._running:
            try:
                await self._process_batch()
            except Exception as e:
                log.error(f"Outbox worker error: {e}")
            await asyncio.sleep(0.25)

    async def stop(self) -> None:
        self._running = False

    async def _process_batch(self) -> None:
        events = await self.store.fetch_pending_outbox(batch=50)
        for event in events:
            event_type = event["event_type"]
            deliverer = self.deliverers.get(event_type)

            if not deliverer:
                log.warning(f"No deliverer for outbox event type: {event_type}")
                await self.store.bump_outbox_attempt(
                    event["id"], f"No deliverer for {event_type}"
                )
                continue

            try:
                await asyncio.wait_for(deliverer(event["payload"]), timeout=300)
                await self.store.mark_outbox_processed(event["id"])
                await self.journal.log(
                    action_type="outbox_delivered",
                    summary=f"Delivered {event_type}",
                    band=2,
                    details={"outbox_id": event["id"], "event_type": event_type},
                )
            except asyncio.TimeoutError:
                log.error(f"Outbox delivery timed out (300s) for event {event['id']} type={event_type}")
                await self.store.bump_outbox_attempt(event["id"], "Delivery timed out (300s)")
            except Exception as e:
                log.error(f"Outbox delivery failed for {event['id']}: {e}", exc_info=True)
                # Check if this is a poison event (max attempts reached)
                attempt = event.get("attempt", 0)
                max_attempts = event.get("max_attempts", 5)
                if attempt + 1 >= max_attempts:
                    log.warning(f"Poison event detected: {event['id']} failed {max_attempts} times, marking as failed")
                    await self.journal.log(
                        action_type="outbox_poison",
                        summary=f"Outbox event {event['id']} permanently failed after {max_attempts} attempts",
                        band=2,
                        details={"outbox_id": event["id"], "event_type": event_type,
                                 "last_error": str(e), "attempts": max_attempts},
                    )
                await self.store.bump_outbox_attempt(event["id"], str(e))
