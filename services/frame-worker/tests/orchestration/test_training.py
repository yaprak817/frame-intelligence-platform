import hashlib
import io
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image
from sqlalchemy import create_engine, insert, select, text, update

from frame_worker.orchestration import training
from frame_worker.orchestration.training import (
    Claim,
    PermanentTrainingError,
    _cleanup_training,
    _copy_final_artifact,
    _decimal,
    _download_image,
    _training_root,
    _upload_working_artifact,
    train_annotation_model,
)


class _Body(io.BytesIO):
    pass


class _Client:
    def __init__(self, payload: bytes, content_type: str = "image/png") -> None:
        self.payload = payload
        self.content_type = content_type

    def get_object(self, **_kwargs):
        return {
            "Body": _Body(self.payload),
            "ContentLength": len(self.payload),
            "ContentType": self.content_type,
        }


def _png() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (2, 3), "white").save(output, format="PNG")
    return output.getvalue()


def _metadata(payload: bytes) -> dict:
    return {
        "source_object_key": "jobs/job/results/run/image.png",
        "source_content_type": "image/png",
        "source_size_bytes": len(payload),
        "source_sha256": hashlib.sha256(payload).hexdigest(),
        "width": 2,
        "height": 3,
    }


def test_yolo_decimal_serialization_is_locale_independent_and_stable() -> None:
    assert _decimal(Decimal("0.5")) == "0.50000000"
    assert _decimal(Decimal("0.123456789")) == "0.12345679"


def test_image_download_verifies_content_type_size_hash_and_decoder(tmp_path) -> None:
    payload = _png()
    target = tmp_path / "image.png"

    _download_image(_Client(payload), "private", _metadata(payload), target)

    assert target.read_bytes() == payload


def test_image_download_rejects_storage_metadata_change(tmp_path) -> None:
    payload = _png()

    with pytest.raises(PermanentTrainingError, match="metadata changed"):
        _download_image(
            _Client(payload, "application/octet-stream"),
            "private",
            _metadata(payload),
            tmp_path / "image.png",
        )


def test_image_download_rejects_hash_change(tmp_path) -> None:
    payload = _png()
    item = _metadata(payload)
    item["source_sha256"] = "0" * 64

    with pytest.raises(PermanentTrainingError, match="integrity failure"):
        _download_image(_Client(payload), "private", item, tmp_path / "image.png")


def test_unsupported_platform_fails_before_process_or_artifact_and_temp_mutation(
    tmp_path, monkeypatch
) -> None:
    training_id, project_id = uuid4(), uuid4()
    database_url = f"sqlite:///{tmp_path / 'platform.db'}"
    engine = create_engine(database_url)
    training.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(training.projects).values(id=project_id))
        connection.execute(
            insert(training.runs).values(
                id=training_id,
                project_id=project_id,
                status="PENDING",
                attempt_generation=0,
            )
        )
    engine.dispose()
    settings = SimpleNamespace(
        database_url=database_url,
        object_storage_endpoint="http://127.0.0.1:9000",
        object_storage_access_key="private",
        object_storage_secret_key="private",
        object_storage_region="us-east-1",
        object_storage_addressing_style="path",
        object_storage_bucket="private",
        processing_temp_root=tmp_path,
        training_lease_seconds=300,
    )
    monkeypatch.setattr(training, "_training_platform_supported", lambda: False)
    monkeypatch.setattr(
        training.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("trainer was spawned"),
    )
    monkeypatch.setattr(
        training,
        "_upload_working_artifact",
        lambda *_args, **_kwargs: pytest.fail("artifact was uploaded"),
    )
    monkeypatch.setattr(
        training,
        "_snapshot",
        lambda *_args, **_kwargs: pytest.fail("temp snapshot was created"),
    )
    with pytest.raises(PermanentTrainingError, match="Unsupported training platform"):
        train_annotation_model(training_id, settings)
    assert not (tmp_path / "annotation-training").exists()
    with create_engine(database_url).connect() as connection:
        row = connection.execute(
            select(training.runs.c.status, training.runs.c.failure_code).where(
                training.runs.c.id == training_id
            )
        ).one()
    assert row == ("FAILED", "TRAINING_UNSUPPORTED_PLATFORM")


