"""Database lifecycle and a read-only readiness probe."""

from sqlalchemy import Engine, create_engine, text

from finreg.config import Settings


def create_database_engine(settings: Settings) -> Engine | None:
    if settings.database_url is None:
        return None
    return create_engine(
        settings.database_url.get_secret_value(),
        pool_pre_ping=True,
        connect_args={"connect_timeout": settings.database_connect_timeout_seconds},
        hide_parameters=True,
    )


def check_database(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
