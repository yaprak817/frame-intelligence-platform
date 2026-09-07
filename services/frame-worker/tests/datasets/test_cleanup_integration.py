import os
from uuid import uuid4

import boto3
import pytest

from frame_worker.datasets.processor import DatasetCleanupError, _cleanup_stale_run

pytestmark = pytest.mark.skipif(
    os.environ.get("OBJECT_STORAGE_INTEGRATION") != "1",
    reason="OBJECT_STORAGE_INTEGRATION=1 is required",
)


def test_real_minio_stale_cleanup_has_exact_attempt_inventory() -> None:
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["OBJECT_STORAGE_ENDPOINT"],
        aws_access_key_id=os.environ["OBJECT_STORAGE_ACCESS_KEY"],
        aws_secret_access_key=os.environ["OBJECT_STORAGE_SECRET_KEY"],
        region_name=os.environ.get("OBJECT_STORAGE_REGION", "us-east-1"),
    )
    bucket = os.environ["OBJECT_STORAGE_BUCKET"]
    job_id, stale, active, previous, other_job = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    stale_prefix = f"jobs/{job_id}/results/{stale}/"
    keys = {
        stale_prefix + "images/partial.jpg",
        f"jobs/{job_id}/results/{active}/active.tmp",
        f"jobs/{job_id}/results/{previous}/manifest.json",
        f"jobs/{job_id}/results/{previous}/exports/accepted.zip",
        f"jobs/{other_job}/results/{stale}/foreign.tmp",
    }

    def inventory() -> set[str]:
        prefixes = (f"jobs/{job_id}/", f"jobs/{other_job}/")
        found: set[str] = set()
        for prefix in prefixes:
            response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
            found.update(item["Key"] for item in response.get("Contents", []))
        return found

    try:
        for key in keys:
            client.put_object(Bucket=bucket, Key=key, Body=b"fixture")
        assert inventory() == keys

        _cleanup_stale_run(client, bucket, job_id, stale, active, 10)
        expected = keys - {stale_prefix + "images/partial.jpg"}
        assert inventory() == expected

        final_keys = {
            stale_prefix + "images/final.jpg",
            stale_prefix + "manifest.json",
        }
        for key in final_keys:
            client.put_object(Bucket=bucket, Key=key, Body=b"fixture")
        before_reconciliation = expected | final_keys
        with pytest.raises(DatasetCleanupError, match="reconciliation"):
            _cleanup_stale_run(client, bucket, job_id, stale, active, 10)
        assert inventory() == before_reconciliation
    finally:
        for key in inventory():
            client.delete_object(Bucket=bucket, Key=key)
        assert inventory() == set()
