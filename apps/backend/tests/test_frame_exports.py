import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.models.frame_export import FrameExport, FrameExportOutbox
from app.schemas.artifacts import StoredManifestV1
from app.schemas.exports import CreateFrameExportRequest, ExportStatus
from app.security.rate_limit import protected_group
from app.services.frame_exports import FrameExportService, selection_hash
from app.services.result_artifacts import _download_filename


def test_export_selection_is_strict() -> None:
    assert CreateFrameExportRequest(mode="all").frame_indices is None
    assert CreateFrameExportRequest(
        mode="selected", frame_indices=[0, 2]
    ).frame_indices == [0, 2]
    for payload in (
        {"mode": "selected", "frame_indices": []},
        {"mode": "selected", "frame_indices": [1, 1]},
        {"mode": "selected", "frame_indices": [-1]},
        {"mode": "all", "frame_indices": [0]},
    ):
        with pytest.raises(ValidationError):
            CreateFrameExportRequest.model_validate(payload)


def test_export_identity_is_canonical_and_scoped_by_run_and_mode() -> None:
    assert selection_hash(sorted([2, 0])) == selection_hash([0, 2])
    constraint = next(
        item
        for item in FrameExport.__table__.constraints
        if item.name == "uq_frame_exports_selection"
    )
    assert [column.name for column in constraint.columns] == [
        "job_id",
        "result_run_token",
        "mode",
        "selection_hash",
    ]


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value

    def scalar_one(self):
        assert self.value is not None
        return self.value


class _MemorySession:
    def __init__(self) -> None:
        self.exports: list[FrameExport] = []
        self.outboxes: list[FrameExportOutbox] = []
        self.pending: list[object] = []

    async def execute(self, statement):
        entity = statement.column_descriptions[0].get("entity")
        parameters = statement.compile().params
        if entity is FrameExport:
            expected = {
                key.rsplit("_", 1)[0]: value for key, value in parameters.items()
            }
            match = next(
                (
                    item
                    for item in self.exports
                    if all(
                        getattr(item, key) == value for key, value in expected.items()
                    )
                ),
                None,
            )
            return _ScalarResult(match)
        export_id = next(iter(parameters.values()))
        return _ScalarResult(
            next(
                (item for item in self.outboxes if item.export_id == export_id),
                None,
            )
        )

    def add_all(self, records) -> None:
        self.pending.extend(records)

    async def commit(self) -> None:
        for record in self.pending:
            target = self.exports if isinstance(record, FrameExport) else self.outboxes
            target.append(record)
        self.pending.clear()

    async def rollback(self) -> None:
        self.pending.clear()


class _Results:
    def __init__(self, manifest: StoredManifestV1) -> None:
        self.manifest = manifest

    async def _load(self, _job_id):
        return object(), self.manifest


def _manifest(job_id, run_token) -> StoredManifestV1:
    frames = [
        {
            "index": index,
            "filename": f"frame_{index:06d}_{index * 1000}ms_640x480.jpg",
            "object_key": (
                f"jobs/{job_id}/results/{run_token}/frames/"
                f"frame_{index:06d}_{index * 1000}ms_640x480.jpg"
            ),
            "content_type": "image/jpeg",
            "size_bytes": index + 1,
            "sha256": f"{index + 1:064x}",
            "timestamp_ms": index * 1000,
            "width": 640,
            "height": 480,
        }
        for index in range(3)
    ]
    return StoredManifestV1.model_validate(
        {
            "schema_version": 1,
            "job_id": job_id,
            "run_token": run_token,
            "created_at": datetime.now(UTC),
            "summary": {
                "frames_saved": 3,
                "candidates": 3,
                "shortlisted": 3,
                "duplicates_removed": 0,
                "processing_seconds": 1.0,
                "duration_seconds": 3.0,
            },
            "frames": frames,
        }
    )


def test_service_canonicalizes_selection_and_scopes_identity() -> None:
    async def scenario() -> None:
        job_id, first_run = uuid4(), uuid4()
        session = _MemorySession()
        results = _Results(_manifest(job_id, first_run))
        service = FrameExportService(
            session, results, max_frames=10, max_total_bytes=100
        )
        reversed_selection = await service.create(
            job_id,
            CreateFrameExportRequest(mode="selected", frame_indices=[2, 0]),
        )
        canonical_selection = await service.create(
            job_id,
            CreateFrameExportRequest(mode="selected", frame_indices=[0, 2]),
        )
        all_frames = await service.create(job_id, CreateFrameExportRequest(mode="all"))
        results.manifest = _manifest(job_id, uuid4())
        new_run = await service.create(
            job_id,
            CreateFrameExportRequest(mode="selected", frame_indices=[0, 2]),
        )
        assert reversed_selection.id == canonical_selection.id
        assert session.exports[0].frame_indices == [0, 2]
        assert all_frames.id != canonical_selection.id
        assert new_run.id != canonical_selection.id

    asyncio.run(scenario())


def test_concurrent_create_integrity_error_returns_winning_export() -> None:
    class RacingSession(_MemorySession):
        async def commit(self) -> None:
            proposed = next(
                item for item in self.pending if isinstance(item, FrameExport)
            )
            winner = FrameExport(
                id=uuid4(),
                job_id=proposed.job_id,
                result_run_token=proposed.result_run_token,
                status=ExportStatus.PREPARING,
                mode=proposed.mode,
                frame_indices=proposed.frame_indices,
                selection_hash=proposed.selection_hash,
                created_at=proposed.created_at,
            )
            self.exports.append(winner)
            raise IntegrityError("insert", {}, RuntimeError("unique race"))

    async def scenario() -> None:
        job_id, run_token = uuid4(), uuid4()
        session = RacingSession()
        service = FrameExportService(
            session,
            _Results(_manifest(job_id, run_token)),
            max_frames=10,
            max_total_bytes=100,
        )
        result = await service.create(
            job_id,
            CreateFrameExportRequest(mode="selected", frame_indices=[1, 0]),
        )
        assert result.id == session.exports[0].id
        assert session.pending == []

    asyncio.run(scenario())


def test_download_filename_is_safe_and_includes_sequence_and_timestamp() -> None:
    assert _download_filename(0, 1_400, "image/jpeg") == ("frame_0001_00-00-01.400.jpg")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        (
            "GET",
            "/api/v1/jobs/11111111-1111-4111-8111-111111111111/result/frames/0/download",
        ),
        ("POST", "/api/v1/jobs/11111111-1111-4111-8111-111111111111/exports"),
        (
            "GET",
            "/api/v1/jobs/11111111-1111-4111-8111-111111111111/exports/22222222-2222-4222-8222-222222222222",
        ),
    ],
)
def test_export_routes_are_result_rate_limited(method: str, path: str) -> None:
    assert protected_group(method, path) == (
        "results",
        "rate_limit_result_requests",
    )
