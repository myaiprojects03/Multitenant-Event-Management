from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """All runtime config. Values come from env vars, then the repo-root .env file."""

    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env",),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "dev"
    log_level: str = "INFO"

    # Three DB identities:
    #  - owner: runs migrations + seeding (owns tables, bypasses RLS). Never used by request code.
    #  - app_rw: what the API uses. Not the owner, so every RLS policy applies.
    #  - app_readonly: NL-to-SQL only. SELECT on approved reporting views, nothing else.
    migration_database_url: str = "postgresql+asyncpg://eventai:eventai@localhost:5433/eventai"
    database_url: str = "postgresql+asyncpg://app_rw:app_rw_dev@localhost:5433/eventai"
    readonly_database_url: str = (
        "postgresql+asyncpg://app_readonly:app_readonly_dev@localhost:5433/eventai"
    )
    app_rw_password: SecretStr = SecretStr("app_rw_dev")
    app_readonly_password: SecretStr = SecretStr("app_readonly_dev")
    # Hosted Postgres (Render) hands out host/user/password separately: when DB_HOST is set, the
    # three URLs above are built from these parts and the app role passwords.
    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "eventai"
    db_user: str = "eventai"
    db_password: SecretStr = SecretStr("")
    redis_url: str = "redis://localhost:6379/0"

    # Hosted demo (free tier, no shell or separate worker): run the arq worker inside the API
    # process, and reseed the synthetic demo data when it is missing or dated before today.
    run_worker_in_process: bool = False
    demo_auto_seed: bool = False

    jwt_secret: SecretStr = SecretStr("dev-only-secret")
    jwt_ttl_minutes: int = 480

    gemini_api_key: SecretStr = SecretStr("")
    gemini_model_fast: str = "gemini-3.5-flash-lite"
    gemini_model_smart: str = "gemini-3.5-flash"
    # comma-separated, tried in order when the primary model is busy/unavailable
    gemini_fast_fallbacks: str = "gemini-3.1-flash-lite,gemini-3.5-flash"
    gemini_smart_fallbacks: str = "gemini-3.6-flash,gemini-3.5-flash-lite"
    llm_timeout_s: float = 30.0
    llm_attempts_per_model: int = 2
    llm_backoff_base_s: float = 1.0
    # a model that keeps answering 429/503/504 is skipped for this long (0 = off)
    llm_busy_cooldown_s: float = 120.0
    # optional, for cost telemetry; free tier = 0
    llm_price_in_per_mtok: float = 0.0
    llm_price_out_per_mtok: float = 0.0

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384
    embedding_cache_dir: str = str(_REPO_ROOT / "backend" / ".cache" / "fastembed")

    # simulated event platform -> us; HMAC-SHA256 over "{timestamp}.{body}"
    webhook_secret: SecretStr = SecretStr("dev-webhook-secret")
    webhook_max_skew_s: int = 300

    cache_default_ttl_s: int = 600
    # per user: requests that hit an LLM-backed endpoint
    rate_limit_per_minute: int = 20
    rate_limit_per_day: int = 300
    rate_limit_per_minute_organizer: int = 40

    # workflows: delivery retries (exponential backoff: base * 2^(attempt-1)) then dead-letter
    delivery_max_attempts: int = 4
    delivery_backoff_base_s: float = 5.0
    # lets a workflow's params ask the simulated sender to fail (demo + tests). Off in prod.
    allow_failure_injection: bool = True

    @model_validator(mode="after")
    def _compose_db_urls(self) -> "Settings":
        if self.db_host:
            where = f"{self.db_host}:{self.db_port}/{self.db_name}"

            def url(user: str, password: SecretStr) -> str:
                pw = quote(password.get_secret_value(), safe="")
                return f"postgresql+asyncpg://{quote(user, safe='')}:{pw}@{where}"

            self.migration_database_url = url(self.db_user, self.db_password)
            self.database_url = url("app_rw", self.app_rw_password)
            self.readonly_database_url = url("app_readonly", self.app_readonly_password)
        return self

    def model_chain(self, tier: str) -> list[str]:
        primary, fallbacks = (
            (self.gemini_model_fast, self.gemini_fast_fallbacks)
            if tier == "fast"
            else (self.gemini_model_smart, self.gemini_smart_fallbacks)
        )
        chain = [primary, *(m.strip() for m in fallbacks.split(",") if m.strip())]
        return list(dict.fromkeys(chain))  # dedupe, keep order


@lru_cache
def get_settings() -> Settings:
    return Settings()