def test_working_upload_head_failure_deletes_only_current_generation(tmp_path) -> None:
    class Client:
        deleted: list[str] = []

        def upload_fileobj(self, *_args, **_kwargs):
            return None

        def head_object(self, **_kwargs):
            raise RuntimeError("head unavailable")

        def delete_object(self, *, Bucket, Key):
            assert Bucket == "private"
            self.deleted.append(Key)

    token = uuid4()
    claim = Claim(
        {"id": uuid4(), "project_id": uuid4()},
        token,
        2,
        f"annotations/project/trainings/run/working/2-{token}/",
    )
    artifact = tmp_path / "snapshot.zip"
    artifact.write_bytes(b"snapshot")
    tracked: list[str] = []
    client = Client()
    with pytest.raises(RuntimeError, match="head unavailable"):
        _upload_working_artifact(
            client,
            "private",
            claim,
            artifact,
            "snapshot.zip",
            "application/zip",
            8,
            hashlib.sha256(b"snapshot").hexdigest(),
            tracked,
        )
    expected = claim.prefix + "snapshot.zip"
    assert tracked == [expected]
    assert client.deleted == [expected]


def test_final_copy_head_failure_tracks_and_cleans_only_copied_key(tmp_path) -> None:
    class Client:
        def __init__(self) -> None:
            self.copied: list[str] = []
            self.deleted: list[str] = []

        def copy_object(self, *, Bucket, Key, **_kwargs):
            assert Bucket == "private"
            self.copied.append(Key)

        def head_object(self, **_kwargs):
            raise RuntimeError("head unavailable")

        def delete_object(self, *, Bucket, Key):
            assert Bucket == "private"
            self.deleted.append(Key)

    training_id, project_id, token = uuid4(), uuid4(), uuid4()
    claim = Claim(
        {"id": training_id, "project_id": project_id},
        token,
        2,
        f"annotations/{project_id}/trainings/{training_id}/working/2-{token}/",
    )
    key = f"annotations/{project_id}/trainings/{training_id}/model.pt"
    other_key = f"annotations/{project_id}/trainings/{uuid4()}/model.pt"
    uploaded: list[str] = []
    client = Client()
    with pytest.raises(RuntimeError, match="head unavailable"):
        _copy_final_artifact(
            client,
            "private",
            claim,
            "model.pt",
            key,
            "application/octet-stream",
            5,
            "a" * 64,
            uploaded,
        )
    assert client.copied == uploaded == [key]
    assert other_key not in uploaded
    _cleanup_training(
        None, client, "private", claim, uploaded, [], _training_root(tmp_path, claim)
    )
    assert client.deleted == [key]


@pytest.mark.parametrize("copy_created_object", [True, False])
def test_ambiguous_final_copy_tracks_key_and_missing_delete_is_idempotent(
    tmp_path, copy_created_object
) -> None:
    training_id, project_id, token = uuid4(), uuid4(), uuid4()
    claim = Claim(
        {"id": training_id, "project_id": project_id},
        token,
        1,
        f"annotations/{project_id}/trainings/{training_id}/working/1-{token}/",
    )
    key = f"annotations/{project_id}/trainings/{training_id}/model.pt"
    other_key = f"annotations/{project_id}/trainings/{uuid4()}/model.pt"

    class Client:
        def __init__(self) -> None:
            self.objects = {other_key: b"other generation"}
            self.deleted: list[str] = []

        def copy_object(self, *, Key, **_kwargs):
            if copy_created_object:
                self.objects[Key] = b"copied model"
            raise RuntimeError("copy response lost")

        def delete_object(self, *, Key, **_kwargs):
            self.deleted.append(Key)
            self.objects.pop(Key, None)

    uploaded: list[str] = []
    client = Client()
    with pytest.raises(RuntimeError, match="copy response lost"):
        _copy_final_artifact(
            client,
            "private",
            claim,
            "model.pt",
            key,
            "application/octet-stream",
            12,
            "a" * 64,
            uploaded,
        )
    assert uploaded == [key]
    _cleanup_training(
        None, client, "private", claim, uploaded, [], _training_root(tmp_path, claim)
    )
    assert client.deleted == [key]
    assert key not in client.objects
    assert client.objects == {other_key: b"other generation"}


