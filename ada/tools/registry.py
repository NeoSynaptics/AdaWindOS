"""Tool registry — category-based deferred loading.

Tier 1: always exposed (core tools Ada uses every turn)
Tier 2: conditionally loaded based on intent classifier tool_categories
Tier 3: specialist-only (used by workers, not Ada)
"""

from dataclasses import dataclass
from typing import Any, Callable, Awaitable


@dataclass
class ToolDef:
    name: str
    description: str
    tier: int               # 1=always, 2=conditional, 3=specialist-only
    category: str           # core | file | bash | schedule | web | task
    handler: Callable[..., Awaitable[Any]] | None = None


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolDef] = {}

    def register(self, tool: ToolDef) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDef | None:
        return self._tools.get(name)

    def get_tier1(self) -> list[ToolDef]:
        """Always-loaded tools."""
        return [t for t in self._tools.values() if t.tier == 1]

    def get_by_categories(self, categories: list[str]) -> list[ToolDef]:
        """Load Tier 2 tools matching requested categories."""
        tier1 = self.get_tier1()
        tier2 = [t for t in self._tools.values()
                 if t.tier == 2 and t.category in categories]
        return tier1 + tier2

    def get_tool_descriptions(self, categories: list[str] | None = None) -> str:
        """Format tool descriptions for the system prompt."""
        if categories:
            tools = self.get_by_categories(categories)
        else:
            tools = self.get_tier1()

        lines = []
        for t in tools:
            lines.append(f"- {t.name}: {t.description}")
        return "\n".join(lines)

    @property
    def all_tools(self) -> list[ToolDef]:
        return list(self._tools.values())
