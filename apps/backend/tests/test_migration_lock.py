from unittest.mock import MagicMock, patch

from app.db import migrate


def test_migration_runner_holds_lock_until_alembic_finishes(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@postgres/db")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (True,)
    events: list[str] = []
    connection.execute.side_effect = lambda sql, _params: (
        events.append("unlock") or MagicMock()
        if "unlock" in sql
        else events.append("lock") or MagicMock(fetchone=lambda: (True,))
    )
    with (
        patch.object(migrate.psycopg, "connect", return_value=connection),
        patch.object(
            migrate.subprocess,
            "run",
            side_effect=lambda *_args, **_kwargs: events.append("alembic")
            or MagicMock(returncode=0),
        ),
    ):
        assert migrate.main() == 0
    assert events == ["lock", "alembic", "unlock"]


def test_migration_runner_times_out_without_running_alembic(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@postgres/db")
    monkeypatch.setenv("MIGRATION_LOCK_TIMEOUT_SECONDS", "1")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = (False,)
    with (
        patch.object(migrate.psycopg, "connect", return_value=connection),
        patch.object(migrate.time, "sleep"),
        patch.object(migrate.time, "monotonic", side_effect=[0, 2]),
        patch.object(migrate.subprocess, "run") as run,
    ):
        try:
            migrate.main()
        except SystemExit as error:
            assert "Timed out" in str(error)
        else:
            raise AssertionError("lock timeout was accepted")
    run.assert_not_called()
