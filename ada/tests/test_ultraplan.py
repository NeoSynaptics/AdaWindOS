"""Tests for UltraPlan — aristotle heuristic, reviewer summary, section extraction."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from ada.ultraplan.planner import UltraPlanner, PlanResult
from ada.ultraplan.reviewer import PlanReviewer, _extract_section
from ada.ultraplan.config import UltraPlanConfig
from ada.ultraplan.queue import PlanTask, PlanQueue, PlanStatus


@pytest.fixture
def config():
    return UltraPlanConfig()


@pytest.fixture
def mock_queue(mock_store):
    return PlanQueue(mock_store)


@pytest.fixture
def planner(config, mock_queue):
    return UltraPlanner(config, mock_queue)


@pytest.fixture
def reviewer(mock_queue):
    return PlanReviewer(mock_queue)


class TestAristotleHeuristic:

    def test_from_scratch_triggers(self, planner):
        task = PlanTask(task_id="t1", title="x", brief="Build from scratch a new auth system", domain="adaos")
        assert planner._should_use_aristotle(task)

    def test_rethink_triggers(self, planner):
        task = PlanTask(task_id="t1", title="x", brief="Rethink the memory architecture", domain="adaos")
        assert planner._should_use_aristotle(task)

    def test_general_domain_triggers(self, planner):
        task = PlanTask(task_id="t1", title="x", brief="Plan a vacation", domain="general")
        assert planner._should_use_aristotle(task)

    def test_incremental_does_not_trigger(self, planner):
        task = PlanTask(task_id="t1", title="x", brief="Add a button to the dashboard", domain="adaos")
        assert not planner._should_use_aristotle(task)

    def test_sportwave_incremental_no_trigger(self, planner):
        task = PlanTask(task_id="t1", title="x", brief="Fix the BLE timeout in SportWave app", domain="sportwave")
        assert not planner._should_use_aristotle(task)


class TestDomainContext:

    def test_known_domain(self, planner):
        ctx = planner._build_domain_context("sportwave")
        assert "SportWave" in ctx

    def test_unknown_domain_fallback(self, planner):
        ctx = planner._build_domain_context("unknown_project")
        assert "General" in ctx or "general" in ctx.lower()


class TestSectionExtraction:

    def test_extract_open_questions(self):
        text = """## Summary
Some summary here.

## Open Questions
- How should auth tokens be stored?
- What's the migration path?

## Validation Criteria
- All tests pass
"""
        questions = _extract_section(text, "Open Questions")
        assert len(questions) == 2
        assert "How should auth tokens be stored?" in questions

    def test_extract_missing_section(self):
        text = "## Summary\nJust a summary."
        assert _extract_section(text, "Open Questions") == []

    def test_extract_validation_criteria(self):
        text = """## Open Questions
- Q1

## Validation Criteria
- All tests pass
- Coverage > 80%
"""
        criteria = _extract_section(text, "Validation Criteria")
        assert len(criteria) == 2


class TestReviewerSummary:

    @pytest.mark.asyncio
    async def test_empty_queue_returns_empty(self, reviewer, mock_queue):
        mock_queue.get_completed_unreviewed = AsyncMock(return_value=[])
        result = await reviewer.get_review_summary()
        assert result == ""

    @pytest.mark.asyncio
    async def test_summary_with_plans(self, reviewer, mock_queue):
        plans = [
            PlanTask(task_id="p1", title="Plan A", brief="x", domain="sportwave",
                     status=PlanStatus.COMPLETED, final_plan="All good"),
            PlanTask(task_id="p2", title="Plan B", brief="y", domain="blackdirt",
                     status=PlanStatus.COMPLETED, final_plan="Has ## Open Questions\n- Q1"),
        ]
        mock_queue.get_completed_unreviewed = AsyncMock(return_value=plans)
        result = await reviewer.get_review_summary()
        assert "2 plan" in result
        assert "SportWave" in result or "Sportwave" in result
        assert "Blackdirt" in result or "BlackDirt" in result
        assert "open question" in result.lower()

    @pytest.mark.asyncio
    async def test_plan_detail(self, reviewer, mock_queue):
        plan = PlanTask(
            task_id="p1", title="Auth redesign", brief="Build auth",
            domain="adaos", status=PlanStatus.COMPLETED,
            final_plan="## Open Questions\n- How to store tokens?\n\n## Key Decisions Made\n- Use JWT",
            intermediate_outputs={"pass_1": "decomp", "pass_2": "critique"},
        )
        mock_queue.get_completed_unreviewed = AsyncMock(return_value=[plan])
        result = await reviewer.get_plan_detail("p1")
        assert result["title"] == "Auth redesign"
        assert "How to store tokens?" in result["open_questions"]
        assert "Use JWT" in result["key_decisions"]
        assert result["decomposition"] == "decomp"

    @pytest.mark.asyncio
    async def test_plan_detail_not_found(self, reviewer, mock_queue):
        mock_queue.get_completed_unreviewed = AsyncMock(return_value=[])
        result = await reviewer.get_plan_detail("nonexistent")
        assert "error" in result
