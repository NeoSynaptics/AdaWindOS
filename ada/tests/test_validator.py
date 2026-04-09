"""Tests for validation gates — Gate 1 (structural), Gate 2 (technical), Gate 4 (attention)."""

import pytest
from ada.executive.validator import Validator, GateResult


@pytest.fixture
def validator():
    return Validator()


class TestGate1Structural:

    def test_no_artifacts_fails(self, validator):
        result = validator._gate_structural({}, [])
        assert not result.passed
        assert "no artifacts" in result.details.lower()

    def test_missing_type_fails(self, validator):
        result = validator._gate_structural({}, [{"content": "x"}])
        assert not result.passed
        assert "type" in result.details.lower()

    def test_missing_content_fails(self, validator):
        result = validator._gate_structural({}, [{"type": "code"}])
        assert not result.passed
        assert "content" in result.details.lower()

    def test_valid_artifact_passes(self, validator):
        result = validator._gate_structural({}, [{"type": "code", "content": "print('hi')"}])
        assert result.passed

    def test_expected_type_missing(self, validator):
        task = {"artifacts_expected": '["spec", "code"]'}
        artifacts = [{"type": "code", "content": "x"}]
        result = validator._gate_structural(task, artifacts)
        assert not result.passed
        assert "spec" in result.details

    def test_all_expected_types_present(self, validator):
        task = {"artifacts_expected": '["code"]'}
        artifacts = [{"type": "code", "content": "x"}]
        result = validator._gate_structural(task, artifacts)
        assert result.passed


class TestGate2Technical:

    @pytest.mark.asyncio
    async def test_valid_python_passes(self, validator):
        artifacts = [{"type": "code", "content": "x = 1\nprint(x)", "filename": "main.py"}]
        result = await validator._gate_technical({}, artifacts)
        assert result.passed

    @pytest.mark.asyncio
    async def test_syntax_error_fails(self, validator):
        artifacts = [{"type": "code", "content": "def foo(\n", "filename": "bad.py"}]
        result = await validator._gate_technical({}, artifacts)
        assert not result.passed
        assert "syntax error" in result.details.lower()

    @pytest.mark.asyncio
    async def test_test_failures_caught(self, validator):
        artifacts = [{"type": "test_results", "content": {"failed": 3, "passed": 10}}]
        result = await validator._gate_technical({}, artifacts)
        assert not result.passed
        assert "3 failures" in result.details

    @pytest.mark.asyncio
    async def test_non_python_passes_through(self, validator):
        artifacts = [{"type": "code", "content": "fn main() {}", "filename": "main.rs"}]
        result = await validator._gate_technical({}, artifacts)
        assert result.passed


class TestGate4Attention:

    def test_requires_decision_is_immediate(self, validator):
        result = validator._gate_attention({"requires_user_decision": True}, [])
        assert result.details == "immediate"

    def test_critical_priority_is_immediate(self, validator):
        result = validator._gate_attention({"priority": "critical"}, [])
        assert result.details == "immediate"

    def test_high_priority_is_delayed(self, validator):
        result = validator._gate_attention({"priority": "high"}, [])
        assert result.details == "delayed"

    def test_architecture_type_is_delayed(self, validator):
        result = validator._gate_attention({"type": "architecture"}, [])
        assert result.details == "delayed"

    def test_default_is_delayed(self, validator):
        result = validator._gate_attention({}, [])
        assert result.details == "delayed"


class TestFullValidation:

    @pytest.mark.asyncio
    async def test_all_gates_pass(self, validator):
        task = {"artifacts_expected": "[]"}
        artifacts = [{"type": "code", "content": "x = 1", "filename": "main.py"}]
        result = await validator.validate(task, artifacts)
        assert result.passed
        assert len(result.gates) == 4

    @pytest.mark.asyncio
    async def test_stops_on_first_failure(self, validator):
        result = await validator.validate({}, [])  # Gate 1 fails
        assert not result.passed
        assert len(result.gates) == 1
        assert result.gates[0].gate == 1
