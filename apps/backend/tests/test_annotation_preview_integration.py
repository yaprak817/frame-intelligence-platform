import asyncio
import hashlib
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import boto3
import pytest
from PIL import Image

from app.domain.jobs import JobStatus, SourceType
from app.models.processing_job import ProcessingJob
from app.services.result_artifacts import ManifestInvalidError, ResultArtifactService
from app.storage.s3 import ObjectNotFoundError, S3ResultObjectStorage

ENDPOINT = os.environ.get("OBJECT_STORAGE_ENDPOINT")
pytestmark = pytest.mark.skipif(
    os.environ.get("OBJECT_STORAGE_INTEGRATION") != "1" or ENDPOINT is None,
    reason="OBJECT_STORAGE_INTEGRATION=1 is required",
)


class Repository:
    def __init__(self, job) -> None:
        self.job = job

    async def get(self, job_id):
        return self.job if job_id == self.job.id else None


def document(job_id, run_token, payload: bytes, yolo_key: str) -> bytes:
    digest = hashlib.sha256(payload).hexdigest()
    prefix = f"jobs/{job_id}/results/{run_token}"
    value = {
        "schema_version": 1,
        "dataset_type": "image",
        "job_id": str(job_id),
        "run_token": str(run_token),
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "summary": {
            "uploaded_files": 1,
            "accepted_files": 1,
            "normal": 1,
            "challenging": 0,
            "unusable": 0,
            "rejected": 0,
            "duplicates": 0,
            "ignored_metadata_entries": 0,
            "recommended_count": 1,
            "recommended_normal": 1,
            "recommended_challenging": 0,
            "target_challenging_ratio": 0.2,
            "actual_challenging_ratio": 0.0,
            "ratio_note": None,
        },
        "recommended_indices": [0],
        "images": [
            {
                "index": 0,
                "filename": "image_000001.jpg",
                "content_type": "image/jpeg",
                "size_bytes": len(payload),
                "sha256": digest,
                "width": 640,
                "height": 640,
                "quality_category": "normal",
                "sharpness": 10.0,
                "brightness": 100.0,
                "underexposed_ratio": 0.0,
                "overexposed_ratio": 0.0,
                "resolution_usable": True,
                "quality_score": 1.0,
                "duplicate": False,
                "object_key": f"{prefix}/images/image_000001.jpg",
                "yolo_object_key": yolo_key,
                "yolo_size_bytes": len(payload),
                "yolo_sha256": digest,
                "output_width": 640,
                "output_height": 640,
                "resize_scale": 1.0,
                "padding": {"top": 0, "right": 0, "bottom": 0, "left": 0},
            }
        ],
        "exports": {
            "accepted": {
                "object_key": f"{prefix}/exports/accepted.zip",
                "size_bytes": 1,
                "sha256": "a" * 64,
            },
            "yolo": {
                "object_key": f"{prefix}/exports/yolo.zip",
                "size_bytes": 1,
                "sha256": "a" * 64,
            },
        },
    }
    return json.dumps(value).encode()


def encoded_image(image_format: str, size: tuple[int, int]) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color=(24, 96, 160)).save(
        output, format=image_format, quality=90
    )
    return output.getvalue()


