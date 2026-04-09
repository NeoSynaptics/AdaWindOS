"""Intent classification via APU gateway + Ollama structured output (JSON Schema).

All LLM calls go through the APU gateway — models are auto-loaded before inference,
ref-counted during inference, and never evicted mid-call.
"""

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .event import Classification
from ..system_prompt import build_classification_prompt

if TYPE_CHECKING:
    from ..apu.gateway import APUGateway

log = logging.getLogger("ada.decision.classifier")

INTENT_SCHEMA = json.loads(
    (Path(__file__).parent / "intent_schema.json").read_text()
)


async def classify_turn(
    gateway: "APUGateway",
    model: str,
    user_message: str,
    active_topic: str = "none",
    pending_tasks: str = "none",
    last_intent: str = "none",
) -> Classification:
    """Classify intent via APU gateway with JSON Schema enforcement."""
    prompt = build_classification_prompt(
        user_message=user_message,
        active_topic=active_topic,
        pending_tasks=pending_tasks,
        last_intent=last_intent,
    )

    try:
        result = await gateway.chat_json(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            schema=INTENT_SCHEMA,
            temperature=0.0,
            timeout=15.0,
        )
    except Exception as e:
        log.error(f"Classification call failed: {e}")
        raise

    if "intent" not in result or "confidence" not in result:
        log.error(f"Classification missing required fields: {result}")
        raise ValueError(f"Missing intent or confidence in: {result}")

    return Classification(
        intent=result["intent"],
        confidence=result["confidence"],
        target_agent=result.get("target_agent"),
        urgency=result.get("urgency", "low"),
        note_action=result.get("note_action", "none"),
        topic=result.get("topic", ""),
        references_existing=result.get("references_existing", False),
    )


async def classify_with_fallback(
    gateway: "APUGateway",
    model: str,
    user_message: str,
    **kwargs,
) -> Classification:
    """Classification with fallback chain: JSON Schema → retry → keyword matching."""
    # Attempt 1
    try:
        return await classify_turn(gateway, model, user_message, **kwargs)
    except Exception as e:
        log.warning(f"Classification attempt 1 failed: {e}")

    # Attempt 2
    try:
        return await classify_turn(gateway, model, user_message, **kwargs)
    except Exception as e:
        log.warning(f"Classification attempt 2 failed: {e}")

    # Attempt 3: keyword fallback (no LLM)
    log.warning("Falling back to keyword-based classification")
    return _keyword_fallback(user_message)


def _keyword_fallback(text: str) -> Classification:
    """Last resort: keyword matching when LLM classification fails."""
    lower = text.lower().strip()

    if any(w in lower for w in ("save", "remember", "note", "just save")):
        return Classification(intent="note", confidence=0.5, topic=text)

    if any(w in lower for w in ("send to arch", "ask arch", "have arch")):
        return Classification(intent="dispatch", confidence=0.5, target_agent="ARCH", topic=text)

    if any(w in lower for w in ("send to forge", "have forge", "build", "implement", "prototype")):
        return Classification(intent="dispatch", confidence=0.5, target_agent="FORGE", topic=text)

    if any(w in lower for w in ("remind me", "cancel", "schedule", "stop", "compact")):
        return Classification(intent="command", confidence=0.5, topic=text)

    if lower in ("hey ada", "hello", "hi", "good morning"):
        return Classification(intent="greeting", confidence=0.8, topic="")

    if any(w in lower for w in ("bye", "that's it", "thanks", "done")):
        return Classification(intent="farewell", confidence=0.7, topic="")

    if "?" in lower:
        return Classification(intent="question", confidence=0.4, topic=text)

    return Classification(intent="brainstorm", confidence=0.3, topic=text)
