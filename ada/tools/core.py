"""Core tools — Tier 1, always available to Ada.

These are registered at startup and always present in the prompt.
"""

from __future__ import annotations
from typing import TYPE_CHECKING

from .registry import ToolDef, ToolRegistry

if TYPE_CHECKING:
    from .handlers import ToolHandlers


def register_core_tools(registry: ToolRegistry, handlers: ToolHandlers | None = None) -> None:
    """Register all Tier 1 (always available) tools."""

    registry.register(ToolDef(
        name="speak",
        description="Speak text aloud via TTS. Use for responses, acknowledgements, summaries.",
        tier=1, category="core",
        handler=handlers.speak if handlers else None,
    ))

    registry.register(ToolDef(
        name="think",
        description="Internal scratchpad. NOT spoken aloud. Use for reasoning steps.",
        tier=1, category="core",
        handler=handlers.think if handlers else None,
    ))

    registry.register(ToolDef(
        name="save_note",
        description="Save a note to the noteboard. Types: personal, actionable, scheduled, conditional.",
        tier=1, category="core",
        handler=handlers.save_note if handlers else None,
    ))

    registry.register(ToolDef(
        name="update_noteboard",
        description="Edit, pin, archive, or delete a note on the noteboard.",
        tier=1, category="core",
        handler=handlers.update_noteboard if handlers else None,
    ))

    registry.register(ToolDef(
        name="dispatch_agent",
        description="Send structured work to Arch (design/research) or Forge (code/implementation).",
        tier=1, category="core",
        handler=handlers.dispatch_agent if handlers else None,
    ))

    registry.register(ToolDef(
        name="validate",
        description="Run 4-gate validation on worker output. Structural, technical, semantic, attention.",
        tier=1, category="core",
        handler=handlers.validate if handlers else None,
    ))

    registry.register(ToolDef(
        name="search",
        description="Search across documents, repos, and knowledge base (Onyx).",
        tier=1, category="core",
        handler=handlers.search if handlers else None,
    ))

    registry.register(ToolDef(
        name="read_file",
        description="Read contents of a local file.",
        tier=1, category="core",
        handler=handlers.read_file if handlers else None,
    ))

    registry.register(ToolDef(
        name="read_instructions",
        description="Load .ada/instructions.md from project or global path.",
        tier=1, category="core",
        handler=handlers.read_instructions if handlers else None,
    ))

    registry.register(ToolDef(
        name="compact",
        description="Trigger context compaction. Use when conversation gets scattered.",
        tier=1, category="core",
        handler=handlers.compact if handlers else None,
    ))

    registry.register(ToolDef(
        name="memory_query",
        description="Search semantic memories for relevant facts, preferences, decisions.",
        tier=1, category="core",
        handler=handlers.memory_query if handlers else None,
    ))

    registry.register(ToolDef(
        name="memory_store",
        description="Store a fact, preference, or decision in semantic memory.",
        tier=1, category="core",
        handler=handlers.memory_store if handlers else None,
    ))

    registry.register(ToolDef(
        name="run_command",
        description="Execute a shell command in restricted/sandboxed mode.",
        tier=1, category="core",
        handler=handlers.run_command if handlers else None,
    ))


def register_extended_tools(registry: ToolRegistry, handlers: ToolHandlers | None = None) -> None:
    """Register Tier 2 (conditionally loaded) tools."""

    registry.register(ToolDef(
        name="schedule",
        description="Schedule an action for a future date/time or condition.",
        tier=2, category="schedule",
        handler=handlers.schedule if handlers else None,
    ))

    registry.register(ToolDef(
        name="task_create",
        description="Create a task directly (not via note promotion).",
        tier=2, category="task",
        handler=handlers.task_create if handlers else None,
    ))

    registry.register(ToolDef(
        name="task_update",
        description="Update a task's status, brief, priority, or other fields.",
        tier=2, category="task",
        handler=handlers.task_update if handlers else None,
    ))

    registry.register(ToolDef(
        name="edit_file",
        description="Edit an existing file with targeted changes.",
        tier=2, category="file",
        handler=handlers.edit_file if handlers else None,
    ))

    registry.register(ToolDef(
        name="write_file",
        description="Create a new file with specified content.",
        tier=2, category="file",
        handler=handlers.write_file if handlers else None,
    ))

    registry.register(ToolDef(
        name="bash",
        description="Full shell access (restricted blocklist applies).",
        tier=2, category="bash",
        handler=handlers.bash if handlers else None,
    ))

    registry.register(ToolDef(
        name="web_fetch",
        description="Fetch content from a URL.",
        tier=2, category="web",
        handler=handlers.web_fetch if handlers else None,
    ))

    registry.register(ToolDef(
        name="web_search",
        description="Search the web via SearXNG.",
        tier=2, category="web",
        handler=handlers.web_search if handlers else None,
    ))

    registry.register(ToolDef(
        name="spawn_subagent",
        description="Launch a focused subagent for a specific subtask.",
        tier=2, category="task",
        handler=handlers.spawn_subagent if handlers else None,
    ))
