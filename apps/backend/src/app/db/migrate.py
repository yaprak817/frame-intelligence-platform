"""Run Alembic while holding a bounded PostgreSQL advisory lock."""

from __future__ import annotations

import os
import subprocess
import time

import psycopg

LOCK_ID = 0x4652414D455F4D49  # "FRAME_MI", stable for this application.


def lock_timeout_seconds() -> int:
    raw = os.getenv("MIGRATION_LOCK_TIMEOUT_SECONDS", "60")
    try:
        value = int(raw)
    except ValueError as error:
        raise SystemExit("MIGRATION_LOCK_TIMEOUT_SECONDS must be an integer") from error
    if not 1 <= value <= 600:
        raise SystemExit("MIGRATION_LOCK_TIMEOUT_SECONDS must be between 1 and 600")
    return value


def database_dsn() -> str:
    value = os.environ.get("DATABASE_URL", "")
    if not value:
        raise SystemExit("DATABASE_URL is required")
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def main() -> int:
    deadline = time.monotonic() + lock_timeout_seconds()
    with psycopg.connect(database_dsn(), autocommit=True) as connection:
        while True:
            acquired = connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)
            ).fetchone()[0]
            if acquired:
                break
            if time.monotonic() >= deadline:
                raise SystemExit("Timed out waiting for the production migration lock")
            time.sleep(1)
        try:
            return subprocess.run(["alembic", "upgrade", "head"], check=False).returncode
        finally:
            connection.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


if __name__ == "__main__":
    raise SystemExit(main())