def test_training_roots_are_canonical_and_generation_specific(
    tmp_path, monkeypatch
) -> None:
    training_id, project_id = uuid4(), uuid4()
    old_token, new_token = uuid4(), uuid4()
    old = Claim({"id": training_id, "project_id": project_id}, old_token, 1, "old/")
    new = Claim({"id": training_id, "project_id": project_id}, new_token, 2, "new/")
    old_root, new_root = _training_root(tmp_path, old), _training_root(tmp_path, new)
    assert old_root != new_root
    assert old_root.parent.parent != new_root.parent.parent
    assert old_root.parent.parent.name == f"1-{old_token}"
    assert new_root.parent.parent.name == f"2-{new_token}"
    assert old_root.name == new_root.name == str(training_id)
    assert old_root.parent.parent / "annotation-training" / str(training_id) == old_root
    assert new_root.parent.parent / "annotation-training" / str(training_id) == new_root
    new_root.mkdir(parents=True)
    old_root.mkdir(parents=True)
    (new_root / "sentinel").write_text("new", encoding="ascii")
    (old_root / "sentinel").write_text("old", encoding="ascii")

    class Client:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_object(self, *, Bucket, Key):
            assert Bucket == "private"
            self.deleted.append(Key)

    client = Client()
    old_process, new_process = object(), object()
    terminated: list[object] = []
    monkeypatch.setattr(training, "_terminate", terminated.append)
    _cleanup_training(
        old_process,
        client,
        "private",
        old,
        [f"annotations/{project_id}/trainings/{training_id}/model.pt"],
        ["old/snapshot.zip", "new/snapshot.zip"],
        old_root,
    )
    assert not old_root.exists()
    assert (new_root / "sentinel").read_text(encoding="ascii") == "new"
    assert client.deleted == [
        f"annotations/{project_id}/trainings/{training_id}/model.pt",
        "old/snapshot.zip",
    ]
    assert terminated == [old_process]
    assert new_process not in terminated


