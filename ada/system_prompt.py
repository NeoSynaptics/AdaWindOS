"""Ada's prompt system — layered architecture.

Layer 1: SOUL (ada/SOUL.md) — personality, tone, identity. Never changes.
Layer 2: CONTEXT — tasks, memories, time, deliveries. Changes every turn.
Layer 3: CONVERSATION — real message turns from episodes. Changes every turn.
Layer 4: ACTION HINT — what Ada just did. Changes per response.

Future layers (not yet implemented):
- USER MODEL — learned preferences, distilled from memory consolidation
- INSTRUCTION FILES — .ada/instructions.md per-project overrides

The LLM only does TWO things:
  1. Classify intent (structured JSON) — CLASSIFICATION_PROMPT
  2. Generate spoken response (personality + context) — build_response_prompt()

Everything else (action resolution, dispatch, validation, retry) is code.
"""

from pathlib import Path

# --- SOUL (loaded once from SOUL.md) ---

_soul_cache: str | None = None


def _load_soul() -> str:
    """Load SOUL.md from ada/ directory. Cached after first read."""
    global _soul_cache
    if _soul_cache is not None:
        return _soul_cache

    soul_path = Path(__file__).parent / "SOUL.md"
    if soul_path.exists():
        _soul_cache = soul_path.read_text().strip()
    else:
        # Fallback if SOUL.md is missing
        _soul_cache = "You are Ada, a voice AI assistant. Be warm, direct, and brief."

    return _soul_cache


# --- RESPONSE PROMPT (system message for response generation) ---

def build_response_prompt(
    current_time: str,
    active_tasks: list[dict] | None = None,
    pending_deliveries: list[dict] | None = None,
    relevant_memories: list[dict] | None = None,
    instruction_files: str = "",
    # Legacy params (ignored, kept for backward compat)
    session_id: str = "",
    voice_state: str = "",
    budget_spent_today: int = 0,
    intent: str = "",
    action: str = "",
    confidence: float = 0.0,
    conversation_history: str = "",
) -> str:
    """Build the system prompt from SOUL + context layers.

    The conversation history is NOT included here — it's added as
    separate message turns in _generate_response() for proper multi-turn.
    """
    parts = [_load_soul()]

    # Context block — only non-empty sections
    context_lines = [f"\n# Right now\n\nTime: {current_time}"]

    if active_tasks:
        lines = [f"- {t.get('task_id','')}: {t.get('title','')} ({t.get('status','')})" for t in active_tasks[:5]]
        context_lines.append("Active tasks:\n" + "\n".join(lines))

    if pending_deliveries:
        context_lines.append(f"Pending deliveries: {len(pending_deliveries)} waiting")

    if relevant_memories:
        lines = [f"- {m.get('content','')}" for m in relevant_memories[:5]]
        context_lines.append("Things you know:\n" + "\n".join(lines))

    if instruction_files:
        context_lines.append(instruction_files)

    parts.append("\n".join(context_lines))

    return "\n\n".join(parts)


# --- CLASSIFICATION PROMPT (used with JSON Schema enforcement) ---

CLASSIFICATION_PROMPT_TEMPLATE = """Classify this message into exactly one intent.

# Intents

brainstorm — exploring an idea, no specific action requested
note — explicitly saving something ("save this", "remember this", "just a thought")
dispatch — requesting work from an agent ("send to Arch", "have Forge build", "ask Arch")
question — asking for information or analysis ("what do you think?", "is this feasible?")
decision — choosing between options Ada presented ("go with X", "yes", "option B")
command — direct instruction ("remind me Friday", "cancel that", "schedule")
followup — adding to the current topic ("also", "and we'd need", "another thing")
greeting — starting a session ("hey Ada", "good morning")
farewell — ending a session ("that's it", "bye", "thanks")
acknowledgement — confirming something ("ok", "got it", "sure")
clarification — responding to Ada's question ("I meant the camera", "no, the other one")
noop — background noise, empty, or not directed at Ada

# Rules

Return exactly ONE intent.
If the message contains both a note and a dispatch ("save this and send to Arch"), pick the higher commitment: dispatch.
If you are not sure, set confidence below 0.6.
target_agent is ARCH for architecture, design, research, analysis. FORGE for code, implementation, building. null if not agent work.
references_existing is true if this continues an ongoing topic from the conversation.

# Context

Active topic: {active_topic}
Recent tasks: {pending_tasks}
Last intent: {last_intent}

# Message

{user_message}"""


def build_classification_prompt(
    user_message: str,
    active_topic: str = "none",
    pending_tasks: str = "none",
    last_intent: str = "none",
) -> str:
    return CLASSIFICATION_PROMPT_TEMPLATE.format(
        user_message=user_message,
        active_topic=active_topic,
        pending_tasks=pending_tasks,
        last_intent=last_intent,
    )


# --- LEGACY (kept for backward compatibility) ---

def build_system_prompt(
    current_time: str,
    session_id: str = "",
    active_tasks: str = "none",
    pending_deliveries: str = "none",
    voice_state: str = "IDLE",
    executive_state: str = "IDLE",
    budget_spent_today: int = 0,
    daily_budget: int = 11_000_000,
    instruction_files: str = "",
) -> str:
    """Legacy system prompt builder — used by context_builder until fully migrated."""
    return build_response_prompt(
        current_time=current_time,
        instruction_files=instruction_files,
    )
