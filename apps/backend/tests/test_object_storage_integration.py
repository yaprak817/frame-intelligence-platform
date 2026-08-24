import asyncio
import os
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import boto3
import psycopg
import pytest
from botocore.config import Config
from fastapi.testclient import TestClient

from app.api.dependencies import get_job_service
from app.main import app
from app.storage.s3 import S3ResultObjectStorage

pytest_plugins = ("pytester",)

pytestmark = pytest.mark.skipif(
    os.environ.get("OBJECT_STORAGE_INTEGRATION") != "1",
    reason="OBJECT_STORAGE_INTEGRATION=1 is required",
)


class IntegrationCleanupError(RuntimeError):
    def __init__(self, errors: list[Exception]) -> None:
        super().__init__("Integration test cleanup failed")
        self.errors = errors


@dataclass
class UploadCleanupResources:
    s3: Any | None = None
    body: Any | None = None
    bucket: str = ""
    database_url: str = ""
    job_id: UUID | None = None
    object_keys: set[str] = field(default_factory=set)


@dataclass
class ResultCleanupResources:
    client: Any | None = None
    storage: Any | None = None
    body: Any | None = None
    bucket: str = ""
    object_key: str | None = None


def _run_cleanup_steps(steps: list[Callable[[], None]]) -> None:
    errors: list[Exception] = []
    for step in steps:
        try:
            step()
        except Exception as error:
            errors.append(error)
    if errors:
        raise IntegrationCleanupError(errors)


def _delete_job(database_url: str, job_id: UUID) -> None:
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))


def _cleanup_upload_resources(
    *,
    s3,
    bucket: str,
    database_url: str,
    job_id: UUID | None,
    object_keys: set[str],
    body=None,
) -> None:
    steps: list[Callable[[], None]] = [
        lambda object_key=object_key: s3.delete_object(Bucket=bucket, Key=object_key)
        for object_key in object_keys
    ]
    if job_id is not None:
        steps.append(lambda: _delete_job(database_url, job_id))
    if body is not None:
        steps.append(body.close)
    steps.append(s3.close)
    _run_cleanup_steps(steps)


def _cleanup_result_resources(resources: ResultCleanupResources) -> None:
    steps: list[Callable[[], None]] = []
    if resources.client is not None and resources.object_key is not None:
        steps.append(
            lambda: resources.client.delete_object(
                Bucket=resources.bucket, Key=resources.object_key
            )
        )
    if resources.body is not None:
        steps.append(resources.body.close)
    if resources.storage is not None:
        steps.append(lambda: asyncio.run(resources.storage.close()))
    if resources.client is not None:
        steps.append(resources.client.close)
    _run_cleanup_steps(steps)


@pytest.fixture
def upload_cleanup_resources():
    resources = UploadCleanupResources()
    yield resources
    if resources.s3 is not None:
        _cleanup_upload_resources(
            s3=resources.s3,
            bucket=resources.bucket,
            database_url=resources.database_url,
            job_id=resources.job_id,
            object_keys=resources.object_keys,
            body=resources.body,
        )


@pytest.fixture
def result_cleanup_resources():
    resources = ResultCleanupResources()
    yield resources
    _cleanup_result_resources(resources)


