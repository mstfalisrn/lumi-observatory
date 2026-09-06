"""Public-repository configuration, privacy, and portability safeguards."""

from __future__ import annotations

import asyncio
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_config_has_no_absolute_deployment_paths() -> None:
    text = (REPO / "packages/observability/config.py").read_text()
    assert "/path/" + "secrets" not in text
    assert "/path/" + "apps" not in text


def test_database_url_has_no_default_password() -> None:
    text = (REPO / "packages/observability/config.py").read_text()
    assert 'DATABASE_URL: str = ""' in text


def test_production_configuration_fails_closed() -> None:
    text = (REPO / "packages/observability/config.py").read_text()
    assert "validate_production" in text
    assert "is_production" in text


def test_default_github_repo_is_empty() -> None:
    text = (REPO / "packages/observability/config.py").read_text()
    assert 'DEFAULT_GITHUB_REPO: str = ""' in text


def test_planner_fallback_has_no_embedded_external_targets() -> None:
    from agent_core.planner import Planner

    planner = Planner(provider=None)
    for kind in ("observe", "investigate", "source_health"):
        plan = asyncio.run(planner.make_plan({"title": "t", "prompt": "p", "scope": {"kind": kind}}))
        for action in plan["actions"]:
            assert "://" not in str(action.get("arguments", {}))


def test_optional_connector_is_disabled_and_unconfigured_by_default() -> None:
    from observability.config import Settings

    settings = Settings(_env_file=None)
    assert hasattr(settings, "TECHNOCORE_ENABLED")
    assert Settings.model_fields["TECHNOCORE_ENABLED"].default is False
    assert Settings.model_fields["TECHNOCORE_MONITORED_ROOMS"].default == ""
    assert Settings.model_fields["TECHNOCORE_BASE_URL"].default == ""


def test_compose_forwards_optional_connector_configuration() -> None:
    text = (REPO / "docker-compose.yml").read_text()
    assert text.count("TECHNOCORE_ENABLED: ${TECHNOCORE_ENABLED:-false}") == 3
    assert text.count("TECHNOCORE_MONITORED_ROOMS: ${TECHNOCORE_MONITORED_ROOMS:-}") == 1


def test_configured_rooms_requires_configuration() -> None:
    from apps.scheduler.agent_scorer import configured_rooms

    assert configured_rooms("") == []
    assert configured_rooms("  ") == []
    assert configured_rooms("a, b ,c") == ["a", "b", "c"]


def test_scorer_has_no_embedded_network_endpoint() -> None:
    text = (REPO / "apps/scheduler/agent_scorer.py").read_text()
    assert "TECHNOCORE_MONITORED_ROOMS" in text
    assert "https://" not in text


def test_systemd_template_is_portable() -> None:
    path = REPO / "infra/systemd/lumi-observatory.service"
    assert path.exists(), "lumi-observatory.service must exist"
    text = path.read_text()
    assert "/path/" + "apps" not in text
    assert "WorkingDirectory" in text


def test_timezone_defaults_to_utc() -> None:
    from observability.config import Settings

    assert Settings.model_fields["APP_TIMEZONE"].default == "UTC"


def test_env_example_keeps_optional_connector_disabled() -> None:
    text = (REPO / ".env.example").read_text()
    assert "TECHNOCORE_ENABLED=false" in text
    assert "APP_TIMEZONE=UTC" in text


def test_env_example_uses_placeholders_only() -> None:
    text = (REPO / ".env.example").read_text()
    assert "CHANGE_ME" in text
    assert "@example.com" in text
