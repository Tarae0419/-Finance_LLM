"""Application configuration; credentials never belong in API responses."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FINREG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    database_url: SecretStr | None = None
    database_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return value
        try:
            url = make_url(value.get_secret_value())
        except (ArgumentError, ValueError):
            raise ValueError("Use a valid PostgreSQL connection URL") from None
        if url.drivername != "postgresql+psycopg":
            raise ValueError("Use the postgresql+psycopg driver")
        if not url.host or not url.database:
            raise ValueError("A PostgreSQL host and database are required")
        return value