def test_upload_persists_real_object_job_and_outbox(upload_cleanup_resources) -> None:
    endpoint = os.environ["OBJECT_STORAGE_ENDPOINT"]
    bucket = os.environ["OBJECT_STORAGE_BUCKET"]
    s3 = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["OBJECT_STORAGE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["OBJECT_STORAGE_SECRET_KEY"],
        region_name=os.environ.get("OBJECT_STORAGE_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )
    upload_cleanup_resources.s3 = s3
    upload_cleanup_resources.bucket = bucket
    database_url = os.environ["DATABASE_URL"].replace(
        "postgresql+psycopg://", "postgresql://"
    )
    upload_cleanup_resources.database_url = database_url
    try:
        s3.create_bucket(Bucket=bucket)
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass

    content = b"deterministic-small-video-fixture"
    key = f"integration-upload-{uuid4()}"
    app.dependency_overrides.pop(get_job_service, None)
    with TestClient(
        app, backend_options={"loop_factory": asyncio.SelectorEventLoop}
    ) as client:
        response = client.post(
            "/api/v1/jobs/upload",
            headers={"Idempotency-Key": key},
            files={"file": ("clip.mp4", content, "video/mp4")},
        )
        if response.status_code == 202:
            upload_cleanup_resources.job_id = UUID(response.json()["job_id"])
            upload_cleanup_resources.object_keys.add(
                f"jobs/{upload_cleanup_resources.job_id}/source/original.mp4"
            )
        assert response.status_code == 202
        assert "credential" not in response.text.lower()

    job_id = upload_cleanup_resources.job_id
    assert job_id is not None
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT source_secret, source_reference "
                "FROM processing_jobs WHERE id=%s",
                (job_id,),
            )
            source_secret, reference = cursor.fetchone()
            upload_cleanup_resources.object_keys.add(reference["object_key"])
            cursor.execute(
                "SELECT payload FROM job_outbox WHERE aggregate_id=%s", (job_id,)
            )
            payload = cursor.fetchone()[0]
    body = s3.get_object(Bucket=bucket, Key=reference["object_key"])["Body"]
    upload_cleanup_resources.body = body
    assert body.read() == content
    assert source_secret is None
    assert reference["object_key"].startswith(f"jobs/{job_id}/source/")
    assert reference["size_bytes"] == len(content)
    assert payload == {"job_id": str(job_id)}


@pytest.mark.parametrize("failing_step", ["object", "database"])
def test_upload_cleanup_wiring_runs_object_database_and_client_steps(
    monkeypatch, failing_step
) -> None:
    calls: list[str] = []

    class FakeClient:
        def delete_object(self, **_kwargs) -> None:
            calls.append("object")
            if failing_step == "object":
                raise RuntimeError("object deletion failed")

        def close(self) -> None:
            calls.append("client")

    def delete_job(_database_url, _job_id) -> None:
        calls.append("database")
        if failing_step == "database":
            raise RuntimeError("database cleanup failed")

    monkeypatch.setattr("test_object_storage_integration._delete_job", delete_job)

    with pytest.raises(IntegrationCleanupError) as caught:
        _cleanup_upload_resources(
            s3=FakeClient(),
            bucket="test-bucket",
            database_url="postgresql://test",
            job_id=uuid4(),
            object_keys={"jobs/test/source/original.mp4"},
        )

    assert calls == ["object", "database", "client"]
    assert len(caught.value.errors) == 1


def test_result_cleanup_closes_body_storage_and_client_after_failures() -> None:
    calls: list[str] = []

    class FakeClient:
        def delete_object(self, **_kwargs) -> None:
            calls.append("object")

        def close(self) -> None:
            calls.append("client")
            raise RuntimeError("client close failed")

    class FakeBody:
        def close(self) -> None:
            calls.append("body")

    class FakeStorage:
        async def close(self) -> None:
            calls.append("storage")
            raise RuntimeError("storage close failed")

    resources = ResultCleanupResources(
        client=FakeClient(),
        storage=FakeStorage(),
        body=FakeBody(),
        bucket="test-bucket",
        object_key="integration-results/test/manifest.json",
    )

    with pytest.raises(IntegrationCleanupError) as caught:
        _cleanup_result_resources(resources)

    assert calls == ["object", "body", "storage", "client"]
    assert len(caught.value.errors) == 2


def test_yield_fixture_reports_test_failure_and_cleanup_error(pytester) -> None:
    pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def resource():
            yield
            raise RuntimeError("cleanup failed")

        def test_body(resource):
            assert False, "body failed"
        """
    )

    result = pytester.runpytest("-q")

    result.assert_outcomes(failed=1, errors=1)


def test_real_minio_presigned_result_url_supports_http_get(
    result_cleanup_resources,
) -> None:
    endpoint = os.environ["OBJECT_STORAGE_ENDPOINT"]
    external_endpoint = os.environ.get("OBJECT_STORAGE_EXTERNAL_ENDPOINT", endpoint)
    bucket = os.environ["OBJECT_STORAGE_BUCKET"]
    access_key = os.environ["OBJECT_STORAGE_ACCESS_KEY"]
    secret_key = os.environ["OBJECT_STORAGE_SECRET_KEY"]
    region = os.environ.get("OBJECT_STORAGE_REGION", "us-east-1")
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(s3={"addressing_style": "path"}),
    )
    storage = None
    key = f"integration-results/{uuid4()}/manifest.json"
    content = b'{"schema_version":1}'
    result_cleanup_resources.client = client
    result_cleanup_resources.bucket = bucket
    result_cleanup_resources.object_key = key
    try:
        client.create_bucket(Bucket=bucket)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=content,
        ContentType="application/json",
    )
    storage = S3ResultObjectStorage(
        internal_endpoint=endpoint,
        external_endpoint=external_endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        region=region,
        addressing_style="path",
    )
    result_cleanup_resources.storage = storage
    metadata = asyncio.run(storage.head(key))
    bounded = asyncio.run(storage.read_bounded(key, len(content)))
    signed = asyncio.run(storage.presign(key, 30))
    response = urllib.request.urlopen(signed.url, timeout=10)
    result_cleanup_resources.body = response
    downloaded = response.read()
    assert metadata.size_bytes == len(content)
    assert metadata.content_type == "application/json"
    assert bounded.payload == content
    assert downloaded == content
    assert external_endpoint in signed.url
