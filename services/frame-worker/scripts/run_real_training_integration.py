import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError
from PIL import Image
from sqlalchemy import create_engine, text

from frame_worker.orchestration.config import WorkerSettings
from frame_worker.orchestration.training import (
    _claim,
    _copy_final_artifact,
    _finalize_failed_attempt,
    _training_root,
    train_annotation_model,
)

settings = WorkerSettings.from_env()
engine = create_engine(settings.database_url)
s3 = boto3.client(
    "s3",
    endpoint_url=settings.object_storage_endpoint,
    aws_access_key_id=settings.object_storage_access_key,
    aws_secret_access_key=settings.object_storage_secret_key,
    region_name=settings.object_storage_region,
)
try:
    s3.create_bucket(Bucket=settings.object_storage_bucket)
except ClientError as error:
    if error.response.get("Error", {}).get("Code") != "BucketAlreadyOwnedByYou":
        raise
job_id, project_id, run_token, training_id = uuid4(), uuid4(), uuid4(), uuid4()
class_id, box_id = uuid4(), uuid4()
now = datetime.now(UTC)
prefix = f"jobs/{job_id}/results/{run_token}/"

with engine.begin() as connection:
    connection.execute(
        text(
            "INSERT INTO processing_jobs "
            "(id,status,source_type,source_display,source_reference,processing_config,"
            "created_at,started_at,completed_at,attempt_count,idempotency_scope,"
            "idempotency_key,request_fingerprint,result_reference,result_summary,"
            "version) VALUES (:id,'SUCCEEDED','UPLOAD','training-integration',"
            "CAST('{}' AS json),"
            "CAST('{}' AS json),:now,:now,:now,1,'issue42',:key,:fingerprint,:result,"
            "CAST('{}' AS json),1)"
        ),
        {
            "id": job_id,
            "now": now,
            "key": str(uuid4()),
            "fingerprint": "f" * 64,
            "result": f"s3://{settings.object_storage_bucket}/{prefix}manifest.json",
        },
    )
    connection.execute(
        text(
            "INSERT INTO annotation_projects "
            "(id,job_id,result_run_token,revision,created_at,updated_at) "
            "VALUES (:id,:job,:token,1,:now,:now)"
        ),
        {"id": project_id, "job": job_id, "token": run_token, "now": now},
    )
    connection.execute(
        text(
            "INSERT INTO annotation_training_runs "
            "(id,project_id,snapshot_version,source_revision,status,selected_image_count,"
            "selected_class_count,selected_box_count,train_image_count,validation_image_count,"
            "config,config_hash,idempotency_key,request_fingerprint,created_at,"
            "progress_completed,progress_total,attempt_generation) "
            "VALUES (:id,:project,1,1,'PENDING',50,1,1,40,10,CAST(:config AS json),"
            ":config_hash,:key,:fingerprint,:now,0,1,0)"
        ),
        {
            "id": training_id,
            "project": project_id,
            "config": json.dumps(
                {
                    "max_snapshot_images": 50,
                    "epochs": 1,
                    "batch_size": 1,
                    "image_size": 640,
                }
            ),
            "config_hash": "a" * 64,
            "key": "issue42-training",
            "fingerprint": "b" * 64,
            "now": now,
        },
    )
    connection.execute(
        text(
            "INSERT INTO annotation_training_snapshot_classes "
            "(training_id,yolo_index,project_id,class_id,name) "
            "VALUES (:training,0,:project,:class_id,:name)"
        ),
        {
            "training": training_id,
            "project": project_id,
            "class_id": class_id,
            "name": "vehicle: [safe] ../../canary",
        },
    )

for index in range(50):
    output = io.BytesIO()
    image = Image.new("RGB", (64, 64), (index * 3 % 255, 80, 120))
    image.save(output, format="JPEG", quality=85)
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    key = prefix + f"frames/frame_{index:06d}.jpg"
    s3.put_object(
        Bucket=settings.object_storage_bucket,
        Key=key,
        Body=payload,
        ContentType="image/jpeg",
        Metadata={"sha256": digest},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO annotation_training_snapshot_images "
                "(training_id,image_index,project_id,filename,source_object_key,"
                "source_size_bytes,source_content_type,source_sha256,width,height,"
                "split) "
                "VALUES (:training,:index,:project,:filename,:object_key,:size,"
                "'image/jpeg',:sha,64,64,:split)"
            ),
            {
                "training": training_id,
                "index": index,
                "project": project_id,
                "filename": f"frame_{index:06d}.jpg",
                "object_key": key,
                "size": len(payload),
                "sha": digest,
                "split": "train" if index < 40 else "val",
            },
        )

with engine.begin() as connection:
    connection.execute(
        text(
            "INSERT INTO annotation_training_snapshot_boxes "
            "(training_id,box_id,project_id,image_index,yolo_index,x_center,y_center,"
            "width,height) "
            "VALUES (:training,:box,:project,0,0,0.5,0.5,0.5,0.5)"
        ),
        {"training": training_id, "box": box_id, "project": project_id},
    )

assert train_annotation_model(training_id, settings) is True
with engine.connect() as connection:
    row = (
        connection.execute(
            text("SELECT * FROM annotation_training_runs WHERE id=:id"),
            {"id": training_id},
        )
        .mappings()
        .one()
    )
assert row["status"] == "SUCCEEDED"
assert row["progress_completed"] == row["progress_total"] == 1
assert row["model_version"] == 1
assert row["lease_token"] is None and row["working_prefix"] is None