def test_stale_generation_final_cleanup_does_not_delete_new_generation_key(
    tmp_path,
) -> None:
    class Engine:
        def begin(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return None

        def scalar(self, _statement):
            return 2

    class Client:
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def delete_object(self, *, Bucket, Key):
            assert Bucket == "private"
            self.deleted.append(Key)

    training_id, project_id, token = uuid4(), uuid4(), uuid4()
    old = Claim(
        {"id": training_id, "project_id": project_id},
        token,
        1,
        f"annotations/{project_id}/trainings/{training_id}/working/1-{token}/",
    )
    root = _training_root(tmp_path, old)
    root.mkdir(parents=True)
    (root / "old").write_text("old", encoding="ascii")
    stable_key = f"annotations/{project_id}/trainings/{training_id}/model.pt"
    client = Client()
    _cleanup_training(
        None,
        client,
        "private",
        old,
        [stable_key],
        [old.prefix + "snapshot.zip"],
        root,
        Engine(),
    )
    assert client.deleted == [old.prefix + "snapshot.zip"]
    assert not root.parent.parent.exists()


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL required"
)
def test_failed_db_finalization_keeps_expired_owner_unclaimable(
    tmp_path, monkeypatch
) -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    schema = f"t42fence_{uuid4().hex}"
    base_engine = create_engine(database_url, pool_pre_ping=True)
    owner_engine = base_engine.execution_options(schema_translate_map={None: schema})
    with base_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        training.metadata.create_all(owner_engine)
        training_id, project_id = uuid4(), uuid4()
        with owner_engine.begin() as connection:
            connection.execute(insert(training.projects).values(id=project_id))
            connection.execute(
                insert(training.runs).values(
                    id=training_id,
                    project_id=project_id,
                    status="PENDING",
                    attempt_generation=0,
                )
            )
        settings = SimpleNamespace(training_lease_seconds=300)
        claim = training._claim(owner_engine, training_id, settings)
        assert claim is not None and claim.generation == 1
        final_key = f"annotations/{project_id}/trainings/{training_id}/model.pt"
        working_key = claim.prefix + "model.pt"

        class Client:
            def __init__(self) -> None:
                self.objects = {final_key: b"final", working_key: b"working"}

            def delete_object(self, **_kwargs):
                pytest.fail("artifact cleanup ran without a DB ownership fence")

        class BrokenEngine:
            def begin(self):
                raise RuntimeError("injected DB lock failure")

        client = Client()
        root = _training_root(tmp_path, claim)
        root.mkdir(parents=True)
        marker = root / "trainer-file"
        marker.write_text("keep", encoding="ascii")
        with pytest.raises(
            training.TrainingError, match="Training finalization dependency failure"
        ) as error:
            training._finalize_failed_attempt(
                BrokenEngine(),
                client,
                "private",
                claim,
                None,
                [final_key],
                [working_key],
                root,
                None,
            )
        assert error.value.__cause__ is None
        assert marker.read_text(encoding="ascii") == "keep"
        with owner_engine.begin() as connection:
            row = connection.execute(
                select(
                    training.runs.c.status,
                    training.runs.c.lease_token,
                    training.runs.c.attempt_generation,
                ).where(training.runs.c.id == training_id)
            ).one()
            assert row == ("RUNNING", claim.token, 1)
            connection.execute(
                update(training.runs)
                .where(training.runs.c.id == training_id)
                .values(lease_expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
        second_engine = create_engine(
            database_url, pool_pre_ping=True
        ).execution_options(schema_translate_map={None: schema})
        try:
            assert training._claim(second_engine, training_id, settings) is None
            assert training._claim(second_engine, training_id, settings) is None
            with second_engine.connect() as connection:
                persisted = connection.execute(
                    select(
                        training.runs.c.status,
                        training.runs.c.lease_token,
                        training.runs.c.attempt_generation,
                    ).where(training.runs.c.id == training_id)
                ).one()
            assert persisted == ("RUNNING", claim.token, 1)
            monkeypatch.setattr(
                training, "create_engine", lambda *_args, **_kwargs: second_engine
            )
            monkeypatch.setattr(
                training.subprocess,
                "Popen",
                lambda *_args, **_kwargs: pytest.fail("new trainer was spawned"),
            )
            executor_settings = SimpleNamespace(
                database_url=database_url,
                training_lease_seconds=300,
                object_storage_endpoint="http://127.0.0.1:9000",
                object_storage_access_key="private",
                object_storage_secret_key="private",
                object_storage_region="us-east-1",
                object_storage_addressing_style="path",
            )
            assert train_annotation_model(training_id, executor_settings) is False
            assert client.objects == {final_key: b"final", working_key: b"working"}
            assert marker.read_text(encoding="ascii") == "keep"
        finally:
            second_engine.dispose()
    finally:
        with base_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        base_engine.dispose()


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"), reason="PostgreSQL required"
)
def test_head_failure_cleanup_blocks_new_claim_with_two_postgres_sessions(
    tmp_path,
) -> None:
    database_url = os.environ["TEST_DATABASE_URL"]
    schema = f"t42final_{uuid4().hex}"
    base_engine = create_engine(database_url, pool_pre_ping=True)
    test_engine = base_engine.execution_options(schema_translate_map={None: schema})
    with base_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        training.metadata.create_all(test_engine)
        training_id, project_id, token = uuid4(), uuid4(), uuid4()
        claim = Claim(
            {"id": training_id, "project_id": project_id},
            token,
            1,
            f"annotations/{project_id}/trainings/{training_id}/working/1-{token}/",
        )
        with test_engine.begin() as connection:
            connection.execute(insert(training.projects).values(id=project_id))
            connection.execute(
                insert(training.runs).values(
                    id=training_id,
                    project_id=project_id,
                    status="RUNNING",
                    lease_token=token,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    attempt_generation=1,
                    working_prefix=claim.prefix,
                )
            )
        key = f"annotations/{project_id}/trainings/{training_id}/model.pt"
        entered, release = threading.Event(), threading.Event()

        class Client:
            def __init__(self) -> None:
                self.objects = {}
                self.deleted: list[str] = []

            def copy_object(self, *, Key, **_kwargs):
                self.objects[Key] = b"old generation"

            def head_object(self, **_kwargs):
                raise RuntimeError("HEAD failed")

            def delete_object(self, *, Key, **_kwargs):
                if Key == key and not entered.is_set():
                    entered.set()
                    if not release.wait(timeout=10):
                        raise RuntimeError("cleanup barrier timed out")
                self.deleted.append(Key)
                self.objects.pop(Key, None)

        client = Client()
        uploaded: list[str] = []
        with pytest.raises(RuntimeError, match="HEAD failed"):
            _copy_final_artifact(
                client,
                "private",
                claim,
                "model.pt",
                key,
                "application/octet-stream",
                14,
                "a" * 64,
                uploaded,
            )
        assert uploaded == [key]
        old_root = _training_root(tmp_path, claim)
        old_root.mkdir(parents=True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            cleanup = pool.submit(
                training._finalize_failed_attempt,
                test_engine,
                client,
                "private",
                claim,
                None,
                uploaded,
                [],
                old_root,
                None,
            )
            assert entered.wait(timeout=5)
            second = pool.submit(
                training._claim,
                test_engine,
                training_id,
                SimpleNamespace(training_lease_seconds=300),
            )
            time.sleep(0.2)
            assert not second.done(), "new claim passed the old cleanup barrier"
            release.set()
            cleanup.result(timeout=10)
            new_claim = second.result(timeout=10)
        assert new_claim is not None and new_claim.generation == 2
        assert key in client.deleted and key not in client.objects
        assert not old_root.parent.parent.exists()
        client.objects[key] = b"new generation"
        _cleanup_training(
            None, client, "private", claim, [key], [], old_root, test_engine
        )
        assert client.objects[key] == b"new generation"
        with test_engine.connect() as connection:
            row = connection.execute(
                select(
                    training.runs.c.attempt_generation, training.runs.c.lease_token
                ).where(training.runs.c.id == training_id)
            ).one()
        assert row == (2, new_claim.token)
    finally:
        with base_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        base_engine.dispose()
