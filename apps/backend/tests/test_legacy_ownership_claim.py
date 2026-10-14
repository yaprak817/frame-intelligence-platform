import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.cli.claim_legacy_ownership import (
    LegacyOwnershipPlan,
    apply_plan,
    build_plan,
    normalize_email,
    run_async,
)


class Result:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class Session:
    def __init__(self, scalar_values, rowcounts=(3, 44)):
        self.scalar_values = list(scalar_values)
        self.rowcounts = list(rowcounts)
        self.committed = False
        self.rolled_back = False

    async def scalar(self, _statement):
        return self.scalar_values.pop(0)

    async def execute(self, _statement):
        return Result(self.rowcounts.pop(0))

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True


def test_normalize_email():
    assert normalize_email("  User@Example.COM ") == "user@example.com"


def test_build_plan_targets_registered_active_user():
    user_id = uuid4()
    session = Session(
        [
            SimpleNamespace(
                id=user_id,
                email="user@example.com",
                is_active=True,
            ),
            3,
            44,
        ]
    )

    plan = asyncio.run(build_plan(session, " User@Example.COM "))

    assert plan == LegacyOwnershipPlan(
        user_id=user_id,
        email="user@example.com",
        brand_count=3,
        job_count=44,
    )


def test_build_plan_rejects_unknown_user():
    session = Session([None])

    with pytest.raises(ValueError, match="Registered user not found"):
        asyncio.run(build_plan(session, "missing@example.com"))


def test_apply_plan_commits_only_after_all_legacy_rows_are_claimed():
    plan = LegacyOwnershipPlan(
        user_id=uuid4(),
        email="user@example.com",
        brand_count=3,
        job_count=44,
    )
    session = Session([0, 0])

    changed = asyncio.run(apply_plan(session, plan))

    assert changed == (3, 44)
    assert session.committed
    assert not session.rolled_back


def test_apply_plan_rolls_back_if_unowned_rows_remain():
    plan = LegacyOwnershipPlan(
        user_id=uuid4(),
        email="user@example.com",
        brand_count=3,
        job_count=44,
    )
    session = Session([1, 0])

    with pytest.raises(RuntimeError, match="transaction rolled back"):
        asyncio.run(apply_plan(session, plan))

    assert session.rolled_back
    assert not session.committed


def test_run_async_executes_coroutine():
    async def sample():
        return 42

    assert run_async(sample()) == 42
