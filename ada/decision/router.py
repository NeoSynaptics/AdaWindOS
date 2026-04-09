"""Agent routing — deterministic, keyword-based."""


ROUTING_TABLE: dict[str, str] = {
    # ARCH: architecture, design, research, analysis
    "architecture": "ARCH",
    "design": "ARCH",
    "tradeoff": "ARCH",
    "review": "ARCH",
    "research": "ARCH",
    "scan": "ARCH",
    "summarize": "ARCH",
    "compare": "ARCH",
    "analyze": "ARCH",
    "evaluate": "ARCH",
    "feasibility": "ARCH",
    # FORGE: implementation, code, build
    "implementation": "FORGE",
    "implement": "FORGE",
    "code": "FORGE",
    "prototype": "FORGE",
    "fix": "FORGE",
    "build": "FORGE",
    "write": "FORGE",
    "refactor": "FORGE",
    "test": "FORGE",
    "deploy": "FORGE",
}


def route_to_agent(topic: str, explicit_target: str | None = None) -> str:
    """Determine which agent should handle a task.

    If an explicit target was given in classification, use it.
    Otherwise, keyword-match against the topic.
    Default: ARCH (better to over-think than under-think).
    """
    if explicit_target in ("ARCH", "FORGE"):
        return explicit_target

    lower = topic.lower()
    for keyword, agent in ROUTING_TABLE.items():
        if keyword in lower:
            return agent

    return "ARCH"  # default: architecture/analysis
