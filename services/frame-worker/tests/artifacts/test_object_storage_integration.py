import hashlib
import json
import os
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config

from frame_worker.artifacts.object_storage import ObjectStorageArtifactStore
from frame_worker.processing.pipeline import ProcessingSummary, SelectedFrame

pytestmark = pytest.mark.skipif(
    os.environ.get("OBJECT_STORAGE_INTEGRATION") != "1",
    reason="OBJECT_STORAGE_INTEGRATION=1 is required",
)


def test_real_minio_persists_manifest_last_and_cleans_up(tmp_path) -> None:
    bucket = os.environ["OBJECT_STORAGE_BUCKET"]
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["OBJECT_STORAGE_ENDPOINT"],
        aws_access_key_id=os.environ["OBJECT_STORAGE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["OBJECT_STORAGE_SECRET_KEY"],
        region_name=os.environ.get("OBJECT_STORAGE_REGION", "us-east-1"),
        config=Config(s3={"addressing_style": "path"}),
    )
    try:
        client.create_bucket(Bucket=bucket)
    except client.exceptions.BucketAlreadyOwnedByYou:
        pass
    output = tmp_path / "frames"
    output.mkdir()
    frames = []
    for index, content in enumerate((b"jpeg-zero", b"jpeg-one")):
        filename = f"frame_{index:06d}_{(index + 1) * 1000}ms_640x480.jpg"
        path = output / filename
        path.write_bytes(content)
        frames.append(
            SelectedFrame(index, (index + 1) * 1000, 640, 480, filename, path)
        )
    summary = ProcessingSummary(
        30, 300, 10.0, 100, 20, 2, 3, 1.5, output, tuple(frames)
    )
    summary_document = {
        "frames_saved": 2,
        "candidates": 100,
        "shortlisted": 20,
        "duplicates_removed": 3,
        "processing_seconds": 1.5,
        "duration_seconds": 10.0,
    }
    store = ObjectStorageArtifactStore(client, bucket)
    persisted = store.persist(uuid4(), uuid4(), summary, summary_document)
    try:
        assert persisted.object_keys[-1].endswith("/manifest.json")
        manifest_response = client.get_object(
            Bucket=bucket, Key=persisted.object_keys[-1]
        )
        assert manifest_response["ContentType"] == "application/json"
        manifest = json.loads(manifest_response["Body"].read())
        assert manifest["schema_version"] == 1
        assert len(manifest["frames"]) == summary_document["frames_saved"]
        for entry, expected in zip(
            manifest["frames"], (b"jpeg-zero", b"jpeg-one"), strict=True
        ):
            response = client.get_object(Bucket=bucket, Key=entry["object_key"])
            body = response["Body"].read()
            assert response["ContentType"] == "image/jpeg"
            assert body == expected
            assert entry["size_bytes"] == len(expected)
            assert entry["sha256"] == hashlib.sha256(expected).hexdigest()
        assert persisted.result_reference == (
            f"s3://{bucket}/{persisted.object_keys[-1]}"
        )
    finally:
        store.cleanup(persisted)
    for key in persisted.object_keys:
        with pytest.raises(client.exceptions.ClientError):
            client.head_object(Bucket=bucket, Key=key)
