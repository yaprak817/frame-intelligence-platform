import unicodedata
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.jobs import JobStatus, SourceType
from app.models.annotations import (
    AnnotationBox,
    AnnotationClass,
    AnnotationImage,
    AnnotationProject,
)
from app.schemas.annotations import (
    AnnotationBoxResponse,
    AnnotationClassMutationResponse,
    AnnotationClassResponse,
    AnnotationImageSummary,
    AnnotationLimits,
    AnnotationProjectResponse,
    CreateAnnotationClassRequest,
    ImageAnnotationsResponse,
    PutImageAnnotationsRequest,
    UpdateAnnotationClassRequest,
)
from app.services.result_artifacts import ResultArtifactService
from app.storage.s3 import ObjectStream


class AnnotationError(RuntimeError):
    code = "ANNOTATION_NOT_AVAILABLE"


class AnnotationNotAvailable(AnnotationError):
    pass


class AnnotationRevisionConflict(AnnotationError):
    code = "ANNOTATION_REVISION_CONFLICT"


class AnnotationClassInvalid(AnnotationError):
    code = "ANNOTATION_CLASS_INVALID"


class AnnotationClassInUse(AnnotationError):
    code = "ANNOTATION_CLASS_IN_USE"


class AnnotationClassOrderLocked(AnnotationError):
    code = "ANNOTATION_CLASS_ORDER_LOCKED"


class AnnotationLimitExceeded(AnnotationError):
    code = "ANNOTATION_LIMIT_EXCEEDED"


class AnnotationImageNotFound(AnnotationError):
    code = "ANNOTATION_IMAGE_NOT_FOUND"


class AnnotationSourceChanged(AnnotationError):
    code = "ANNOTATION_SOURCE_CHANGED"


class AnnotationStorageUnavailable(AnnotationError):
    code = "ANNOTATION_STORAGE_UNAVAILABLE"


