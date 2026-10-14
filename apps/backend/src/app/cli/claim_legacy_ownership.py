from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionFactory
from app.models.auth import User
from app.models.brands import Brand
from app.models.processing_job import ProcessingJob


@dataclass(frozen=True)
class LegacyOwnershipPlan:
    user_id: UUID
    email: str
    brand_count: int
    job_count: int


def normalize_email(value: str) -> str:
    return value.strip().casefold()


async def build_plan(
    session: AsyncSession,
    email: str,
) -> LegacyOwnershipPlan:
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("Target email is required")

    user = await session.scalar(
        select(User).where(User.email == normalized)
    )
    if user is None:
        raise ValueError("Registered user not found")
    if not user.is_active:
        raise ValueError("Target user is inactive")

    brand_count = int(
        await session.scalar(
            select(func.count())
            .select_from(Brand)
            .where(Brand.owner_user_id.is_(None))
        )
        or 0
    )
    job_count = int(
        await session.scalar(
            select(func.count())
            .select_from(ProcessingJob)
            .where(ProcessingJob.owner_user_id.is_(None))
        )
        or 0
    )

    return LegacyOwnershipPlan(
        user_id=user.id,
        email=user.email,
        brand_count=brand_count,
        job_count=job_count,
    )


async def apply_plan(
    session: AsyncSession,
    plan: LegacyOwnershipPlan,
) -> tuple[int, int]:
    brand_result = await session.execute(
        update(Brand)
        .where(Brand.owner_user_id.is_(None))
        .values(owner_user_id=plan.user_id)
    )
    job_result = await session.execute(
        update(ProcessingJob)
        .where(ProcessingJob.owner_user_id.is_(None))
        .values(owner_user_id=plan.user_id)
    )

    remaining_brands = int(
        await session.scalar(
            select(func.count())
            .select_from(Brand)
            .where(Brand.owner_user_id.is_(None))
        )
        or 0
    )
    remaining_jobs = int(
        await session.scalar(
            select(func.count())
            .select_from(ProcessingJob)
            .where(ProcessingJob.owner_user_id.is_(None))
        )
        or 0
    )

    if remaining_brands or remaining_jobs:
        await session.rollback()
        raise RuntimeError(
            "Legacy ownership claim was incomplete; transaction rolled back"
        )

    await session.commit()
    return int(brand_result.rowcount or 0), int(job_result.rowcount or 0)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Assign legacy unowned brands and jobs to one registered user."
    )
    result.add_argument("email")
    result.add_argument(
        "--apply",
        action="store_true",
        help="Apply changes. Without this flag the command is read-only.",
    )
    result.add_argument(
        "--confirm-email",
        help="Required with --apply and must match the target email.",
    )
    return result


async def run(args: argparse.Namespace) -> int:
    async with SessionFactory() as session:
        plan = await build_plan(session, args.email)

        print(f"target_email={plan.email}")
        print(f"legacy_brands={plan.brand_count}")
        print(f"legacy_jobs={plan.job_count}")

        if not args.apply:
            print("mode=DRY_RUN")
            return 0

        if normalize_email(args.confirm_email or "") != plan.email:
            raise ValueError(
                "--confirm-email must exactly match the registered target email"
            )

        brands, jobs = await apply_plan(session, plan)
        print("mode=APPLIED")
        print(f"claimed_brands={brands}")
        print(f"claimed_jobs={jobs}")
        return 0


def run_async(coroutine):
    if sys.platform == "win32":
        with asyncio.Runner(
            loop_factory=asyncio.SelectorEventLoop
        ) as runner:
            return runner.run(coroutine)
    return asyncio.run(coroutine)


def main() -> None:
    args = parser().parse_args()
    try:
        raise SystemExit(run_async(run(args)))
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