snapshot_key = f"annotations/{project_id}/trainings/{training_id}/snapshot.zip"
model_key = f"annotations/{project_id}/trainings/{training_id}/model.pt"
snapshot = s3.get_object(Bucket=settings.object_storage_bucket, Key=snapshot_key)
snapshot_payload = snapshot["Body"].read()
model = s3.get_object(Bucket=settings.object_storage_bucket, Key=model_key)
model_payload = model["Body"].read()
assert snapshot["ContentType"] == "application/zip"
assert model["ContentType"] == "application/octet-stream"
assert len(snapshot_payload) == row["snapshot_artifact_size_bytes"]
assert hashlib.sha256(snapshot_payload).hexdigest() == row["snapshot_artifact_sha256"]
assert len(model_payload) == row["model_artifact_size_bytes"]
assert hashlib.sha256(model_payload).hexdigest() == row["model_artifact_sha256"]
assert len(model_payload) <= 256 * 1024 * 1024
with zipfile.ZipFile(io.BytesIO(snapshot_payload)) as archive:
    names = set(archive.namelist())
    assert {"data.yaml", "snapshot.json", "labels/train/image_000001.txt"} <= names
    assert archive.read("labels/train/image_000001.txt") == b""
    assert b"vehicle: [safe] ../../canary" in archive.read("data.yaml")
    assert len([name for name in names if name.startswith("images/")]) == 50
    assert len([name for name in names if name.startswith("labels/")]) == 50
working = s3.list_objects_v2(
    Bucket=settings.object_storage_bucket,
    Prefix=f"annotations/{project_id}/trainings/{training_id}/working/",
)
assert working.get("KeyCount", 0) == 0
assert train_annotation_model(training_id, settings) is False
with create_engine(settings.database_url).connect() as refreshed:
    persisted = refreshed.execute(
        text("SELECT status, model_version FROM annotation_training_runs WHERE id=:id"),
        {"id": training_id},
    ).one()
assert persisted == ("SUCCEEDED", 1)

# Exercise an ambiguous final-copy/HEAD failure against the same real
# PostgreSQL and MinIO services. The failed generation must leave no stable
# or working object and must never be claimable again.
failed_id = uuid4()
with engine.begin() as connection:
    connection.execute(
        text(
            "INSERT INTO annotation_training_runs "
            "(id,project_id,snapshot_version,source_revision,status,selected_image_count,"
            "selected_class_count,selected_box_count,train_image_count,validation_image_count,"
            "config,config_hash,idempotency_key,request_fingerprint,created_at,"
            "progress_completed,progress_total,attempt_generation) "
            "VALUES (:id,:project,2,2,'PENDING',50,1,1,40,10,CAST('{}' AS json),"
            ":config_hash,:key,:fingerprint,:now,0,1,0)"
        ),
        {
            "id": failed_id,
            "project": project_id,
            "config_hash": "c" * 64,
            "key": str(uuid4()),
            "fingerprint": "d" * 64,
            "now": datetime.now(UTC),
        },
    )
claim = _claim(engine, failed_id, settings)
assert claim is not None and claim.generation == 1
failed_prefix = f"annotations/{project_id}/trainings/{failed_id}/"
working_key = claim.prefix + "model.pt"
final_key = failed_prefix + "model.pt"
payload = b"failed-generation-check"
digest = hashlib.sha256(payload).hexdigest()
s3.put_object(
    Bucket=settings.object_storage_bucket,
    Key=working_key,
    Body=payload,
    ContentType="application/octet-stream",
    Metadata={"sha256": digest},
)


class HeadFailureClient:
    def copy_object(self, **kwargs):
        return s3.copy_object(**kwargs)

    def head_object(self, **kwargs):
        if kwargs["Key"] == final_key:
            raise RuntimeError("injected HEAD response failure")
        return s3.head_object(**kwargs)

    def delete_object(self, **kwargs):
        return s3.delete_object(**kwargs)


failed_client = HeadFailureClient()
uploaded = []
try:
    _copy_final_artifact(
        failed_client,
        settings.object_storage_bucket,
        claim,
        "model.pt",
        final_key,
        "application/octet-stream",
        len(payload),
        digest,
        uploaded,
    )
except RuntimeError as error:
    assert str(error) == "injected HEAD response failure"
else:
    raise AssertionError("HEAD failure was not injected")
assert uploaded == [final_key]
assert s3.head_object(Bucket=settings.object_storage_bucket, Key=final_key)[
    "ContentLength"
] == len(payload)
failed_root = _training_root(
    settings.processing_temp_root or Path("/tmp/frame-intelligence"), claim
)
failed_root.mkdir(parents=True, exist_ok=False)
_finalize_failed_attempt(
    engine,
    failed_client,
    settings.object_storage_bucket,
    claim,
    None,
    uploaded,
    [working_key],
    failed_root,
    "TRAINING_FAILED",
)
with engine.connect() as connection:
    failed_row = connection.execute(
        text(
            "SELECT status,failure_code,lease_token,attempt_generation "
            "FROM annotation_training_runs WHERE id=:id"
        ),
        {"id": failed_id},
    ).one()
assert failed_row == ("FAILED", "TRAINING_FAILED", None, 1)
assert _claim(engine, failed_id, settings) is None
assert not failed_root.parent.parent.exists()
assert (
    s3.list_objects_v2(Bucket=settings.object_storage_bucket, Prefix=failed_prefix).get(
        "KeyCount", 0
    )
    == 0
)
print(
    "REAL_TRAINING_PASS "
    f"model_bytes={len(model_payload)} snapshot_bytes={len(snapshot_payload)}"
)