class AnnotationService:
    def __init__(
        self,
        session: AsyncSession,
        results: ResultArtifactService,
        *,
        max_classes: int = 100,
        max_boxes_per_image: int = 200,
        max_boxes_per_project: int = 50_000,
    ) -> None:
        self.session = session
        self.results = results
        self.max_classes = max_classes
        self.max_boxes_per_image = max_boxes_per_image
        self.max_boxes_per_project = max_boxes_per_project

    async def get_or_create(self, job_id: UUID) -> tuple[AnnotationProject, bool]:
        job, manifest = await self.results.annotation_source(job_id)
        if (
            JobStatus(job.status) is not JobStatus.SUCCEEDED
            or SourceType(job.source_type) is not SourceType.IMAGE_DATASET
        ):
            raise AnnotationNotAvailable
        existing = await self._project(job_id, manifest.run_token)
        if existing:
            return existing, False
        now = datetime.now(UTC)
        project = AnnotationProject(
            id=uuid4(),
            job_id=job_id,
            result_run_token=manifest.run_token,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        images = [
            AnnotationImage(
                project_id=project.id,
                image_index=image.index,
                image_filename=image.filename,
                image_sha256=image.sha256,
                yolo_sha256=image.yolo_sha256,
                completed=False,
                updated_at=now,
            )
            for image in manifest.images
            if image.quality_category in {"normal", "challenging"}
            and image.object_key is not None
            and image.yolo_object_key is not None
            and image.output_width == 640
            and image.output_height == 640
        ]
        try:
            self.session.add(project)
            await self.session.flush()
            self.session.add_all(images)
            await self.session.commit()
            return project, True
        except IntegrityError:
            await self.session.rollback()
            winner = await self._project(job_id, manifest.run_token)
            if winner is None:
                raise AnnotationNotAvailable from None
            return winner, False

    async def get(self, job_id: UUID) -> AnnotationProject:
        job, manifest = await self.results.annotation_source(job_id)
        if SourceType(job.source_type) is not SourceType.IMAGE_DATASET:
            raise AnnotationNotAvailable
        project = await self._project(job_id, manifest.run_token)
        if project is None:
            raise AnnotationNotAvailable
        return project

    async def response(
        self, project: AnnotationProject, page: int, page_size: int
    ) -> AnnotationProjectResponse:
        classes = list(
            (
                await self.session.scalars(
                    select(AnnotationClass)
                    .where(AnnotationClass.project_id == project.id)
                    .order_by(AnnotationClass.yolo_index)
                )
            ).all()
        )
        total = int(
            await self.session.scalar(
                select(func.count())
                .select_from(AnnotationImage)
                .where(AnnotationImage.project_id == project.id)
            )
            or 0
        )
        rows = (
            await self.session.execute(
                select(AnnotationImage, func.count(AnnotationBox.id))
                .outerjoin(
                    AnnotationBox,
                    (AnnotationBox.project_id == AnnotationImage.project_id)
                    & (AnnotationBox.image_index == AnnotationImage.image_index),
                )
                .where(AnnotationImage.project_id == project.id)
                .group_by(AnnotationImage.project_id, AnnotationImage.image_index)
                .order_by(AnnotationImage.image_index)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return AnnotationProjectResponse(
            id=project.id,
            job_id=project.job_id,
            revision=project.revision,
            classes=[self._class_response(item) for item in classes],
            images=[
                AnnotationImageSummary(
                    index=image.image_index,
                    filename=image.image_filename,
                    completed=image.completed,
                    box_count=count,
                    preview_url=f"/api/v1/jobs/{project.job_id}/annotations/images/{image.image_index}/preview",
                )
                for image, count in rows
            ],
            page=page,
            page_size=page_size,
            total_images=total,
            limits=AnnotationLimits(
                max_classes=self.max_classes,
                max_boxes_per_image=self.max_boxes_per_image,
                max_boxes_per_project=self.max_boxes_per_project,
            ),
        )

    async def create_class(
        self, project: AnnotationProject, request: CreateAnnotationClassRequest
    ) -> AnnotationClassMutationResponse:
        await self._cas(project, request.expected_revision)
        count = int(
            await self.session.scalar(
                select(func.count())
                .select_from(AnnotationClass)
                .where(AnnotationClass.project_id == project.id)
            )
            or 0
        )
        if count >= self.max_classes:
            await self.session.rollback()
            raise AnnotationLimitExceeded
        highest = await self.session.scalar(
            select(func.max(AnnotationClass.yolo_index)).where(
                AnnotationClass.project_id == project.id
            )
        )
        now = datetime.now(UTC)
        item = AnnotationClass(
            id=uuid4(),
            project_id=project.id,
            yolo_index=0 if highest is None else highest + 1,
            name=request.name,
            normalized_name=self._name_key(request.name),
            color=request.color.upper(),
            created_at=now,
            updated_at=now,
        )
        self.session.add(item)
        try:
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            raise AnnotationClassInvalid from error
        return AnnotationClassMutationResponse(
            revision=project.revision, annotation_class=self._class_response(item)
        )

    async def update_class(
        self,
        project: AnnotationProject,
        class_id: UUID,
        request: UpdateAnnotationClassRequest,
    ) -> AnnotationClassMutationResponse:
        item = await self._class(project.id, class_id)
        await self._cas(project, request.expected_revision)
        item.name = request.name
        item.normalized_name = self._name_key(request.name)
        item.color = request.color.upper()
        item.updated_at = datetime.now(UTC)
        try:
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            raise AnnotationClassInvalid from error
        return AnnotationClassMutationResponse(
            revision=project.revision, annotation_class=self._class_response(item)
        )

    async def delete_class(
        self, project: AnnotationProject, class_id: UUID, expected_revision: int
    ) -> AnnotationClassMutationResponse:
        item = await self._class(project.id, class_id)
        used = await self.session.scalar(
            select(func.count())
            .select_from(AnnotationBox)
            .where(
                AnnotationBox.project_id == project.id,
                AnnotationBox.class_id == item.id,
            )
        )
        if used:
            raise AnnotationClassInUse
        highest = await self.session.scalar(
            select(func.max(AnnotationClass.yolo_index)).where(
                AnnotationClass.project_id == project.id
            )
        )
        if item.yolo_index != highest:
            raise AnnotationClassOrderLocked
        await self._cas(project, expected_revision)
        await self.session.delete(item)
        await self.session.commit()
        return AnnotationClassMutationResponse(
            revision=project.revision, annotation_class=None
        )

    async def image(
        self, project: AnnotationProject, image_index: int
    ) -> ImageAnnotationsResponse:
        image = await self.session.get(AnnotationImage, (project.id, image_index))
        if image is None:
            raise AnnotationImageNotFound
        boxes = list(
            (
                await self.session.scalars(
                    select(AnnotationBox)
                    .where(
                        AnnotationBox.project_id == project.id,
                        AnnotationBox.image_index == image_index,
                    )
                    .order_by(AnnotationBox.id)
                )
            ).all()
        )
        return ImageAnnotationsResponse(
            project_revision=project.revision,
            image_index=image_index,
            completed=image.completed,
            boxes=[self._box_response(box) for box in boxes],
        )

    async def put_image(
        self,
        project: AnnotationProject,
        image_index: int,
        request: PutImageAnnotationsRequest,
    ) -> ImageAnnotationsResponse:
        image = await self.session.get(AnnotationImage, (project.id, image_index))
        if image is None:
            raise AnnotationImageNotFound
        if len(request.boxes) > self.max_boxes_per_image:
            raise AnnotationLimitExceeded
        class_ids = set(
            (
                await self.session.scalars(
                    select(AnnotationClass.id).where(
                        AnnotationClass.project_id == project.id
                    )
                )
            ).all()
        )
        if any(box.class_id not in class_ids for box in request.boxes):
            raise AnnotationClassInvalid
        existing = int(
            await self.session.scalar(
                select(func.count())
                .select_from(AnnotationBox)
                .where(AnnotationBox.project_id == project.id)
            )
            or 0
        )
        current = int(
            await self.session.scalar(
                select(func.count())
                .select_from(AnnotationBox)
                .where(
                    AnnotationBox.project_id == project.id,
                    AnnotationBox.image_index == image_index,
                )
            )
            or 0
        )
        if existing - current + len(request.boxes) > self.max_boxes_per_project:
            raise AnnotationLimitExceeded
        await self._cas(project, request.expected_revision)
        await self.session.execute(
            delete(AnnotationBox).where(
                AnnotationBox.project_id == project.id,
                AnnotationBox.image_index == image_index,
            )
        )
        now = datetime.now(UTC)
        image.completed = request.completed
        image.updated_at = now
        self.session.add_all(
            [
                AnnotationBox(
                    id=box.id,
                    project_id=project.id,
                    image_index=image_index,
                    class_id=box.class_id,
                    x_center=box.x_center,
                    y_center=box.y_center,
                    width=box.width,
                    height=box.height,
                    created_at=now,
                    updated_at=now,
                )
                for box in request.boxes
            ]
        )
        try:
            await self.session.commit()
        except IntegrityError as error:
            await self.session.rollback()
            raise AnnotationClassInvalid from error
        return await self.image(project, image_index)

    async def preview(
        self, project: AnnotationProject, image_index: int
    ) -> ObjectStream:
        image = await self.session.get(AnnotationImage, (project.id, image_index))
        if image is None:
            raise AnnotationImageNotFound
        try:
            return await self.results.dataset_yolo_preview(
                project.job_id, project.result_run_token, image_index
            )
        except Exception as error:
            from app.services.result_artifacts import (
                ArtifactNotFoundError,
                ResultUnavailableError,
            )

            if isinstance(error, ArtifactNotFoundError):
                raise AnnotationImageNotFound from error
            if isinstance(error, ResultUnavailableError):
                raise AnnotationSourceChanged from error
            raise

    async def _cas(self, project: AnnotationProject, expected: int) -> None:
        now = datetime.now(UTC)
        revision = await self.session.scalar(
            update(AnnotationProject)
            .where(
                AnnotationProject.id == project.id,
                AnnotationProject.revision == expected,
            )
            .values(revision=AnnotationProject.revision + 1, updated_at=now)
            .returning(AnnotationProject.revision)
        )
        if revision is None:
            await self.session.rollback()
            raise AnnotationRevisionConflict
        project.revision = revision
        project.updated_at = now

    async def _project(self, job_id: UUID, run_token: UUID) -> AnnotationProject | None:
        return await self.session.scalar(
            select(AnnotationProject).where(
                AnnotationProject.job_id == job_id,
                AnnotationProject.result_run_token == run_token,
            )
        )

    async def _class(self, project_id: UUID, class_id: UUID) -> AnnotationClass:
        item = await self.session.scalar(
            select(AnnotationClass).where(
                AnnotationClass.project_id == project_id, AnnotationClass.id == class_id
            )
        )
        if item is None:
            raise AnnotationClassInvalid
        return item

    @staticmethod
    def _name_key(value: str) -> str:
        return unicodedata.normalize("NFC", value.casefold())

    @staticmethod
    def _class_response(item: AnnotationClass) -> AnnotationClassResponse:
        return AnnotationClassResponse(
            id=item.id, yolo_index=item.yolo_index, name=item.name, color=item.color
        )

    @staticmethod
    def _box_response(box: AnnotationBox) -> AnnotationBoxResponse:
        return AnnotationBoxResponse(
            id=box.id,
            class_id=box.class_id,
            x_center=box.x_center,
            y_center=box.y_center,
            width=box.width,
            height=box.height,
        )
