from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr
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
    redis_url: str = "redis://localhost:6379/0"

    jwt_secret: SecretStr = SecretStr("dev-only-secret")
    jwt_ttl_minutes: int = 480

    gemini_api_key: SecretStr = SecretStr("")
    gemini_model_fast: str = "gemini-3.5-flash-lite"
    gemini_model_smart: str = "gemini-3.5-flash"

    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dim: int = 384


@lru_cache
def get_settings() -> Settings:
    return Settings()
