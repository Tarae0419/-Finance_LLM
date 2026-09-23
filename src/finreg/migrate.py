"""Apply additive SQL migrations atomically and detect changed migration files."""

import hashlib
from pathlib import Path

import psycopg
from sqlalchemy.engine import make_url

from finreg.config import Settings


def connect_database(settings: Settings):
    if settings.database_url is None:
        raise ValueError("FINREG_DATABASE_URL is required")
    url = make_url(settings.database_url.get_secret_value()).set(drivername="postgresql")
    return psycopg.connect(
        url.render_as_string(hide_password=False),
        connect_timeout=settings.database_connect_timeout_seconds,
    )


def migrate(connection, directory: Path) -> list[str]:
    paths = sorted(directory.glob("*.sql"))
    if not paths:
        raise ValueError("No SQL migrations found")
    applied = []
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(72401001)")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migration "
            "(name text PRIMARY KEY, sha256 text NOT NULL, applied_at timestamptz DEFAULT now())"
        )
        for path in paths:
            raw = path.read_bytes().replace(b"\r\n", b"\n")
            digest = hashlib.sha256(raw).hexdigest()
            row = connection.execute(
                "SELECT sha256 FROM schema_migration WHERE name = %s", (path.name,)
            ).fetchone()
            if row:
                if row[0] != digest:
                    raise ValueError(f"Applied migration changed: {path.name}")
                continue
            connection.execute(raw.decode("utf-8"))
            connection.execute(
                "INSERT INTO schema_migration (name, sha256) VALUES (%s, %s)",
                (path.name, digest),
            )
            applied.append(path.name)
    return applied


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    with connect_database(Settings()) as connection:
        print({"applied": migrate(connection, root / "migrations")})
