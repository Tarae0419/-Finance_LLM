import os

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.exc import OperationalError

from finreg.config import Settings
from finreg.main import create_app


def test_live_without_database_is_not_reported_as_ready():
    settings = Settings(_env_file=None, database_url=None)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["database"] == "not_configured"


def test_database_failure_does_not_leak_credentials(monkeypatch):
    secret = "private-database-password"
    settings = Settings(
        _env_file=None,
        database_url=SecretStr(f"postgresql+psycopg://finreg:{secret}@localhost/finreg"),
    )

    def fail_check(engine):
        raise OperationalError("SELECT 1", {}, Exception(secret))

    monkeypatch.setattr("finreg.main.check_database", fail_check)
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["database"] == "unavailable"
        assert secret not in response.text
        assert client.get("/health/live").status_code == 200


def test_settings_require_postgresql_and_hide_invalid_input():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, database_url=SecretStr("sqlite:///private-path.db"))
    assert "private-path" not in str(error.value)


@pytest.mark.integration
def test_readiness_against_real_postgresql():
    url = os.environ.get("FINREG_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set FINREG_TEST_DATABASE_URL to a disposable PostgreSQL database")
    settings = Settings(_env_file=None, database_url=SecretStr(url))
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["database"] == "available"
