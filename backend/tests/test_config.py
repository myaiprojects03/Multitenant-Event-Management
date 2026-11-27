"""Hosted-config parsing: one owner URL + one app secret expand into every DB URL and secret."""

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings

NEON = "postgresql://neondb_owner:p%40ss@ep-x-123-pooler.us-east-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """CI exports JWT_SECRET, DATABASE_URL...: explicit values would win over derived ones."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def make(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


def test_owner_url_expands_to_three_direct_tls_urls():
    s = make(database_owner_url=SecretStr(NEON), app_secret=SecretStr("s3cret"))
    host = "ep-x-123.us-east-1.aws.neon.tech:5432/neondb?ssl=require"  # pooler dropped
    assert s.migration_database_url == f"postgresql+asyncpg://neondb_owner:p%40ss@{host}"
    assert s.database_url.startswith("postgresql+asyncpg://app_rw:")
    assert s.database_url.endswith(f"@{host}")
    assert s.readonly_database_url.startswith("postgresql+asyncpg://app_readonly:")


def test_app_secret_derives_distinct_stable_secrets_unless_set():
    a = make(app_secret=SecretStr("s3cret"))
    b = make(app_secret=SecretStr("s3cret"), webhook_secret=SecretStr("explicit"))
    derived = {
        a.app_rw_password.get_secret_value(),
        a.app_readonly_password.get_secret_value(),
        a.jwt_secret.get_secret_value(),
        a.webhook_secret.get_secret_value(),
    }
    assert len(derived) == 4 and all(len(v) == 64 for v in derived)
    assert b.jwt_secret.get_secret_value() == a.jwt_secret.get_secret_value()
    assert b.webhook_secret.get_secret_value() == "explicit"


def test_production_refuses_the_dev_jwt_secret():
    with pytest.raises(ValidationError):
        make(app_env="production", jwt_secret=SecretStr("dev-only-secret"))
    assert make(app_env="production", app_secret=SecretStr("s3cret")).app_env == "production"
