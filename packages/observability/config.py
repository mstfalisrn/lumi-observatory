# LUMI Agentic Observatory — central configuration
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../../.env"),
        extra="ignore",
        case_sensitive=False,
    )

    APP_ENV: str = "development"
    APP_TIMEZONE: str = "UTC"
    SCHEMA_VERSION: int = 1

    # DB — must be provided via environment / secrets file; no hardcoded password
    DATABASE_URL: str = ""
    REDIS_URL: str = "redis://localhost:6379/0"

    # Security — fail-closed in production; dev placeholders are non-secret and blocked in prod
    JWT_SECRET: str = ""
    SESSION_ENCRYPTION_MASTER_KEY: str = ""
    TELEGRAM_WEBHOOK_SECRET: str = ""
    CLOUDFLARE_ACCESS_AUD: str = ""
    CLOUDFLARE_ACCESS_CERT_PEM_PATH: str = ""

    # Postgres (used by docker-compose / backup script)
    POSTGRES_USER: str = "lumi"
    POSTGRES_DB: str = "lumi"
    POSTGRES_PASSWORD: str = ""

    # Local auth (no Cloudflare Access)
    ADMIN_EMAIL: str = "admin@example.com"
    ADMIN_PASSWORD_HASH: str = ""
    SESSION_TTL_SECONDS: int = 12 * 3600
    RATE_LIMIT_PER_MINUTE: int = 120
    RATE_LIMIT_TASK_PER_MINUTE: int = 20
    MAX_REQUEST_BODY_BYTES: int = 1_048_576

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_ALLOWED_USER_IDS: str = ""
    TELEGRAM_GROUP_ENABLED: bool = False

    # LLM
    LLM_PROVIDER: str = "mock"
    LLM_BASE_URL: str = ""
    LLM_MODEL: str = ""
    LLM_API_KEY: str = ""

    # GitHub — optional; empty means planner must not auto-generate github actions
    DEFAULT_GITHUB_REPO: str = ""

    # Guardrail
    RUN_MAX_ITERATIONS: int = 40
    RUN_MAX_TOOL_CALLS: int = 80
    RUN_MAX_WALL_SECONDS: int = 900
    RUN_MAX_TOKEN_BUDGET: int = 200_000
    RUN_MAX_COST_BUDGET: float = 5.0

    # Connector / SSRF
    CONNECTOR_ALLOWED_HOSTS: str = ""
    CONNECTOR_MAX_RESPONSE_BYTES: int = 1_048_576
    CONNECTOR_MAX_REDIRECTS: int = 3

    # Embedding (memory semantic retrieval)
    EMBEDDING_MODEL: str = ""  # if empty, LLM_MODEL is used
    EMBEDDING_DIM: int = 1536  # configurable by model (not a fixed assumption)

    # Memory auto-promote
    MEMORY_AUTO_PROMOTE_THRESHOLD: float = 0.85

    # Technocore — optional integration, disabled by default
    TECHNOCORE_ENABLED: bool = False
    TECHNOCORE_BASE_URL: str = ""
    TECHNOCORE_ROOM_CLAIM: str = ""
    TECHNOCORE_MONITORED_ROOMS: str = ""
    TECHNOCORE_ED25519_KEY_PATH: str = ""

    # API host/port (0.0.0.0 inside container; host binding is restricted to 127.0.0.1 via Docker port mapping)
    API_HOST: str = "0.0.0.0"  # nosec B104
    API_PORT: int = 8000
    WORKER_PORT: int = 8001
    SCHEDULER_PORT: int = 8002

    # UI
    VITE_API_BASE: str = "/api"

    @property
    def allowed_user_ids(self) -> set[int]:
        raw = self.TELEGRAM_ALLOWED_USER_IDS.strip()
        if not raw or raw == "*":
            return set()
        out: set[int] = set()
        for part in raw.split(","):
            part = part.strip()
            if part and part.lstrip("-").isdigit():
                out.add(int(part))
        return out

    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def technocore_rooms(self) -> list[str]:
        """Parse TECHNOCORE_MONITORED_ROOMS as comma-separated list."""
        raw = self.TECHNOCORE_MONITORED_ROOMS.strip()
        if not raw:
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]

    @property
    def secrets_file(self) -> Path:
        import os

        for cand in [
            os.getenv("SECRETS_FILE", ""),
            "./secrets/app.env",
            "/run/secrets/app.env",
        ]:
            if cand and Path(cand).exists():
                return Path(cand)
        return Path(".env")

    def validate_production(self) -> None:
        """Fail-closed: production requires real secrets, no placeholders."""
        if not self.is_production:
            return
        placeholders = {"CHANGE_ME", "dev-only-change-me", "dev-only-32-byte-master-key-0000000000", "dev-webhook-secret", "random", ""}
        errors: list[str] = []
        # DATABASE_URL must be set and not contain a placeholder password
        if not self.DATABASE_URL or "random" in self.DATABASE_URL:
            errors.append("DATABASE_URL must be set in production (no default password)")
        if not self.DATABASE_URL:
            errors.append("DATABASE_URL is required in production")
        # Security secrets must be set and not placeholders
        for name in ("JWT_SECRET", "SESSION_ENCRYPTION_MASTER_KEY", "TELEGRAM_WEBHOOK_SECRET", "POSTGRES_PASSWORD", "ADMIN_PASSWORD_HASH"):
            val = getattr(self, name, "")
            if not val or val in placeholders or "CHANGE_ME" in val:
                errors.append(f"{name} must be set to a real secret in production")
        if errors:
            raise RuntimeError("Production config invalid: " + "; ".join(errors))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
