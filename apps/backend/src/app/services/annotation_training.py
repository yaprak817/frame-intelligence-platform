import asyncio
import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.annotation_training import (
    AnnotationTrainingOutbox,
    AnnotationTrainingRun,
    AnnotationTrainingSnapshotBox,
    AnnotationTrainingSnapshotClass,
    AnnotationTrainingSnapshotImage,
)
from app.models.annotations import (
    AnnotationBox,
    AnnotationClass,
    AnnotationImage,
    AnnotationProject,
)
from app.schemas.annotation_training import (
    AnnotationTrainingConfig,
    AnnotationTrainingPage,
    AnnotationTrainingResponse,
    AnnotationTrainingStatus,
    CreateAnnotationTrainingRequest,
)
from app.schemas.artifacts import StoredDatasetManifestV1, StoredManifestV1
from app.services.result_artifacts import ResultArtifactService
from app.storage.s3 import ObjectStorageError, ObjectStream

MIN_IMAGES = 50
MAX_CLASSES = 20
MAX_BOXES = 20_000
DEFAULT_MAX_SNAPSHOTS_PER_PROJECT = 5
RECOVERABLE_UNIQUE_CONSTRAINTS = {
    "uq_annotation_training_runs_idempotency",
    "uq_annotation_training_runs_fingerprint",
    "uq_annotation_training_runs_source_revision",
    "uq_annotation_training_runs_version",
    "uq_annotation_training_runs_active_project",
}


class AnnotationTrainingError(RuntimeError):
    code = "ANNOTATION_TRAINING_NOT_AVAILABLE"


class AnnotationTrainingNotFound(AnnotationTrainingError):
    pass


class AnnotationTrainingRevisionConflict(AnnotationTrainingError):
    code = "ANNOTATION_REVISION_CONFLICT"


class AnnotationTrainingInsufficientImages(AnnotationTrainingError):
    code = "ANNOTATION_TRAINING_INSUFFICIENT_IMAGES"


class AnnotationTrainingInvalidDataset(AnnotationTrainingError):
    code = "ANNOTATION_TRAINING_INVALID_DATASET"


class AnnotationTrainingIdempotencyConflict(AnnotationTrainingError):
    code = "ANNOTATION_TRAINING_IDEMPOTENCY_CONFLICT"


class AnnotationTrainingSourceChanged(AnnotationTrainingError):
    code = "ANNOTATION_SOURCE_CHANGED"


class AnnotationTrainingSnapshotLimitReached(AnnotationTrainingError):
    code = "ANNOTATION_TRAINING_SNAPSHOT_LIMIT_REACHED"


class AnnotationTrainingRevisionAlreadySnapshotted(AnnotationTrainingError):
    code = "ANNOTATION_TRAINING_REVISION_ALREADY_SNAPSHOTTED"


class AnnotationTrainingAlreadyActive(AnnotationTrainingError):
    code = "ANNOTATION_TRAINING_ALREADY_ACTIVE"


class AnnotationTrainingInvalidState(AnnotationTrainingError):
    code = "ANNOTATION_TRAINING_INVALID_STATE"


