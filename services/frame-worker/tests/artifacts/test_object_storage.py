import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from frame_worker.artifacts.manifest import manifest_bytes
from frame_worker.artifacts.models import FrameManifestEntry
from frame_worker.artifacts.object_storage import (
    ArtifactStorageError,
    ObjectStorageArtifactStore,
    PersistedArtifacts,
    artifact_prefix,
)
from frame_worker.processing.pipeline import ProcessingSummary, SelectedFrame


class RecordingClient:
    def __init__(self, fail_upload: int | None = None) -> None:
        self.fail_upload = fail_upload
        self.calls = []
        self.uploads = 0

    def upload_fileobj(self, body, bucket, key, ExtraArgs):
        self.uploads += 1
        if self.fail_upload == self.uploads:
            raise ConnectionError("secret storage endpoint")
        self.calls.append(("upload", bucket, key, ExtraArgs, body.read()))

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))


def _summary(directory: Path, count: int = 2) -> ProcessingSummary:
    directory.mkdir()
    frames = []
    for index in range(count):
        filename = f"frame_{index:06d}_{(index + 1) * 1000}ms_640x480.jpg"
        path = directory / filename
        path.write_bytes(f"jpeg-{index}".encode())
        frames.append(
            SelectedFrame(index, (index + 1) * 1000, 640, 480, filename, path)
        )
    return ProcessingSummary(
        30,
        300,
        10.0,
        100,
        20,
        count,
        3,
        1.5,
        directory,
        tuple(frames),
    )


def _document(count: int = 2) -> dict[str, int | float]:
    return {
        "frames_saved": count,
        "candidates": 100,
        "shortlisted": 20,
        "duplicates_removed": 3,
        "processing_seconds": 1.5,
        "duration_seconds": 10.0,
    }


def test_artifact_prefix_is_deterministic_and_run_scoped() -> None:
    job_id = uuid4()
    run_token = uuid4()
    assert artifact_prefix(job_id, run_token) == (f"jobs/{job_id}/results/{run_token}")


def test_manifest_v1_is_canonical_and_deterministic() -> None:
    job_id = uuid4()
    run_token = uuid4()
    entry = FrameManifestEntry(
        0,
        "frame_000000_1000ms_640x480.jpg",
        f"jobs/{job_id}/results/{run_token}/frames/frame_000000_1000ms_640x480.jpg",
        "image/jpeg",
        4,
        hashlib.sha256(b"jpeg").hexdigest(),
        1000,
        640,
        480,
    )
    from datetime import UTC, datetime

    created_at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    first = manifest_bytes(
        job_id=job_id,
        run_token=run_token,
        created_at=created_at,
        summary=_document(1),
        frames=(entry,),
    )
    second = manifest_bytes(
        job_id=job_id,
        run_token=run_token,
        created_at=created_at,
        summary=_document(1),
        frames=(entry,),
    )
    assert first == second
    document = json.loads(first)
    assert document["schema_version"] == 1
    assert document["frames"] == [entry.as_dict()]
    assert document["created_at"] == "2026-01-02T03:04:05Z"


def test_frames_are_ordered_hashed_and_uploaded_before_manifest(tmp_path) -> None:
    client = RecordingClient()
    store = ObjectStorageArtifactStore(client, "results")
    job_id = uuid4()
    run_token = uuid4()
    summary = _summary(tmp_path / "frames")

    persisted = store.persist(job_id, run_token, summary, _document())

    assert [call[0] for call in client.calls] == ["upload", "upload", "put"]
    assert [call[2] for call in client.calls[:2]] == [
        f"{persisted.prefix}/frames/{summary.frames[0].filename}",
        f"{persisted.prefix}/frames/{summary.frames[1].filename}",
    ]
    assert all(call[3] == {"ContentType": "image/jpeg"} for call in client.calls[:2])
    manifest_call = client.calls[-1][1]
    assert manifest_call["ContentType"] == "application/json"
    manifest = json.loads(manifest_call["Body"])
    assert manifest["frames"][0]["size_bytes"] == len(b"jpeg-0")
    assert manifest["frames"][0]["sha256"] == hashlib.sha256(b"jpeg-0").hexdigest()
    assert persisted.result_reference == (
        f"s3://results/{persisted.prefix}/manifest.json"
    )
    assert str(tmp_path) not in persisted.result_reference


def test_partial_failure_cleans_only_uploaded_current_run_keys(tmp_path) -> None:
    client = RecordingClient(fail_upload=2)
    store = ObjectStorageArtifactStore(client, "results")
    job_id = uuid4()
    run_token = uuid4()
    prefix = artifact_prefix(job_id, run_token)

    with pytest.raises(ArtifactStorageError, match="unavailable"):
        store.persist(job_id, run_token, _summary(tmp_path / "frames"), _document())

    deletes = [call[1]["Key"] for call in client.calls if call[0] == "delete"]
    assert deletes == [f"{prefix}/frames/frame_000000_1000ms_640x480.jpg"]


def test_path_traversal_and_count_mismatch_are_rejected(tmp_path) -> None:
    store = ObjectStorageArtifactStore(RecordingClient(), "results")
    summary = _summary(tmp_path / "frames", 1)
    escaped = tmp_path / summary.frames[0].filename
    escaped.write_bytes(b"escape")
    bad_frame = SelectedFrame(0, 1000, 640, 480, escaped.name, escaped)
    bad_summary = ProcessingSummary(
        summary.source_fps,
        summary.total_frames,
        summary.duration_seconds,
        summary.candidate_frames,
        summary.shortlisted_frames,
        1,
        summary.duplicate_frames,
        summary.processing_seconds,
        summary.output_directory,
        (bad_frame,),
    )
    with pytest.raises(ValueError, match="escaped"):
        store.persist(uuid4(), uuid4(), bad_summary, _document(1))

    mismatch = ProcessingSummary(
        summary.source_fps,
        summary.total_frames,
        summary.duration_seconds,
        summary.candidate_frames,
        summary.shortlisted_frames,
        2,
        summary.duplicate_frames,
        summary.processing_seconds,
        summary.output_directory,
        summary.frames,
    )
    with pytest.raises(ValueError, match="count"):
        store.persist(uuid4(), uuid4(), mismatch, _document(2))


def test_symlink_is_rejected(tmp_path, monkeypatch) -> None:
    store = ObjectStorageArtifactStore(RecordingClient(), "results")
    output = tmp_path / "frames"
    output.mkdir()
    link = output / "frame_000000_1000ms_640x480.jpg"
    link.write_bytes(b"target")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == link or original_is_symlink(path),
    )
    frame = SelectedFrame(0, 1000, 640, 480, link.name, link)
    symlink_summary = ProcessingSummary(
        30,
        300,
        10.0,
        100,
        20,
        1,
        3,
        1.5,
        output,
        (frame,),
    )
    with pytest.raises(ValueError, match="metadata|regular file"):
        store.persist(uuid4(), uuid4(), symlink_summary, _document(1))


def test_cleanup_rejects_keys_outside_current_run_prefix() -> None:
    client = RecordingClient()
    store = ObjectStorageArtifactStore(client, "results")
    prefix = artifact_prefix(uuid4(), uuid4())
    store.cleanup(
        PersistedArtifacts(
            "s3://results/safe/manifest.json",
            prefix,
            (f"{prefix}/manifest.json", "jobs/other/results/run/manifest.json"),
        )
    )
    deletes = [call[1]["Key"] for call in client.calls if call[0] == "delete"]
    assert deletes == [f"{prefix}/manifest.json"]
