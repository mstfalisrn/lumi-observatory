"""LUMI capability skills.

Skills are source-controlled JSON manifests. They describe bounded, auditable
capabilities; they are not executable prompts supplied by users. A manifest can
only reference registered read-only tools and selected task skills resolve their
targets from explicit task scope.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SkillRegistryError(ValueError):
    """Raised when a manifest is malformed or cannot safely form a task plan."""


# Keep this independent of executor.py to avoid a circular import in workers.
REGISTERED_TOOL_NAMES = frozenset(
    {
        "http_json_read",
        "github_repo_read",
        "internal_health",
        "technocore_read",
        "technocore_signed_write",
    }
)
_READ_ONLY_TOOL_NAMES = frozenset(
    {
        "http_json_read",
        "github_repo_read",
        "internal_health",
        "technocore_read",
    }
)
_SCOPE_REFERENCE = re.compile(r"^\$scope\.([a-zA-Z_][a-zA-Z0-9_]*)(?:\[(\d+)\])?$")


@dataclass(frozen=True)
class SkillDefinition:
    """A validated capability contract stored in one JSON manifest."""

    skill_id: str
    title: str
    description: str
    version: int
    execution_mode: str
    allowed_tools: tuple[str, ...]
    required_scope: tuple[str, ...]
    actions: tuple[dict[str, Any], ...]
    evidence_contract: tuple[str, ...]
    safety_notes: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any], source: Path) -> SkillDefinition:
        skill_id = str(payload.get("id") or "").strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,63}", skill_id):
            raise SkillRegistryError(f"{source.name}: invalid skill id")
        title = str(payload.get("title") or "").strip()
        description = str(payload.get("description") or "").strip()
        if not title or not description:
            raise SkillRegistryError(f"{source.name}: title and description are required")
        try:
            version = int(payload.get("version", 1))
        except (TypeError, ValueError) as exc:
            raise SkillRegistryError(f"{source.name}: version must be an integer") from exc
        if version < 1:
            raise SkillRegistryError(f"{source.name}: version must be positive")
        execution_mode = str(payload.get("execution_mode") or "task").strip().lower()
        if execution_mode not in {"task", "scheduled"}:
            raise SkillRegistryError(f"{source.name}: execution_mode must be task or scheduled")

        raw_tools = payload.get("allowed_tools") or []
        if not isinstance(raw_tools, list) or not all(isinstance(x, str) for x in raw_tools):
            raise SkillRegistryError(f"{source.name}: allowed_tools must be a string list")
        tools = tuple(dict.fromkeys(x.strip() for x in raw_tools if x.strip()))
        if any(tool not in REGISTERED_TOOL_NAMES for tool in tools):
            raise SkillRegistryError(f"{source.name}: manifest references an unknown tool")
        # A source-controlled capability catalog must never grant an automatic write path.
        if any(tool not in _READ_ONLY_TOOL_NAMES for tool in tools):
            raise SkillRegistryError(f"{source.name}: only read-only tools are allowed in manifests")

        raw_scope = payload.get("required_scope") or []
        if not isinstance(raw_scope, list) or not all(isinstance(x, str) and x for x in raw_scope):
            raise SkillRegistryError(f"{source.name}: required_scope must be a string list")
        required_scope = tuple(dict.fromkeys(raw_scope))

        raw_actions = payload.get("actions") or []
        if not isinstance(raw_actions, list) or not all(isinstance(x, dict) for x in raw_actions):
            raise SkillRegistryError(f"{source.name}: actions must be an object list")
        if execution_mode == "task" and not raw_actions:
            raise SkillRegistryError(f"{source.name}: task skills require at least one action")
        actions: list[dict[str, Any]] = []
        for action in raw_actions:
            tool = str(action.get("tool") or "")
            if tool not in tools:
                raise SkillRegistryError(f"{source.name}: action tool is outside allowed_tools")
            args = action.get("arguments") or {}
            if not isinstance(args, dict):
                raise SkillRegistryError(f"{source.name}: action arguments must be an object")
            actions.append(copy.deepcopy(action))

        def _string_tuple(field: str) -> tuple[str, ...]:
            raw = payload.get(field) or []
            if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
                raise SkillRegistryError(f"{source.name}: {field} must be a non-empty string list")
            return tuple(x.strip() for x in raw)

        return cls(
            skill_id=skill_id,
            title=title,
            description=description,
            version=version,
            execution_mode=execution_mode,
            allowed_tools=tools,
            required_scope=required_scope,
            actions=tuple(actions),
            evidence_contract=_string_tuple("evidence_contract"),
            safety_notes=_string_tuple("safety_notes"),
        )

    def summary(self) -> dict[str, Any]:
        """A browser-safe manifest summary; it contains no configuration or secret data."""
        return {
            "id": self.skill_id,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "execution_mode": self.execution_mode,
            "allowed_tools": list(self.allowed_tools),
            "required_scope": list(self.required_scope),
            "evidence_contract": list(self.evidence_contract),
            "safety_notes": list(self.safety_notes),
        }


class SkillRegistry:
    """Immutable registry loaded from trusted repository files."""

    def __init__(self, definitions: list[SkillDefinition]) -> None:
        by_id = {definition.skill_id: definition for definition in definitions}
        if len(by_id) != len(definitions):
            raise SkillRegistryError("duplicate skill id")
        self._definitions = by_id

    def get(self, skill_id: str) -> SkillDefinition:
        try:
            return self._definitions[skill_id]
        except KeyError as exc:
            raise SkillRegistryError("unknown skill") from exc

    def summaries(self) -> list[dict[str, Any]]:
        return [self._definitions[key].summary() for key in sorted(self._definitions)]

    def build_task_plan(self, skill_id: str, scope: dict[str, Any]) -> dict[str, Any]:
        definition = self.get(skill_id)
        if definition.execution_mode != "task":
            raise SkillRegistryError("skill is scheduled-only")
        scope = scope if isinstance(scope, dict) else {}
        for key in definition.required_scope:
            value = scope.get(key)
            if value is None or value == "" or value == []:
                raise SkillRegistryError("required skill scope is missing")

        actions: list[dict[str, Any]] = []
        for index, template in enumerate(definition.actions, start=1):
            action = copy.deepcopy(template)
            tool = str(action.get("tool") or "")
            if tool not in definition.allowed_tools:
                raise SkillRegistryError("manifest action is outside the skill contract")
            args = action.get("arguments") or {}
            action["arguments"] = _resolve_scope_references(args, scope)
            action["action_id"] = f"action_{index}"
            action.setdefault("reason", definition.description)
            action.setdefault("expected_evidence", list(definition.evidence_contract))
            action["action_class"] = "READ_ONLY"
            actions.append(action)

        return {
            "goal": definition.skill_id,
            "assumptions": ["Targets are supplied explicitly in task scope."],
            "success_criteria": list(definition.evidence_contract),
            "actions": actions,
            "_skill": {"id": definition.skill_id, "version": definition.version},
        }


def _resolve_scope_references(value: Any, scope: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_scope_references(item, scope) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_scope_references(item, scope) for item in value]
    if not isinstance(value, str):
        return value
    match = _SCOPE_REFERENCE.fullmatch(value)
    if match is None:
        return value
    key, raw_index = match.groups()
    if key not in scope:
        raise SkillRegistryError("skill scope reference is missing")
    selected: Any = scope[key]
    if raw_index is not None:
        if not isinstance(selected, list):
            raise SkillRegistryError("skill scope reference requires a list")
        index = int(raw_index)
        if index >= len(selected):
            raise SkillRegistryError("skill scope list reference is out of range")
        selected = selected[index]
    if isinstance(selected, (dict, list)):
        return copy.deepcopy(selected)
    if selected is None:
        raise SkillRegistryError("skill scope reference is empty")
    return str(selected)


def default_skill_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "skills"


def load_skill_registry(directory: Path | None = None) -> SkillRegistry:
    directory = directory or default_skill_dir()
    if not directory.is_dir():
        raise SkillRegistryError("skill directory is unavailable")
    definitions: list[SkillDefinition] = []
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SkillRegistryError(f"invalid skill manifest: {path.name}") from exc
        if not isinstance(payload, dict):
            raise SkillRegistryError(f"{path.name}: manifest root must be an object")
        definitions.append(SkillDefinition.from_payload(payload, path))
    if not definitions:
        raise SkillRegistryError("no skill manifests found")
    return SkillRegistry(definitions)
