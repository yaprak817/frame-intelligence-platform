import base64
import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

TEST_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"T" * 32).decode("ascii")
os.environ.setdefault("JOB_SOURCE_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY)
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://frame_user:change_me@localhost:5432/frame_intelligence",
)

from app.api.dependencies import get_job_service  # noqa: E402
from app.main import app  # noqa: E402
from app.models.job_outbox import JobOutbox  # noqa: E402
from app.models.processing_job import ProcessingJob  # noqa: E402
from app.repositories.jobs import JobRepository  # noqa: E402
from app.security.source_secrets import SourceSecretCipher  # noqa: E402
from app.services.job_service import JobService  # noqa: E402


class FakeJobRepository(JobRepository):
    def __init__(self) -> None:
        self.jobs: dict[object, ProcessingJob] = {}
        self.outbox: list[JobOutbox] = []
        self.fail_write = False

    async def get(self, job_id: object) -> ProcessingJob | None:
        return self.jobs.get(job_id)

    async def find_by_idempotency(self, scope: str, key: str) -> ProcessingJob | None:
        return next(
            (
                job
                for job in self.jobs.values()
                if job.idempotency_scope == scope and job.idempotency_key == key
            ),
            None,
        )

    async def create_with_outbox(self, job: ProcessingJob, event: JobOutbox) -> None:
        if self.fail_write:
            raise RuntimeError("simulated transaction failure")
        self.jobs[job.id] = job
        self.outbox.append(event)


@pytest.fixture
def repository() -> FakeJobRepository:
    return FakeJobRepository()


@pytest.fixture
def cipher() -> SourceSecretCipher:
    return SourceSecretCipher(TEST_ENCRYPTION_KEY)


@pytest.fixture
def client(
    repository: FakeJobRepository,
    cipher: SourceSecretCipher,
) -> Iterator[TestClient]:
    service = JobService(repository, cipher)
    app.dependency_overrides[get_job_service] = lambda: service
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
