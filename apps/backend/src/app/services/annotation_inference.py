from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.annotation_inference import (
    AnnotationInferenceOutbox,
    AnnotationInferenceRun,
)
from app.models.annotation_training import (
    AnnotationTrainingRun,
    AnnotationTrainingSnapshotClass,
)
from app.models.annotations import (
    AnnotationBox,
    AnnotationClass,
    AnnotationImage,
    AnnotationProject,
)
from app.schemas.annotation_inference import (
    AnnotationInferenceResponse,
    AnnotationInferenceStatus,
)
from app.schemas.annotation_training import AnnotationTrainingStatus
from app.schemas.artifacts import StoredDatasetManifestV1, StoredManifestV1
from app.services.result_artifacts import ResultArtifactService


class AnnotationInferenceError(RuntimeError):
    code = "ANNOTATION_INFERENCE_NOT_AVAILABLE"


class AnnotationInferenceNotFound(AnnotationInferenceError):
    code = "ANNOTATION_INFERENCE_NOT_FOUND"


class AnnotationInferenceModelNotFound(AnnotationInferenceError):
    code = "ANNOTATION_INFERENCE_MODEL_NOT_FOUND"


class AnnotationInferenceNoTargets(AnnotationInferenceError):
    code = "ANNOTATION_INFERENCE_NO_TARGETS"


class AnnotationInferenceLimitExceeded(AnnotationInferenceError):
    code = "ANNOTATION_INFERENCE_LIMIT_EXCEEDED"


class AnnotationInferenceSourceChanged(AnnotationInferenceError):
    code = "ANNOTATION_SOURCE_CHANGED"


class AnnotationInferenceModelClassesStale(AnnotationInferenceError):
    code = "ANNOTATION_INFERENCE_MODEL_CLASSES_STALE"


class AnnotationInferenceDispatchFailed(AnnotationInferenceError):
    code = "ANNOTATION_INFERENCE_DISPATCH_FAILED"


