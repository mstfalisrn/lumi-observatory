# LUMI — Planner (LLM → Pydantic-validated action plan with arguments)
from __future__ import annotations

import json

from pydantic import BaseModel, Field, field_validator

from agent_core.llm import LLMMessage, LLMProvider
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
            raise ValueError(f"bilinmeyen tool: {v}")
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
        """If LLM exists, produce a real plan; otherwise, template fallback with arguments."""
        goal = (task.get("scope") or {}).get("kind", "observe")
        title = task.get("title", "")
        prompt = task.get("prompt", "")

        if self.provider is not None:
            try:
                sys_msg = (
                    "You are the LUMI planner. Return ONLY valid JSON. Schema:\n"
                    '{"goal": str, "assumptions": [str], "success_criteria": [str], '
                    '"actions": [{"action_id":"action_1","tool":str,"arguments":{...},'
                    '"reason":str,"expected_evidence":[str],"action_class":"READ_ONLY"}]}\n'
                    "Available tools and their required arguments:\n"
                    "- http_json_read: {url (required)}\n"
                    "- github_repo_read: {repo (required) — in owner/repo format, the repo specified by the user}\n"
                    "- technocore_read: {room, since} (only if Technocore enabled)\n"
                    "- internal_health: {}\n"
                    "- technocore_signed_write: {room, payload, idempotency_key} (approval required, only if Technocore enabled)\n"
                    "Action IDs must be sequential: action_1, action_2, ...\n"
                    "Do NOT invent personal repos or Technocore rooms. Only use repos/URLs the user explicitly provided.\n"
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
                    try:
                        parsed = json.loads(text)
                        # Strip hallucinated repo actions when no default repo is configured:
                        # with DEFAULT_GITHUB_REPO unset, the operator did not point LUMI at
                        # any repo, so repo reads are filtered out entirely.
                        if isinstance(parsed.get("actions"), list):
                            parsed["actions"] = [
                                act for act in parsed["actions"]
                                if not (
                                    isinstance(act, dict)
                                    and act.get("tool") == "github_repo_read"
                                    and not settings.DEFAULT_GITHUB_REPO
                                )
                            ]
                            if not parsed["actions"]:
                                return self._template_plan(goal, title)
                        plan = TaskPlan(**parsed)  # Pydantic validation
                        out = plan.model_dump()
                        # Drop repo actions that carry no repo argument (invalid even when a default exists)
                        out["actions"] = [
                            a for a in out["actions"]
                            if not (a["tool"] == "github_repo_read" and not a["arguments"].get("repo"))
                        ]
                        if not out["actions"]:
                            return self._template_plan(goal, title)
                        out["_llm_usage"] = usage
                        return out
                    except Exception:
                        pass  # fallback if LLM output is invalid
            except Exception:
                pass

        return self._template_plan(goal, title)

    def _template_plan(self, goal: str, title: str) -> dict:
        """Deterministic fallback — only safe local/internal actions, no personal URLs/repos."""
        if goal == "source_health":
            actions = [{"action_id": "action_1", "tool": "internal_health", "arguments": {},
                        "reason": "Check internal service health", "expected_evidence": ["health status"],
                        "action_class": "READ_ONLY"}]
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
        return {"goal": goal, "assumptions": [], "success_criteria": ["evidence collected"],
                "actions": actions}