class AnnotationTrainingService:
    def __init__(
        self,
        session: AsyncSession,
        results: ResultArtifactService,
        *,
        max_snapshots_per_project: int = DEFAULT_MAX_SNAPSHOTS_PER_PROJECT,
    ) -> None:
        if not 1 <= max_snapshots_per_project <= 20:
            raise ValueError("max_snapshots_per_project must be between 1 and 20")
        self.session = session
        self.results = results
        self.max_snapshots_per_project = max_snapshots_per_project

    async def create(
        self,
        job_id: UUID,
        request: CreateAnnotationTrainingRequest,
        idempotency_key: str,
    ) -> tuple[AnnotationTrainingResponse, bool]:
        _job, manifest = await self.results.annotation_source(job_id)
        project = await self.session.scalar(
            select(AnnotationProject)
            .where(
                AnnotationProject.job_id == job_id,
                AnnotationProject.result_run_token == manifest.run_token,
            )
            .with_for_update()
        )
        if project is None:
            await self.session.rollback()
            raise AnnotationTrainingNotFound
        project_id = project.id

        config = request.config.model_dump(mode="json")
        config_bytes = json.dumps(config, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
        config_hash = hashlib.sha256(config_bytes).hexdigest()
        fingerprint = hashlib.sha256(
            b"annotation-training-snapshot:v1\0"
            + project.id.bytes
            + request.expected_revision.to_bytes(8, "big", signed=False)
            + config_bytes
        ).hexdigest()
        existing = await self._idempotent(project_id, idempotency_key, fingerprint)
        if existing is not None:
            response = self.response(existing, job_id)
            await self.session.rollback()
            return response, False
        if project.revision != request.expected_revision:
            await self.session.rollback()
            raise AnnotationTrainingRevisionConflict

        revision_snapshot = await self.session.scalar(
            select(AnnotationTrainingRun).where(
                AnnotationTrainingRun.project_id == project_id,
                AnnotationTrainingRun.source_revision == project.revision,
            )
        )
        if revision_snapshot is not None:
            await self.session.rollback()
            raise AnnotationTrainingRevisionAlreadySnapshotted
        snapshot_count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(AnnotationTrainingRun)
                .where(AnnotationTrainingRun.project_id == project_id)
            )
            or 0
        )
        if snapshot_count >= self.max_snapshots_per_project:
            await self.session.rollback()
            raise AnnotationTrainingSnapshotLimitReached

        images = list(
            (
                await self.session.scalars(
                    select(AnnotationImage)
                    .where(
                        AnnotationImage.project_id == project_id,
                        AnnotationImage.completed.is_(True),
                    )
                    .order_by(AnnotationImage.image_index)
                    .limit(request.config.max_snapshot_images)
                )
            ).all()
        )
        if len(images) < MIN_IMAGES:
            await self.session.rollback()
            raise AnnotationTrainingInsufficientImages
        classes = list(
            (
                await self.session.scalars(
                    select(AnnotationClass)
                    .where(AnnotationClass.project_id == project_id)
                    .order_by(AnnotationClass.yolo_index)
                )
            ).all()
        )
        selected_indices = [item.image_index for item in images]
        boxes = list(
            (
                await self.session.scalars(
                    select(AnnotationBox)
                    .where(
                        AnnotationBox.project_id == project_id,
                        AnnotationBox.image_index.in_(selected_indices),
                    )
                    .order_by(AnnotationBox.image_index, AnnotationBox.id)
                )
            ).all()
        )
        if not boxes or len(boxes) > MAX_BOXES:
            await self.session.rollback()
            raise AnnotationTrainingInvalidDataset
        used_class_ids = {item.class_id for item in boxes}
        classes = [item for item in classes if item.id in used_class_ids]
        if (
            not classes
            or len(classes) > MAX_CLASSES
            or {item.id for item in classes} != used_class_ids
        ):
            await self.session.rollback()
            raise AnnotationTrainingInvalidDataset

        metadata = await self._metadata_for_project(project, images, manifest)
        if any(item.image_index not in metadata for item in images):
            await self.session.rollback()
            raise AnnotationTrainingSourceChanged
        splits = self.split(project_id, images, boxes)
        train_count = sum(value == "train" for value in splits.values())
        validation_count = len(splits) - train_count
        version = (
            int(
                await self.session.scalar(
                    select(func.max(AnnotationTrainingRun.snapshot_version)).where(
                        AnnotationTrainingRun.project_id == project_id
                    )
                )
                or 0
            )
            + 1
        )
        now = datetime.now(UTC)
        run = AnnotationTrainingRun(
            id=uuid4(),
            project_id=project_id,
            snapshot_version=version,
            source_revision=project.revision,
            status=AnnotationTrainingStatus.SNAPSHOT_READY,
            selected_image_count=len(images),
            selected_class_count=len(classes),
            selected_box_count=len(boxes),
            train_image_count=train_count,
            validation_image_count=validation_count,
            config=config,
            config_hash=config_hash,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            created_at=now,
            started_at=None,
            completed_at=now,
            failure_code=None,
            snapshot_artifact_reference=None,
            snapshot_artifact_size_bytes=None,
            snapshot_artifact_sha256=None,
            model_artifact_reference=None,
            model_artifact_size_bytes=None,
            model_artifact_sha256=None,
            progress_completed=0,
            progress_total=0,
            lease_token=None,
            lease_expires_at=None,
            attempt_generation=0,
            working_prefix=None,
            model_version=None,
        )
        yolo_by_class = {item.id: item.yolo_index for item in classes}
        try:
            self.session.add(run)
            await self.session.flush()
            self.session.add_all(
                [
                    AnnotationTrainingSnapshotClass(
                        training_id=run.id,
                        project_id=project_id,
                        class_id=item.id,
                        yolo_index=item.yolo_index,
                        name=item.name,
                    )
                    for item in classes
                ]
            )
            await self.session.flush()
            self.session.add_all(
                [
                    AnnotationTrainingSnapshotImage(
                        training_id=run.id,
                        project_id=project_id,
                        image_index=item.image_index,
                        filename=metadata[item.image_index][0],
                        source_object_key=metadata[item.image_index][1],
                        source_size_bytes=metadata[item.image_index][2],
                        source_content_type=metadata[item.image_index][3],
                        source_sha256=metadata[item.image_index][4],
                        width=metadata[item.image_index][5],
                        height=metadata[item.image_index][6],
                        timestamp_ms=metadata[item.image_index][7],
                        split=splits[item.image_index],
                    )
                    for item in images
                ]
            )
            await self.session.flush()
            self.session.add_all(
                [
                    AnnotationTrainingSnapshotBox(
                        training_id=run.id,
                        project_id=project_id,
                        box_id=item.id,
                        image_index=item.image_index,
                        yolo_index=yolo_by_class[item.class_id],
                        x_center=item.x_center,
                        y_center=item.y_center,
                        width=item.width,
                        height=item.height,
                    )
                    for item in boxes
                ]
            )
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            winner = await self._recover_unique_race(
                error,
                project_id=project_id,
                idempotency_key=idempotency_key,
                fingerprint=fingerprint,
                source_revision=request.expected_revision,
            )
            return self.response(winner, job_id), False
        return self.response(run, job_id), True

    async def list_runs(
        self, job_id: UUID, *, limit: int, after_snapshot_version: int | None
    ) -> AnnotationTrainingPage:
        project = await self._current_project(job_id)
        query = select(AnnotationTrainingRun).where(
            AnnotationTrainingRun.project_id == project.id
        )
        if after_snapshot_version is not None:
            query = query.where(
                AnnotationTrainingRun.snapshot_version > after_snapshot_version
            )
        # Version is unique within a project, so it is a sufficient stable cursor.
        rows = (
            await self.session.scalars(
                query.order_by(
                    AnnotationTrainingRun.snapshot_version.asc(),
                    AnnotationTrainingRun.id.asc(),
                ).limit(limit + 1)
            )
        ).all()
        has_more = len(rows) > limit
        page = rows[:limit]
        return AnnotationTrainingPage(
            items=[self.response(item, job_id) for item in page],
            next_cursor=page[-1].snapshot_version if has_more else None,
            has_more=has_more,
        )

    async def get(self, job_id: UUID, training_id: UUID) -> AnnotationTrainingResponse:
        project = await self._current_project(job_id)
        item = await self.session.scalar(
            select(AnnotationTrainingRun).where(
                AnnotationTrainingRun.id == training_id,
                AnnotationTrainingRun.project_id == project.id,
            )
        )
        if item is None:
            raise AnnotationTrainingNotFound
        return self.response(item, job_id)

    async def start(
        self, job_id: UUID, training_id: UUID
    ) -> tuple[AnnotationTrainingResponse, bool]:
        project = await self._current_project(job_id)
        item = await self.session.scalar(
            select(AnnotationTrainingRun)
            .where(
                AnnotationTrainingRun.id == training_id,
                AnnotationTrainingRun.project_id == project.id,
            )
            .with_for_update()
        )
        if item is None:
            await self.session.rollback()
            raise AnnotationTrainingNotFound
        if item.status in {
            AnnotationTrainingStatus.PENDING,
            AnnotationTrainingStatus.RUNNING,
            AnnotationTrainingStatus.SUCCEEDED,
        }:
            response = self.response(item, job_id)
            await self.session.rollback()
            return response, False
        if item.status not in {
            AnnotationTrainingStatus.SNAPSHOT_READY,
            AnnotationTrainingStatus.FAILED,
        }:
            await self.session.rollback()
            raise AnnotationTrainingInvalidState
        active = await self.session.scalar(
            select(AnnotationTrainingRun.id)
            .where(
                AnnotationTrainingRun.project_id == project.id,
                AnnotationTrainingRun.id != item.id,
                AnnotationTrainingRun.status.in_(
                    (AnnotationTrainingStatus.PENDING, AnnotationTrainingStatus.RUNNING)
                ),
            )
            .limit(1)
        )
        if active is not None:
            await self.session.rollback()
            raise AnnotationTrainingAlreadyActive
        now = datetime.now(UTC)
        item.status = AnnotationTrainingStatus.PENDING
        item.completed_at = None
        item.failure_code = None
        item.progress_completed = 0
        item.progress_total = int(item.config.get("epochs", 10))
        outbox = await self.session.scalar(
            select(AnnotationTrainingOutbox)
            .where(
                AnnotationTrainingOutbox.training_id == item.id,
                AnnotationTrainingOutbox.event_type == "START_ANNOTATION_TRAINING",
            )
            .with_for_update()
        )

        if outbox is None:
            self.session.add(
                AnnotationTrainingOutbox(
                    id=uuid4(),
                    training_id=item.id,
                    event_type="START_ANNOTATION_TRAINING",
                    payload={"training_id": str(item.id)},
                    created_at=now,
                    published_at=None,
                    attempt_count=0,
                    next_attempt_at=now,
                )
            )
        else:
            outbox.payload = {"training_id": str(item.id)}
            outbox.created_at = now
            outbox.published_at = None
            outbox.attempt_count = 0
            outbox.next_attempt_at = now
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            current = await self.session.scalar(
                select(AnnotationTrainingRun).where(
                    AnnotationTrainingRun.id == item.id,
                    AnnotationTrainingRun.project_id == project.id,
                )
            )
            if current is not None and current.status in {
                AnnotationTrainingStatus.PENDING,
                AnnotationTrainingStatus.RUNNING,
                AnnotationTrainingStatus.SUCCEEDED,
            }:
                return self.response(current, job_id), False
            raise
        return self.response(item, job_id), True

    async def latest_model(self, job_id: UUID) -> AnnotationTrainingRun:
        project = await self._current_project(job_id)
        item = await self.session.scalar(
            select(AnnotationTrainingRun)
            .where(
                AnnotationTrainingRun.project_id == project.id,
                AnnotationTrainingRun.status == AnnotationTrainingStatus.SUCCEEDED,
            )
            .order_by(AnnotationTrainingRun.model_version.desc())
            .limit(1)
        )
        if item is None:
            raise AnnotationTrainingNotFound
        return item

    async def snapshot_stream(
        self, job_id: UUID, training_id: UUID
    ) -> tuple[ObjectStream, str]:
        project = await self._current_project(job_id)
        item = await self.session.scalar(
            select(AnnotationTrainingRun).where(
                AnnotationTrainingRun.id == training_id,
                AnnotationTrainingRun.project_id == project.id,
                AnnotationTrainingRun.status == AnnotationTrainingStatus.SUCCEEDED,
            )
        )
        if item is None or item.snapshot_artifact_reference is None:
            raise AnnotationTrainingNotFound
        expected_key = f"annotations/{project.id}/trainings/{item.id}/snapshot.zip"
        expected_reference = f"s3://{self.results._storage.bucket}/{expected_key}"
        if not hmac.compare_digest(
            item.snapshot_artifact_reference, expected_reference
        ):
            raise ObjectStorageError("Invalid training artifact reference")
        stream = await self.results._storage.open_stream(expected_key)
        if (
            stream.metadata.size_bytes != item.snapshot_artifact_size_bytes
            or stream.metadata.content_type != "application/zip"
            or stream.metadata.sha256 != item.snapshot_artifact_sha256
        ):
            await asyncio.to_thread(stream.body.close)
            raise ObjectStorageError("Invalid training artifact metadata")
        return stream, f"annotation-snapshot-v{item.snapshot_version}.zip"

    async def _current_project(self, job_id: UUID) -> AnnotationProject:
        _job, manifest = await self.results.annotation_source(job_id)
        project = await self.session.scalar(
            select(AnnotationProject).where(
                AnnotationProject.job_id == job_id,
                AnnotationProject.result_run_token == manifest.run_token,
            )
        )
        if project is None:
            raise AnnotationTrainingNotFound
        return project

    async def _idempotent(
        self, project_id: UUID, key: str, fingerprint: str
    ) -> AnnotationTrainingRun | None:
        item = await self.session.scalar(
            select(AnnotationTrainingRun).where(
                AnnotationTrainingRun.project_id == project_id,
                AnnotationTrainingRun.idempotency_key == key,
            )
        )
        if item is not None:
            if not hmac.compare_digest(item.request_fingerprint, fingerprint):
                raise AnnotationTrainingIdempotencyConflict
            return item
        return None

    @staticmethod
    def split(
        project_id: UUID,
        images: list[AnnotationImage],
        boxes: list[AnnotationBox],
    ) -> dict[int, str]:
        def rank(item: AnnotationImage) -> tuple[bytes, int]:
            return (
                hashlib.sha256(
                    project_id.bytes
                    + item.image_index.to_bytes(8, "big", signed=False)
                    + bytes.fromhex(item.image_sha256)
                ).digest(),
                item.image_index,
            )

        ranked = sorted(
            images,
            key=rank,
        )
        images_by_index = {item.image_index: item for item in images}
        positive_by_class: dict[UUID, set[int]] = {}
        for box in boxes:
            positive_by_class.setdefault(box.class_id, set()).add(box.image_index)
        anchors = {
            min(
                (images_by_index[index] for index in indices),
                key=rank,
            ).image_index
            for indices in positive_by_class.values()
        }
        validation_count = max(1, len(ranked) // 5)
        validation_candidates = [
            item for item in ranked if item.image_index not in anchors
        ]
        validation = {
            item.image_index for item in validation_candidates[:validation_count]
        }
        return {
            item.image_index: "val" if item.image_index in validation else "train"
            for item in images
        }

    @staticmethod
    def _constraint_name(error: IntegrityError) -> str | None:
        current: object | None = error.orig
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            diag = getattr(current, "diag", None)
            name = getattr(diag, "constraint_name", None) or getattr(
                current, "constraint_name", None
            )
            if isinstance(name, str):
                return name
            current = getattr(current, "__cause__", None) or getattr(
                current, "__context__", None
            )
        return None

    async def _recover_unique_race(
        self,
        error: IntegrityError,
        *,
        project_id: UUID,
        idempotency_key: str,
        fingerprint: str,
        source_revision: int,
    ) -> AnnotationTrainingRun:
        constraint = self._constraint_name(error)
        if constraint not in RECOVERABLE_UNIQUE_CONSTRAINTS:
            raise error
        winner = await self._idempotent(project_id, idempotency_key, fingerprint)
        if winner is not None:
            return winner
        if constraint == "uq_annotation_training_runs_source_revision":
            revision_winner = await self.session.scalar(
                select(AnnotationTrainingRun).where(
                    AnnotationTrainingRun.project_id == project_id,
                    AnnotationTrainingRun.source_revision == source_revision,
                )
            )
            if revision_winner is not None:
                raise AnnotationTrainingRevisionAlreadySnapshotted from None
        raise error

    async def _metadata_for_project(
        self,
        project: AnnotationProject,
        images: list[AnnotationImage],
        anchor_manifest: StoredManifestV1 | StoredDatasetManifestV1,
    ) -> dict[int, tuple]:
        # Legacy job-level projects keep the original single-manifest behavior.
        if project.brand_id is None:
            return self._metadata(anchor_manifest)

        metadata: dict[int, tuple] = {}
        manifests: dict[UUID, StoredManifestV1 | StoredDatasetManifestV1] = {
            project.job_id: anchor_manifest
        }

        for image in images:
            source_job_id = image.source_job_id or project.job_id
            source_run_token = image.source_result_run_token or project.result_run_token
            source_image_index = (
                image.source_image_index
                if image.source_image_index is not None
                else image.image_index
            )

            manifest = manifests.get(source_job_id)
            if manifest is None:
                _job, manifest = await self.results.annotation_source(source_job_id)
                manifests[source_job_id] = manifest

            if manifest.run_token != source_run_token:
                raise AnnotationTrainingSourceChanged

            source_metadata = self._metadata(manifest)
            item = source_metadata.get(source_image_index)
            if item is None:
                raise AnnotationTrainingSourceChanged

            metadata[image.image_index] = item

        return metadata

    @staticmethod
    def _metadata(
        manifest: StoredManifestV1 | StoredDatasetManifestV1,
    ) -> dict[int, tuple]:
        if isinstance(manifest, StoredDatasetManifestV1):
            return {
                item.index: (
                    item.filename,
                    item.yolo_object_key,
                    item.yolo_size_bytes,
                    "image/jpeg",
                    item.yolo_sha256,
                    item.output_width,
                    item.output_height,
                    None,
                )
                for item in manifest.images
                if item.yolo_object_key is not None
            }
        return {
            item.index: (
                item.filename,
                item.object_key,
                item.size_bytes,
                item.content_type,
                item.sha256,
                item.width,
                item.height,
                item.timestamp_ms,
            )
            for item in manifest.frames
        }

    @staticmethod
    def response(
        item: AnnotationTrainingRun, job_id: UUID
    ) -> AnnotationTrainingResponse:
        return AnnotationTrainingResponse(
            id=item.id,
            snapshot_version=item.snapshot_version,
            source_revision=item.source_revision,
            status=AnnotationTrainingStatus(item.status),
            image_count=item.selected_image_count,
            class_count=item.selected_class_count,
            box_count=item.selected_box_count,
            train_image_count=item.train_image_count,
            validation_image_count=item.validation_image_count,
            config=AnnotationTrainingConfig.model_validate(item.config),
            created_at=item.created_at,
            started_at=item.started_at,
            completed_at=item.completed_at,
            failure_code=item.failure_code,
            progress_completed=item.progress_completed,
            progress_total=item.progress_total,
            model_version=item.model_version,
            status_url=f"/api/v1/jobs/{job_id}/annotations/trainings/{item.id}",
            snapshot_download_url=(
                f"/api/v1/jobs/{job_id}/annotations/trainings/{item.id}/snapshot/download"
                if item.status == AnnotationTrainingStatus.SUCCEEDED
                else None
            ),
        )
