import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.frame_export import FrameExport, FrameExportOutbox
from app.schemas.artifacts import StoredManifestV1
from app.schemas.exports import (
    CreateFrameExportRequest,
    ExportStatus,
    FrameExportResponse,
)
from app.services.result_artifacts import ResultArtifactService


class ExportNotFoundError(RuntimeError):
    pass


class ExportNotReadyError(RuntimeError):
    pass


def selection_hash(indices: list[int]) -> str:
    return hashlib.sha256(
        json.dumps(indices, separators=(",", ":")).encode()
    ).hexdigest()


class FrameExportService:
    def __init__(
        self,
        session: AsyncSession,
        results: ResultArtifactService,
        *,
        max_frames: int,
        max_total_bytes: int,
    ) -> None:
        self.session = session
        self.results = results
        self.max_frames = max_frames
        self.max_total_bytes = max_total_bytes

    async def create(
        self, job_id: UUID, request: CreateFrameExportRequest
    ) -> FrameExportResponse:
        _job, manifest = await self.results._load(job_id)
        indices = self._indices(request, manifest)
        if (
            len(indices) > self.max_frames
            or sum(manifest.frames[index].size_bytes for index in indices)
            > self.max_total_bytes
        ):
            raise ValueError("Frame export exceeds configured limits")
        canonical_selection_hash = selection_hash(indices)
        existing = (
            await self.session.execute(
                select(FrameExport).where(
                    FrameExport.job_id == job_id,
                    FrameExport.result_run_token == manifest.run_token,
                    FrameExport.mode == request.mode,
                    FrameExport.selection_hash == canonical_selection_hash,
                )
            )
        ).scalar_one_or_none()
        if existing:
            if (
                existing.status == ExportStatus.FAILED
                and existing.failure_code != "EXPORT_INVALID"
            ):
                existing.status = ExportStatus.PREPARING
                existing.completed_at = None
                existing.failure_code = None
                outbox = (
                    await self.session.execute(
                        select(FrameExportOutbox).where(
                            FrameExportOutbox.export_id == existing.id
                        )
                    )
                ).scalar_one()
                outbox.published_at = None
                outbox.next_attempt_at = datetime.now(UTC)
                await self.session.commit()
            return self.response(existing)
        now = datetime.now(UTC)
        export = FrameExport(
            id=uuid4(),
            job_id=job_id,
            result_run_token=manifest.run_token,
            status=ExportStatus.PREPARING,
            mode=request.mode,
            frame_indices=indices,
            selection_hash=canonical_selection_hash,
            created_at=now,
        )
        event = FrameExportOutbox(
            id=uuid4(),
            export_id=export.id,
            payload={"export_id": str(export.id)},
            created_at=now,
            next_attempt_at=now,
            attempt_count=0,
        )
        self.session.add_all([export, event])
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            export = (
                await self.session.execute(
                    select(FrameExport).where(
                        FrameExport.job_id == job_id,
                        FrameExport.result_run_token == manifest.run_token,
                        FrameExport.mode == request.mode,
                        FrameExport.selection_hash == canonical_selection_hash,
                    )
                )
            ).scalar_one()
        return self.response(export)

    async def get(self, job_id: UUID, export_id: UUID) -> FrameExport:
        export = await self.session.get(FrameExport, export_id)
        if export is None or export.job_id != job_id:
            raise ExportNotFoundError
        return export

    @staticmethod
    def _indices(
        request: CreateFrameExportRequest, manifest: StoredManifestV1
    ) -> list[int]:
        indices = (
            list(range(len(manifest.frames)))
            if request.mode == "all"
            else sorted(request.frame_indices or [])
        )
        if not indices or indices[-1] >= len(manifest.frames):
            raise ValueError("Invalid frame selection")
        return indices

    @staticmethod
    def response(export: FrameExport) -> FrameExportResponse:
        base = f"/api/v1/jobs/{export.job_id}/exports/{export.id}"
        count = len(export.frame_indices) if export.frame_indices is not None else 0
        return FrameExportResponse(
            id=export.id,
            job_id=export.job_id,
            status=ExportStatus(export.status),
            mode=export.mode,
            frame_count=count,
            created_at=export.created_at,
            completed_at=export.completed_at,
            status_url=base,
            download_url=f"{base}/download"
            if export.status == ExportStatus.READY
            else None,
            failure_code=export.failure_code,
        )
