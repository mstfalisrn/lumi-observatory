import asyncio
import json

import pytest

from agent_core.coordinator import RunBudget, RunCoordinator
from agent_core.executor import ToolExecutor, ToolRegistry
from agent_core.llm import LLMProvider, LLMResult, MockProvider
from agent_core.planner import Planner
from agent_core.skills import SkillRegistryError, load_skill_registry
from agent_core.verifier import DefaultVerifier
from context_engine.assembler import ContextAssembler
from policy.engine import PolicyDecision


class _UnsafeHttpPlanProvider(LLMProvider):
    name = "unsafe-http-plan"

    async def chat(self, messages, tools=None, **kw):
        payload = {
            "goal": "observe",
            "actions": [
                {
                    "action_id": "action_1",
                    "tool": "http_json_read",
                    "arguments": {"url": "http://127.0.0.1:8000/private"},
                    "reason": "bad target",
                    "expected_evidence": [],
                    "action_class": "READ_ONLY",
                }
            ],
        }
        return LLMResult(text=json.dumps(payload), usage={"total_tokens": 3})

    async def check(self):
        return True


class _GuardedPlanner:
    async def make_plan(self, task):
        return {
            "goal": "observe",
            "actions": [],
            "_plan_guard": {"rejected_action_count": 1, "reasons": ["http_target_not_explicitly_scoped"]},
        }


class _FailingPlanner:
    async def make_plan(self, task):
        return {
            "goal": "health",
            "actions": [
                {
                    "action_id": "action_1",
                    "tool": "internal_health",
                    "arguments": {},
                }
            ],
        }


class _AllowPolicy:
    def decide(self, tool):
        return PolicyDecision("READ_ONLY", "ALLOW", "test")


def test_manifest_registry_exposes_only_valid_capabilities():
    registry = load_skill_registry()
    ids = {item["id"] for item in registry.summaries()}
    assert {
        "system-health",
        "github-observation",
        "approved-http-observation",
        "technocore-read-observation",
        "risk-triage",
    } <= ids
    assert "technocore_signed_write" not in {tool for item in registry.summaries() for tool in item["allowed_tools"]}


def test_selected_http_skill_uses_explicit_scope_only():
    plan = load_skill_registry().build_task_plan(
        "approved-http-observation",
        {"allowed_urls": ["https://example.com/data.json"]},
    )
    assert plan["actions"][0]["tool"] == "http_json_read"
    assert plan["actions"][0]["arguments"] == {"url": "https://example.com/data.json"}
    with pytest.raises(SkillRegistryError):
        load_skill_registry().build_task_plan("approved-http-observation", {})
    with pytest.raises(SkillRegistryError):
        load_skill_registry().build_task_plan("risk-triage", {})


def test_planner_rejects_unscoped_or_private_http_before_executor():
    plan = asyncio.run(
        Planner(provider=_UnsafeHttpPlanProvider()).make_plan(
            {"title": "t", "prompt": "p", "scope": {"kind": "observe"}}
        )
    )
    assert plan["actions"] == []
    assert plan["_plan_guard"]["rejected_action_count"] == 1
    assert plan["_plan_guard"]["reasons"] == ["http_target_not_explicitly_scoped"]


def test_planner_builds_selected_skill_plan():
    plan = asyncio.run(
        Planner(provider=MockProvider()).make_plan(
            {
                "title": "health",
                "prompt": "check",
                "scope": {"skill_id": "system-health"},
            }
        )
    )
    assert plan["_skill"] == {"id": "system-health", "version": 1}
    assert plan["actions"][0]["tool"] == "internal_health"


def test_guarded_empty_plan_never_calls_executor_and_is_auditable():
    called = False

    async def should_not_run(**kwargs):
        nonlocal called
        called = True
        return {"ok": True}

    registry = ToolRegistry()
    registry.register("internal_health", should_not_run, {"parameters": {"type": "object", "properties": {}}})
    executor = ToolExecutor(registry, task={"scope": {}, "prompt": "p", "title": "t"})
    coordinator = RunCoordinator(run_id="guarded", budget=RunBudget(max_iterations=2, max_tool_calls=2))
    status, _, events = asyncio.run(
        coordinator.run(
            executor, _GuardedPlanner(), ContextAssembler(), _AllowPolicy(), MockProvider(), DefaultVerifier()
        )
    )
    assert status == "FAILED"
    assert coordinator.failure_code == "plan_guard_rejected"
    assert called is False
    assert any(event["event_type"] == "PLAN_GUARD" for event in events)


def test_tool_failure_has_safe_structured_reason():
    async def fail(**kwargs):
        raise RuntimeError("sensitive diagnostic should not be persisted")

    registry = ToolRegistry()
    registry.register("internal_health", fail, {"parameters": {"type": "object", "properties": {}}})
    executor = ToolExecutor(registry, task={"scope": {}, "prompt": "p", "title": "t"})
    coordinator = RunCoordinator(run_id="failure", budget=RunBudget(max_iterations=2, max_tool_calls=2))
    status, _, events = asyncio.run(
        coordinator.run(
            executor, _FailingPlanner(), ContextAssembler(), _AllowPolicy(), MockProvider(), DefaultVerifier()
        )
    )
    assert status == "FAILED"
    assert coordinator.failure_code == "tool_error:internal_health:RuntimeError"
    assert "sensitive diagnostic" not in coordinator.failure_code
    assert any(event["event_type"] == "TOOL_ERROR" for event in events)
