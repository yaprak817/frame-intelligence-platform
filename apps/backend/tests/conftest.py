import asyncio
import base64
import hashlib
import os
from collections.abc import Iterator
from types import SimpleNamespace
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

TEST_ENCRYPTION_KEY = base64.urlsafe_b64encode(b"T" * 32).decode("ascii")
os.environ.setdefault("JOB_SOURCE_ENCRYPTION_KEY", TEST_ENCRYPTION_KEY)
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://frame_user:change_me@localhost:5432/frame_intelligence",
)

TEST_USER_ID = uuid4()
TEST_USER = SimpleNamespace(id=TEST_USER_ID, email="user@example.com")

from app.api.dependencies import get_current_user, get_job_service  # noqa: E402
from app.main import app  # noqa: E402
from app.models.job_outbox import JobOutbox  # noqa: E402
from app.models.processing_job import ProcessingJob  # noqa: E402
from app.repositories.jobs import JobRepository  # noqa: E402
from app.security.source_secrets import SourceSecretCipher  # noqa: E402
from app.services.job_service import JobService  # noqa: E402
from app.storage.s3 import S3ObjectReference  # noqa: E402


class FakeStorage:
    def __init__(self) -> None:
        self.uploads: list[str] = []
        self.deletes: list[S3ObjectReference] = []

    async def upload(self, file, object_key: str) -> S3ObjectReference:
        content = await file.read()
        self.uploads.append(object_key)
        return S3ObjectReference(
            1,
            "test-bucket",
            object_key,
            None,
            '"etag"',
            len(content),
            hashlib.sha256(content).hexdigest(),
        )

    async def delete(self, reference: S3ObjectReference) -> None:
        self.deletes.append(reference)


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
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def client(
    repository: FakeJobRepository,
    cipher: SourceSecretCipher,
    storage: FakeStorage,
) -> Iterator[TestClient]:
    service = JobService(repository, cipher, storage)
    app.dependency_overrides[get_job_service] = lambda: service
    app.dependency_overrides[get_current_user] = lambda: TEST_USER
    with TestClient(app) as test_client:

        class AllowingLimiter:
            async def check(self, **_kwargs):
                from app.security.rate_limit import RateLimitDecision

                return RateLimitDecision(True, 1)

        test_client.app.state.rate_limiter = AllowingLimiter()
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture
def real_authenticated_client() -> Iterator[tuple[TestClient, UUID]]:
    database_url = os.environ.get("DATABASE_URL")
    test_database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url or not test_database_url:
        raise RuntimeError(
            "Real authentication integration tests require DATABASE_URL and "
            "TEST_DATABASE_URL."
        )
    application_target = make_url(database_url)
    test_target = make_url(test_database_url)
    if (
        application_target.host != test_target.host
        or application_target.port != test_target.port
        or application_target.database != test_target.database
        or not (test_target.database or "").endswith("_test")
    ):
        raise RuntimeError(
            "Real authentication integration tests require DATABASE_URL to point "
            "at the isolated *_test database configured by TEST_DATABASE_URL."
        )

    app.dependency_overrides.pop(get_job_service, None)
    app.dependency_overrides.pop(get_current_user, None)
    with TestClient(
        app,
        backend_options={"loop_factory": asyncio.SelectorEventLoop},
    ) as test_client:
        email = f"backend-integration-{uuid4()}@example.test"
        password = f"Integration_{uuid4()}_Only!"
        test_client.headers.update({"Origin": str(test_client.base_url).rstrip("/")})
        registration = test_client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password},
        )
        assert registration.status_code == 201, (
            f"Test-user registration expected HTTP 201, got {registration.status_code}."
        )
        user_id = UUID(registration.json()["id"])
        assert test_client.cookies.get("frame_session")

        current_user = test_client.get("/api/v1/auth/me")
        assert current_user.status_code == 200, (
            "Test session expected HTTP 200 from auth/me, "
            f"got {current_user.status_code}."
        )
        assert UUID(current_user.json()["id"]) == user_id
        yield test_client, user_id

        psycopg_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        with psycopg.connect(psycopg_url) as connection:
            connection.execute("DELETE FROM users WHERE id=%s", (user_id,))
