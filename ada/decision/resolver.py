"""Action Resolver — deterministic code, no LLM.

LLM classifies intent. This code decides what action to take.
Budget checks, band enforcement, retry limits — all deterministic.
"""

from dataclasses import dataclass, field
from .event import Classification, Action


@dataclass
class ActionPlan:
    actions: list[Action]
    band: int                               # highest band in chain
    requires_user_decision: bool = False
    escalation_reason: str | None = None


class ActionResolver:
    def __init__(self, budget_checker, state_checker):
        self.budget = budget_checker
        self.state = state_checker

    def resolve(self, classification: Classification) -> ActionPlan:
        c = classification

        # Rule 0: Low confidence → ask clarification
        if c.confidence < 0.6 and c.intent not in ("greeting", "farewell", "noop", "acknowledgement"):
            return ActionPlan(
                actions=[Action(action="ask_clarification", band=1)],
                band=1,
            )

        # Rule 1: Greetings / farewells / acks → respond only
        if c.intent in ("greeting", "farewell", "acknowledgement"):
            return ActionPlan(
                actions=[Action(action="respond_only", band=1)],
                band=1,
            )

        # Rule 2: Noop → noop
        if c.intent == "noop":
            return ActionPlan(actions=[Action(action="noop", band=1)], band=1)

        # Rule 3: Agent delivery → validate result
        if c.intent == "agent_delivery":
            return ActionPlan(
                actions=[Action(action="validate_result", band=2)],
                band=2,
            )

        # Rule 4: Timer trigger → execute command
        if c.intent == "timer_trigger":
            return ActionPlan(
                actions=[Action(action="execute_command", band=1)],
                band=1,
            )

        # Rule 5: System event → handle internally
        if c.intent == "system_event":
            return ActionPlan(
                actions=[Action(action="save_silently", band=1)],
                band=1,
            )

        # Rule 6: Explicit note → create note
        if c.intent == "note":
            return ActionPlan(
                actions=[Action(action="create_note", band=1)],
                band=1,
            )

        # Rule 7: Brainstorm → create note (might promote later)
        if c.intent == "brainstorm":
            if self._is_complex_enough_for_ultraplan(c):
                return ActionPlan(
                    actions=[
                        Action(action="create_note", band=1),
                        Action(action="queue_ultraplan", band=2),
                    ],
                    band=2,
                )
            return ActionPlan(
                actions=[Action(action="create_note", band=1)],
                band=1,
            )

        # Rule 8: Followup → update existing note or create new
        if c.intent == "followup":
            if c.references_existing and self.state.has_active_note():
                return ActionPlan(
                    actions=[Action(action="update_note", band=1)],
                    band=1,
                )
            return ActionPlan(
                actions=[Action(action="create_note", band=1)],
                band=1,
            )

        # Rule 9: Question → check if needs agent
        if c.intent == "question":
            if c.target_agent:
                if not self.budget.allows_dispatch(c.target_agent):
                    return self._budget_escalation()
                return ActionPlan(
                    actions=[
                        Action(action="promote_note", band=1),
                        Action(action="create_task", band=2),
                        Action(action="dispatch_agent", band=2, target=c.target_agent),
                    ],
                    band=2,
                )
            return ActionPlan(
                actions=[Action(action="respond_only", band=1)],
                band=1,
            )

        # Rule 10: Dispatch → promote note, create task, dispatch
        if c.intent == "dispatch":
            target = c.target_agent or "ARCH"  # default to ARCH if unclear
            if not self.budget.allows_dispatch(target):
                return self._budget_escalation()
            chain = []
            if c.references_existing and self.state.has_active_note():
                chain.append(Action(action="promote_note", band=1))
            chain.append(Action(action="create_task", band=2))
            chain.append(Action(action="dispatch_agent", band=2, target=target))
            return ActionPlan(actions=chain, band=2)

        # Rule 11: Decision → update task with user's choice
        if c.intent == "decision":
            if self.state.awaiting_user_decision():
                return ActionPlan(
                    actions=[Action(action="update_task", band=2)],
                    band=2,
                )
            return ActionPlan(
                actions=[Action(action="respond_only", band=1)],
                band=1,
            )

        # Rule 12: Clarification → respond with updated context
        if c.intent == "clarification":
            return ActionPlan(
                actions=[Action(action="respond_only", band=1)],
                band=1,
            )

        # Rule 13: Command → execute
        if c.intent == "command":
            return ActionPlan(
                actions=[Action(action="execute_command", band=2)],
                band=2,
            )

        # Fallback: ask clarification
        return ActionPlan(
            actions=[Action(action="ask_clarification", band=1)],
            band=1,
        )

    def _budget_escalation(self) -> ActionPlan:
        return ActionPlan(
            actions=[Action(action="escalate_to_user", band=1, reason="budget_threshold")],
            band=1,
            requires_user_decision=True,
            escalation_reason="budget_threshold",
        )

    def _is_complex_enough_for_ultraplan(self, c: Classification) -> bool:
        keywords = ("redesign", "from scratch", "rethink", "complete", "full plan", "architecture")
        return any(k in c.topic.lower() for k in keywords) and c.urgency != "high"
