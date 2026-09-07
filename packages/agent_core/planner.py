# LUMI — Planner (LLM → Pydantic-validated action plan with arguments)
from __future__ import annotations

import copy
import ipaddress
import json
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from agent_core.llm import LLMMessage, LLMProvider
from agent_core.skills import SkillRegistryError, load_skill_registry
from observability.config import settings

# Tool names registered in the registry (must be kept in sync with executor)
KNOWN_TOOLS = (
    "technocore_read",
    "github_repo_read",
    "http_json_read",
    "internal_health",
    "technocore_signed_write",
)


class PlanAction(BaseModel):
    action_id: str = Field(pattern=r"^action_\d+$")
    tool: str
    arguments: dict = Field(default_factory=dict)
    reason: str = ""
    expected_evidence: list[str] = Field(default_factory=list)
    action_class: str = Field(default="READ_ONLY")

    @field_validator("tool")
    @classmethod
    def _known_tool(cls, v: str) -> str:
        if v not in KNOWN_TOOLS:
            raise ValueError(f"unknown tool: {v}")
        return v


class TaskPlan(BaseModel):
    goal: str
    assumptions: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    actions: list[PlanAction] = Field(min_length=1, max_length=12)


class Planner:
    def __init__(self, provider: LLMProvider | None = None) -> None:
        self.provider = provider
        self.provider_calls = 0  # mandatory test: > 0 on real call

    async def make_plan(self, task: dict) -> dict:
        """Return a validated, capability-bounded plan.

        A selected source-controlled skill takes precedence over an LLM plan. An
        LLM may still propose read-only actions, but external targets must be
        explicitly present in task scope. Runtime connector checks remain in
        place; this is an earlier plan-safety gate, not a replacement for SSRF.
        """
        raw_scope = task.get("scope")
        scope: dict[str, Any] = raw_scope if isinstance(raw_scope, dict) else {}
        goal = str(scope.get("kind") or "observe")
        title = str(task.get("title") or "")
        prompt = str(task.get("prompt") or "")
        selected_skill = str(scope.get("skill_id") or "").strip()

        if selected_skill:
            try:
                plan = load_skill_registry().build_task_plan(selected_skill, scope)
            except SkillRegistryError:
                return self._guarded_empty_plan(goal, selected_skill, "selected_skill_unavailable")
            safe_actions, guard = self._sanitize_plan_actions(plan.get("actions") or [], scope)
            plan["actions"] = safe_actions
            if guard:
                plan["_plan_guard"] = guard
            return plan

        if self.provider is not None:
            try:
                sys_msg = (
                    "You are the LUMI planner. Return ONLY valid JSON. Schema:\n"
                    '{"goal": str, "assumptions": [str], "success_criteria": [str], '
                    '"actions": [{"action_id":"action_1","tool":str,"arguments":{...},'
                    '"reason":str,"expected_evidence":[str],"action_class":"READ_ONLY"}]}\n'
                    "Available tools and their required arguments:\n"
                    "- http_json_read: {url (required; only an exact URL from task scope.allowed_urls)}\n"
                    "- github_repo_read: {repo (required; only an exact repo from task scope.github_repos)}\n"
                    "- technocore_read: {room, since} (only configured rooms; only if Technocore enabled)\n"
                    "- internal_health: {}\n"
                    "- technocore_signed_write: {room, payload, idempotency_key} (approval required; explicit scope only)\n"
                    "Action IDs must be sequential: action_1, action_2, ...\n"
                    "Do NOT infer URLs, repositories, rooms, identities, or write targets from prompt text.\n"
                )
                user_msg = f"Task: {title} — {prompt}\nScope kind: {goal}\n"
                res = await self.provider.chat(
                    [LLMMessage("system", sys_msg), LLMMessage("user", user_msg)], tools=None
                )
                self.provider_calls += 1
                usage = getattr(res, "usage", {}) or {}
                if res.text:
                    text = res.text.strip()
                    if text.startswith("```"):
                        text = text.strip("`")
                        text = text.removeprefix("json")
                    parsed = json.loads(text)
                    plan = TaskPlan(**parsed).model_dump()
                    safe_actions, guard = self._sanitize_plan_actions(plan["actions"], scope)
                    plan["actions"] = safe_actions
                    plan["_llm_usage"] = usage
                    if guard:
                        plan["_plan_guard"] = guard
                    return plan
            except Exception:
                # Invalid model output must never become an unsafe action. The
                # deterministic fallback stays local/read-only.
                fallback = self._template_plan(goal, title)
                fallback["_plan_guard"] = {
                    "rejected_action_count": 0,
                    "reasons": ["invalid_llm_plan_fallback"],
                }
                return fallback

        return self._template_plan(goal, title)

    def _guarded_empty_plan(self, goal: str, skill_id: str, reason: str) -> dict:
        """Return no executable action when an explicit capability request is unsafe."""
        return {
            "goal": goal,
            "assumptions": [],
            "success_criteria": ["operator must provide a valid explicit scope"],
            "actions": [],
            "_skill": {"id": skill_id},
            "_plan_guard": {"rejected_action_count": 1, "reasons": [reason]},
        }

    @staticmethod
    def _scope_strings(scope: dict[str, Any], plural: str, singular: str | None = None) -> set[str]:
        raw = scope.get(plural, [])
        values: list[Any] = raw if isinstance(raw, list) else [raw]
        if singular and scope.get(singular) not in (None, ""):
            values.append(scope[singular])
        return {str(value).strip() for value in values if isinstance(value, str) and value.strip()}

    @staticmethod
    def _http_target_is_preflight_safe(url: str) -> bool:
        """Cheap non-network preflight; HttpJsonConnector validates DNS again at execution."""
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return False
            if parsed.username or parsed.password:
                return False
            host = parsed.hostname.rstrip(".").lower()
            if host in {"localhost", "metadata.google.internal", "metadata.google.com", "instance-data"}:
                return False
            if host.endswith(".internal") or host.endswith(".local"):
                return False
            if parsed.port is not None and parsed.port not in {80, 443, 8000, 8001, 8002, 3525}:
                return False
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                return True
            return not (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_unspecified
                or ip.is_reserved
            )
        except (TypeError, ValueError):
            return False

    def _sanitize_plan_actions(
        self, actions: list[dict[str, Any]], scope: dict[str, Any]
    ) -> tuple[list[dict], dict | None]:
        """Drop unscoped external actions and expose only non-sensitive reason codes."""
        allowed_urls = self._scope_strings(scope, "allowed_urls", "url")
        allowed_repos = self._scope_strings(scope, "github_repos", "github_repo")
        if settings.DEFAULT_GITHUB_REPO:
            allowed_repos.add(settings.DEFAULT_GITHUB_REPO.strip())
        allowed_rooms = self._scope_strings(scope, "allowed_rooms", "room")
        if settings.TECHNOCORE_ENABLED:
            allowed_rooms.update(settings.technocore_rooms)
        configured_hosts = {
            value.strip().lower() for value in settings.CONNECTOR_ALLOWED_HOSTS.split(",") if value.strip()
        }

        safe_actions: list[dict] = []
        reasons: list[str] = []
        for raw_action in actions:
            action = copy.deepcopy(raw_action) if isinstance(raw_action, dict) else {}
            tool = str(action.get("tool") or "")
            arguments = action.get("arguments") if isinstance(action.get("arguments"), dict) else {}
            reason: str | None = None

            if tool not in KNOWN_TOOLS:
                reason = "unknown_tool"
            elif tool == "http_json_read":
                target = str(arguments.get("url") or "")
                if target not in allowed_urls:
                    reason = "http_target_not_explicitly_scoped"
                elif not self._http_target_is_preflight_safe(target):
                    reason = "http_target_preflight_rejected"
                elif configured_hosts and (urlparse(target).hostname or "").rstrip(".").lower() not in configured_hosts:
                    reason = "http_host_outside_allowlist"
            elif tool == "github_repo_read":
                target = str(arguments.get("repo") or "")
                if target not in allowed_repos:
                    reason = "github_target_not_explicitly_scoped"
            elif tool == "technocore_read":
                room = str(arguments.get("room") or "")
                if not settings.TECHNOCORE_ENABLED:
                    reason = "technocore_connector_disabled"
                elif room not in allowed_rooms:
                    reason = "technocore_room_not_explicitly_scoped"
            elif tool == "technocore_signed_write":
                room = str(arguments.get("room") or "")
                if not settings.TECHNOCORE_ENABLED:
                    reason = "technocore_connector_disabled"
                elif not bool(scope.get("allow_public_write")):
                    reason = "public_write_not_explicitly_scoped"
                elif room not in allowed_rooms:
                    reason = "technocore_room_not_explicitly_scoped"

            if reason:
                reasons.append(reason)
                continue
            action["arguments"] = arguments
            safe_actions.append(action)

        if not reasons:
            return safe_actions, None
        return safe_actions, {
            "rejected_action_count": len(reasons),
            "reasons": sorted(set(reasons)),
        }

    def _template_plan(self, goal: str, title: str) -> dict:
        """Deterministic fallback — only safe local/internal actions, no personal URLs/repos."""
        if goal == "source_health":
            actions = [
                {
                    "action_id": "action_1",
                    "tool": "internal_health",
                    "arguments": {},
                    "reason": "Check internal service health",
                    "expected_evidence": ["health status"],
                    "action_class": "READ_ONLY",
                }
            ]
        else:
            # Generic observe/investigate: safe local check only.
            # Do NOT auto-reference personal repos, Technocore rooms, or external URLs.
            actions = [
                {
                    "action_id": "action_1",
                    "tool": "internal_health",
                    "arguments": {},
                    "reason": "Check internal service health",
                    "expected_evidence": ["health status"],
                    "action_class": "READ_ONLY",
                }
            ]
        return {"goal": goal, "assumptions": [], "success_criteria": ["evidence collected"], "actions": actions}
