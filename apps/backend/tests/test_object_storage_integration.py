import asyncio
import os
from uuid import UUID, uuid4

import boto3
import psycopg
import pytest
from botocore.config import Config
from fastapi.testclient import TestClient

from app.api.dependencies import get_job_service
from app.main import app

pytestmark = pytest.mark.skipif(
    os.environ.get("OBJECT_STORAGE_INTEGRATION") != "1",
    reason="OBJECT_STORAGE_INTEGRATION=1 is required",
)


def test_upload_persists_real_object_job_and_outbox() -> None:
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
        assert response.status_code == 202
        assert "credential" not in response.text.lower()
        job_id = UUID(response.json()["job_id"])

    database_url = os.environ["DATABASE_URL"].replace(
        "postgresql+psycopg://", "postgresql://"
    )
    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT source_secret, source_reference "
                "FROM processing_jobs WHERE id=%s",
                (job_id,),
            )
            source_secret, reference = cursor.fetchone()
            cursor.execute(
                "SELECT payload FROM job_outbox WHERE aggregate_id=%s", (job_id,)
            )
            payload = cursor.fetchone()[0]
    try:
        stored = s3.get_object(Bucket=bucket, Key=reference["object_key"])
        assert stored["Body"].read() == content
        assert source_secret is None
        assert reference["object_key"].startswith(f"jobs/{job_id}/source/")
        assert reference["size_bytes"] == len(content)
        assert payload == {"job_id": str(job_id)}
    finally:
        s3.delete_object(Bucket=bucket, Key=reference["object_key"])
        with psycopg.connect(database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM processing_jobs WHERE id=%s", (job_id,))