class AnnotationInferenceService:
    def __init__(
        self,
        session: AsyncSession,
        results: ResultArtifactService,
    ) -> None:
        self.session = session
        self.results = results

    async def start(self, job_id: UUID) -> tuple[AnnotationInferenceResponse, bool]:
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
            raise AnnotationInferenceNotFound

        active = await self.session.scalar(
            select(AnnotationInferenceRun)
            .where(
                AnnotationInferenceRun.project_id == project.id,
                AnnotationInferenceRun.status.in_(
                    (
                        AnnotationInferenceStatus.PENDING,
                        AnnotationInferenceStatus.RUNNING,
                    )
                ),
            )
            .order_by(AnnotationInferenceRun.created_at.desc())
            .limit(1)
        )
        if active is not None:
            response = self.response(active, job_id)
            await self.session.rollback()
            return response, False

        training = await self.session.scalar(
            select(AnnotationTrainingRun)
            .where(
                AnnotationTrainingRun.project_id == project.id,
                AnnotationTrainingRun.status == AnnotationTrainingStatus.SUCCEEDED,
            )
            .order_by(AnnotationTrainingRun.model_version.desc())
            .limit(1)
        )
        if training is None or training.model_version is None:
            await self.session.rollback()
            raise AnnotationInferenceModelNotFound

        model_classes = list(
            (
                await self.session.scalars(
                    select(AnnotationTrainingSnapshotClass)
                    .where(AnnotationTrainingSnapshotClass.training_id == training.id)
                    .order_by(AnnotationTrainingSnapshotClass.yolo_index)
                )
            ).all()
        )
        current_class_ids = set(
            (
                await self.session.scalars(
                    select(AnnotationClass.id).where(
                        AnnotationClass.project_id == project.id
                    )
                )
            ).all()
        )
        if not model_classes or any(
            item.class_id not in current_class_ids for item in model_classes
        ):
            await self.session.rollback()
            raise AnnotationInferenceModelClassesStale

        has_box = exists(
            select(AnnotationBox.id).where(
                AnnotationBox.project_id == AnnotationImage.project_id,
                AnnotationBox.image_index == AnnotationImage.image_index,
            )
        )
        images = list(
            (
                await self.session.scalars(
                    select(AnnotationImage)
                    .where(
                        AnnotationImage.project_id == project.id,
                        AnnotationImage.completed.is_(False),
                        ~has_box,
                    )
                    .order_by(AnnotationImage.image_index)
                    .limit(1001)
                )
            ).all()
        )
        if len(images) > 1000:
            await self.session.rollback()
            raise AnnotationInferenceLimitExceeded
        if not images:
            latest = await self.session.scalar(
                select(AnnotationInferenceRun)
                .where(
                    AnnotationInferenceRun.project_id == project.id,
                    AnnotationInferenceRun.status
                    == AnnotationInferenceStatus.SUCCEEDED,
                )
                .order_by(AnnotationInferenceRun.created_at.desc())
                .limit(1)
            )
            if latest is not None:
                response = self.response(latest, job_id)
                await self.session.rollback()
                return response, False
            await self.session.rollback()
            raise AnnotationInferenceNoTargets

        metadata = await self._metadata_for_project(project, images, manifest)
        targets: list[dict[str, object]] = []
        for image in images:
            source = metadata.get(image.image_index)
            if source is None:
                await self.session.rollback()
                raise AnnotationInferenceSourceChanged
            expected_sha = image.yolo_sha256 if source[7] else image.image_sha256
            if source[4] != expected_sha:
                await self.session.rollback()
                raise AnnotationInferenceSourceChanged
            targets.append(
                {
                    "image_index": image.image_index,
                    "source_object_key": source[0],
                    "source_size_bytes": source[1],
                    "source_content_type": source[2],
                    "source_sha256": source[4],
                    "width": source[5],
                    "height": source[6],
                }
            )

        now = datetime.now(UTC)
        run = AnnotationInferenceRun(
            id=uuid4(),
            project_id=project.id,
            training_id=training.id,
            model_version=training.model_version,
            status=AnnotationInferenceStatus.PENDING,
            targets=targets,
            target_image_count=len(targets),
            processed_image_count=0,
            created_box_count=0,
            created_at=now,
            started_at=None,
            completed_at=None,
            failure_code=None,
        )
        self.session.add(run)
        await self.session.flush()
        self.session.add(
            AnnotationInferenceOutbox(
                id=uuid4(),
                inference_id=run.id,
                created_at=now,
                published_at=None,
                attempt_count=0,
                next_attempt_at=now,
            )
        )
        try:
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            winner = await self.session.scalar(
                select(AnnotationInferenceRun)
                .where(
                    AnnotationInferenceRun.project_id == project.id,
                    AnnotationInferenceRun.status.in_(
                        (
                            AnnotationInferenceStatus.PENDING,
                            AnnotationInferenceStatus.RUNNING,
                        )
                    ),
                )
                .order_by(AnnotationInferenceRun.created_at.desc())
                .limit(1)
            )
            if winner is None:
                raise
            return self.response(winner, job_id), False
        except Exception:
            await self.session.rollback()
            raise

        return self.response(run, job_id), True

    async def get(self, job_id: UUID, run_id: UUID) -> AnnotationInferenceResponse:
        project = await self._current_project(job_id)
        run = await self.session.scalar(
            select(AnnotationInferenceRun).where(
                AnnotationInferenceRun.id == run_id,
                AnnotationInferenceRun.project_id == project.id,
            )
        )
        if run is None:
            raise AnnotationInferenceNotFound
        return self.response(run, job_id)

    async def latest(self, job_id: UUID) -> AnnotationInferenceResponse:
        project = await self._current_project(job_id)
        run = await self.session.scalar(
            select(AnnotationInferenceRun)
            .where(AnnotationInferenceRun.project_id == project.id)
            .order_by(AnnotationInferenceRun.created_at.desc())
            .limit(1)
        )
        if run is None:
            raise AnnotationInferenceNotFound
        return self.response(run, job_id)

    async def _current_project(self, job_id: UUID) -> AnnotationProject:
        _job, manifest = await self.results.annotation_source(job_id)
        project = await self.session.scalar(
            select(AnnotationProject).where(
                AnnotationProject.job_id == job_id,
                AnnotationProject.result_run_token == manifest.run_token,
            )
        )
        if project is None:
            raise AnnotationInferenceNotFound
        return project

    async def _metadata_for_project(
        self,
        project: AnnotationProject,
        images: list[AnnotationImage],
        anchor_manifest: StoredManifestV1 | StoredDatasetManifestV1,
    ) -> dict[int, tuple]:
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

            source_manifest = manifests.get(source_job_id)
            if source_manifest is None:
                _job, source_manifest = await self.results.annotation_source(
                    source_job_id
                )
                manifests[source_job_id] = source_manifest

            if source_manifest.run_token != source_run_token:
                raise AnnotationInferenceSourceChanged

            source_metadata = self._metadata(source_manifest)
            source = source_metadata.get(source_image_index)
            if source is None:
                raise AnnotationInferenceSourceChanged

            metadata[image.image_index] = source

        return metadata

    @staticmethod
    def _metadata(
        manifest: StoredManifestV1 | StoredDatasetManifestV1,
    ) -> dict[int, tuple]:
        if isinstance(manifest, StoredDatasetManifestV1):
            return {
                image.index: (
                    image.yolo_object_key,
                    image.yolo_size_bytes,
                    "image/jpeg",
                    image.filename,
                    image.yolo_sha256,
                    image.output_width,
                    image.output_height,
                    True,
                )
                for image in manifest.images
                if image.quality_category in {"normal", "challenging"}
                and image.yolo_object_key is not None
                and image.yolo_size_bytes is not None
                and image.yolo_sha256 is not None
                and image.output_width == 640
                and image.output_height == 640
            }
        return {
            frame.index: (
                frame.object_key,
                frame.size_bytes,
                frame.content_type,
                frame.filename,
                frame.sha256,
                frame.width,
                frame.height,
                False,
            )
            for frame in manifest.frames
        }

    @staticmethod
    def response(
        run: AnnotationInferenceRun, job_id: UUID
    ) -> AnnotationInferenceResponse:
        return AnnotationInferenceResponse(
            id=run.id,
            training_id=run.training_id,
            model_version=run.model_version,
            status=AnnotationInferenceStatus(run.status),
            target_image_count=run.target_image_count,
            processed_image_count=run.processed_image_count,
            created_box_count=run.created_box_count,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            failure_code=run.failure_code,
            status_url=f"/api/v1/jobs/{job_id}/annotations/auto-label/{run.id}",
        )