async def exercise_preview() -> None:
    assert ENDPOINT is not None
    access = os.environ.get("OBJECT_STORAGE_ACCESS_KEY", "frame_admin")
    secret = os.environ.get("OBJECT_STORAGE_SECRET_KEY", "test_password")
    bucket = f"issue42-preview-{uuid4()}"
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        region_name="us-east-1",
    )
    client.create_bucket(Bucket=bucket)
    job_id, run_token = uuid4(), uuid4()
    prefix = f"jobs/{job_id}/results/{run_token}"
    manifest_key = f"{prefix}/manifest.json"
    yolo_key = f"{prefix}/yolo/image_000001.jpg"
    payload = encoded_image("JPEG", (640, 640))
    manifest = document(job_id, run_token, payload, yolo_key)
    client.put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=manifest,
        ContentType="application/json",
    )
    client.put_object(
        Bucket=bucket,
        Key=yolo_key,
        Body=payload,
        ContentType="image/jpeg",
        Metadata={"sha256": hashlib.sha256(payload).hexdigest()},
    )
    now = datetime.now(UTC)
    job = ProcessingJob(
        id=job_id,
        status=JobStatus.SUCCEEDED,
        source_type=SourceType.IMAGE_DATASET,
        source_display="dataset",
        source_secret=None,
        source_reference={},
        processing_config={},
        created_at=now,
        started_at=now,
        completed_at=now,
        failure_code=None,
        failure_message=None,
        attempt_count=1,
        idempotency_scope="test",
        idempotency_key=str(uuid4()),
        request_fingerprint="f" * 64,
        result_reference=f"s3://{bucket}/{manifest_key}",
        result_summary={},
        run_token=None,
        lease_expires_at=None,
        version=1,
    )
    storage = S3ResultObjectStorage(
        internal_endpoint=ENDPOINT,
        external_endpoint=ENDPOINT,
        access_key=access,
        secret_key=secret,
        bucket=bucket,
        region="us-east-1",
        addressing_style="path",
    )

    class CloseSpy:
        def __init__(self, body) -> None:
            self.body = body
            self.closed = False

        def read(self, size=-1):
            return self.body.read(size)

        def close(self):
            self.closed = True
            return self.body.close()

    opened = []
    original_open_stream = storage.open_stream

    async def tracked_open_stream(key):
        result = await original_open_stream(key)
        result.body = CloseSpy(result.body)
        opened.append(result.body)
        return result

    storage.open_stream = tracked_open_stream
    try:
        with tempfile.TemporaryDirectory(prefix="issue42-preview-") as spool_root:
            service = ResultArtifactService(
                Repository(job),
                storage,
                300,
                spool_min_free_bytes=0,
                spool_root=spool_root,
            )
            stream = await service.dataset_yolo_preview(job_id, run_token, 0)
            spool_path = stream.body._path
            assert os.path.exists(spool_path)
            received = b"".join([chunk async for chunk in stream.chunks()])
            assert received == payload
            assert opened[-1].closed and not os.path.exists(spool_path)

            stream = await service.dataset_yolo_preview(job_id, run_token, 0)
            spool_path = stream.body._path
            iterator = stream.chunks(4)
            assert await anext(iterator) == payload[:4]
            await iterator.aclose()
            assert opened[-1].closed and not os.path.exists(spool_path)

            client.put_object(
                Bucket=bucket,
                Key=manifest_key,
                Body=manifest,
                ContentType="application/json",
            )
            for tampered, content_type in [
                (b"x" * len(payload), "image/jpeg"),
                (payload[:-1], "image/jpeg"),
                (payload + b"x", "image/jpeg"),
                (payload, "application/octet-stream"),
            ]:
                client.put_object(
                    Bucket=bucket,
                    Key=yolo_key,
                    Body=tampered,
                    ContentType=content_type,
                )
                with pytest.raises(ManifestInvalidError):
                    await service.dataset_yolo_preview(job_id, run_token, 0)
                assert opened[-1].closed
                assert not os.listdir(spool_root)

            decoder_rejections = [
                b"not-a-jpeg",
                encoded_image("PNG", (640, 640)),
                encoded_image("JPEG", (639, 640)),
                encoded_image("JPEG", (640, 639)),
                payload[:-32],
            ]
            for invalid_payload in decoder_rejections:
                client.put_object(
                    Bucket=bucket,
                    Key=manifest_key,
                    Body=document(job_id, run_token, invalid_payload, yolo_key),
                    ContentType="application/json",
                )
                client.put_object(
                    Bucket=bucket,
                    Key=yolo_key,
                    Body=invalid_payload,
                    ContentType="image/jpeg",
                )
                with pytest.raises(ManifestInvalidError):
                    await service.dataset_yolo_preview(job_id, run_token, 0)
                assert opened[-1].closed
                assert not os.listdir(spool_root)

            client.delete_object(Bucket=bucket, Key=yolo_key)
            with pytest.raises(ObjectNotFoundError):
                await service.dataset_yolo_preview(job_id, run_token, 0)
            assert not os.listdir(spool_root)
    finally:
        await storage.close()
        objects = client.list_objects_v2(Bucket=bucket).get("Contents", [])
        for item in objects:
            client.delete_object(Bucket=bucket, Key=item["Key"])
        client.delete_bucket(Bucket=bucket)
        client.close()


def test_real_minio_yolo_preview_integrity_and_cleanup() -> None:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            runner.run(exercise_preview())
    else:
        asyncio.run(exercise_preview())
