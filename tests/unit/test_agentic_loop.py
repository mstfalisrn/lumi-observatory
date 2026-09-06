# LUMI — PHASE 3 agentic loop tests
import asyncio
import json

import pytest

from agent_core.coordinator import RunBudget, RunCoordinator
from agent_core.executor import ToolExecutor, ToolRegistry
from agent_core.llm import LLMProvider, LLMResult, MockProvider
from agent_core.planner import Planner
from agent_core.verifier import DefaultVerifier
from context_engine.assembler import ContextAssembler
from policy.engine import PolicyDecision


class _JSONProvider(LLMProvider):
    """Provider that produces deterministic valid JSON plans (for provider_calls counter)."""
    name = "json_test"

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, tools=None, **kw):
        self.calls += 1
        plan = {"goal": "observe", "assumptions": [], "success_criteria": ["evidence"],
                "actions": [{"action_id": "action_1", "tool": "http_json_read",
                             "arguments": {"url": "https://example.com/data.json"},
                             "reason": "fetch", "expected_evidence": [], "action_class": "READ_ONLY"},
                            {"action_id": "action_2", "tool": "github_repo_read",
                             "arguments": {"repo": "example-owner/example-repo"},
                             "reason": "repo", "expected_evidence": [], "action_class": "READ_ONLY"}]}
        return LLMResult(text=json.dumps(plan), finish_reason="stop",
                         usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})

    async def check(self):
        return True


class TestPlanner:
    def test_provider_called(self):
        p = _JSONProvider()
        pl = Planner(provider=p)
        plan = asyncio.run(pl.make_plan({"title": "t", "prompt": "p", "scope": {"kind": "observe"}}))
        assert p.calls > 0, "provider not called"
        assert plan["actions"], "actions empty"

    def test_actions_have_arguments(self):
        p = _JSONProvider()
        pl = Planner(provider=p)
        plan = asyncio.run(pl.make_plan({"title": "t", "prompt": "p", "scope": {"kind": "observe"}}))
        act = plan["actions"][0]
        assert act["tool"] == "http_json_read"
        assert act["arguments"].get("url") == "https://example.com/data.json"
        # github_repo_read action must be filtered: no DEFAULT_GITHUB_REPO configured
        tools = [a["tool"] for a in plan["actions"]]
        assert "github_repo_read" not in tools

    def test_repo_action_passes_when_default_repo_configured(self, monkeypatch):
        from observability.config import settings

        monkeypatch.setattr(settings, "DEFAULT_GITHUB_REPO", "example-owner/example-repo")
        p = _JSONProvider()
        pl = Planner(provider=p)
        plan = asyncio.run(pl.make_plan({"title": "t", "prompt": "p", "scope": {"kind": "observe"}}))
        tools = [a["tool"] for a in plan["actions"]]
        assert "github_repo_read" in tools
        repo_acts = [a for a in plan["actions"] if a["tool"] == "github_repo_read"]
        assert repo_acts[0]["arguments"].get("repo") == "example-owner/example-repo"

    def test_template_fallback_has_required_args(self):
        pl = Planner(provider=None)  # template fallback — generic safe actions only
        plan = asyncio.run(pl.make_plan({"title": "t", "prompt": "p", "scope": {"kind": "observe"}}))
        # Template fallback is restricted to safe local health checks.
        for act in plan["actions"]:
            assert act["tool"] in ("internal_health",), f"unexpected fallback tool: {act['tool']}"
            assert act["arguments"] == {}

    def test_unknown_tool_rejected(self):
        from agent_core.planner import PlanAction
        with pytest.raises(Exception):
            PlanAction(action_id="action_1", tool="totally_unknown_tool", arguments={})


class TestCoordinatorArgs:
    def test_args_passed_to_executor(self):
        reg = ToolRegistry()
        calls = {}

        async def rec(**kw):
            calls.update(kw)
            return {"ok": True}

        reg.register("technocore_read", rec, {"parameters": {"type": "object", "properties": {"room": {"type": "string"}, "since": {"type": "integer"}}}})
        executor = ToolExecutor(reg, task={"scope": {"kind": "observe"}, "prompt": "p", "title": "t"})

        class _FixedPlanner:
            async def make_plan(self, task):
                return {"goal": "observe", "actions": [
                    {"action_id": "action_1", "tool": "technocore_read",
                     "arguments": {"room": "test-room", "since": 5}, "reason": "", "expected_evidence": [], "action_class": "READ_ONLY"}]}

        class _AllowPolicy:
            def decide(self, tool):
                return PolicyDecision("READ_ONLY", "ALLOW", "test")

        coord = RunCoordinator(run_id="r-1", budget=RunBudget(max_iterations=5, max_tool_calls=5))
        asyncio.run(coord.run(executor, _FixedPlanner(), ContextAssembler(),
                              _AllowPolicy(), MockProvider(), DefaultVerifier()))
        assert calls.get("room") == "test-room", "argument not passed to executor"
        assert calls.get("since") == 5


class TestRunOutcome:
    def _setup(self, tool_fn):
        reg = ToolRegistry()
        reg.register("internal_health", tool_fn, {"parameters": {"type": "object", "properties": {}}})
        executor = ToolExecutor(reg, task={"scope": {"kind": "source_health"}, "prompt": "p", "title": "t"})

        class _FixedPlanner:
            async def make_plan(self, task):
                return {"goal": "source_health", "actions": [
                    {"action_id": "action_1", "tool": "internal_health", "arguments": {},
                     "reason": "", "expected_evidence": [], "action_class": "READ_ONLY"}]}

        class _AllowPolicy:
            def decide(self, tool):
                return PolicyDecision("READ_ONLY", "ALLOW", "test")

        return executor, _FixedPlanner(), _AllowPolicy()

    def test_success_completed(self):
        async def ok(**kw):
            return {"healthy": True}
        executor, planner, policy = self._setup(ok)
        coord = RunCoordinator(run_id="r-1", budget=RunBudget(max_iterations=5, max_tool_calls=5))
        status, _, _ = asyncio.run(coord.run(executor, planner, ContextAssembler(),
                                             policy, MockProvider(), DefaultVerifier()))
        assert status == "COMPLETED"

    def test_error_not_completed(self):
        async def fail(**kw):
            raise RuntimeError("boom")
        executor, planner, policy = self._setup(fail)
        coord = RunCoordinator(run_id="r-1", budget=RunBudget(max_iterations=5, max_tool_calls=5))
        status, _, _ = asyncio.run(coord.run(executor, planner, ContextAssembler(),
                                             policy, MockProvider(), DefaultVerifier()))
        assert status == "FAILED", f"beklenen FAILED, gelen {status}"

    def test_token_usage_recorded(self):
        async def ok(**kw):
            return {"healthy": True}
        executor, planner, policy = self._setup(ok)
        coord = RunCoordinator(run_id="r-1", budget=RunBudget(max_iterations=5, max_tool_calls=5))
        asyncio.run(coord.run(executor, planner, ContextAssembler(),
                              policy, MockProvider(), DefaultVerifier()))
        assert coord.tokens_used >= 0 and coord.cost_used >= 0.0
