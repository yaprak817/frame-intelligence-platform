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
from app.models.brands import Brand, BrandClass, BrandDataset
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
from app.schemas.artifacts import StoredDatasetManifestV1
from app.services.result_artifacts import (
    FailedResultUnavailableError,
    ResultArtifactService,
    ResultJobNotFoundError,
    ResultNotReadyError,
)
from app.storage.s3 import ObjectStream

MAX_BRAND_DATASETS = 100
MAX_BRAND_IMAGES = 1000
MAX_BRAND_CLASSES = 100


class AnnotationError(RuntimeError):
    code = "ANNOTATION_NOT_AVAILABLE"


class AnnotationNotAvailable(AnnotationError):
    pass


class AnnotationBrandConflict(AnnotationError):
    code = "ANNOTATION_BRAND_CONFLICT"


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
        if JobStatus(job.status) is not JobStatus.SUCCEEDED or SourceType(
            job.source_type
        ) not in {SourceType.IMAGE_DATASET, SourceType.URL, SourceType.UPLOAD}:
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
        source_images = (
            [
                (image.index, image.filename, image.sha256, image.yolo_sha256)
                for image in manifest.images
                if image.quality_category in {"normal", "challenging"}
                and image.object_key is not None
                and image.yolo_object_key is not None
                and image.output_width == 640
                and image.output_height == 640
            ]
            if isinstance(manifest, StoredDatasetManifestV1)
            else [
                (frame.index, frame.filename, frame.sha256, frame.sha256)
                for frame in manifest.frames
            ]
        )
        images = [
            AnnotationImage(
                project_id=project.id,
                image_index=index,
                image_filename=filename,
                image_sha256=image_sha256,
                yolo_sha256=preview_sha256,
                completed=False,
                updated_at=now,
            )
            for index, filename, image_sha256, preview_sha256 in source_images
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

    async def get_or_create_brand(
        self, brand_id: UUID
    ) -> tuple[AnnotationProject, bool]:
        brand = await self.session.get(Brand, brand_id)
        if brand is None:
            raise AnnotationNotAvailable

        datasets = list(
            (
                await self.session.scalars(
                    select(BrandDataset)
                    .where(
                        BrandDataset.brand_id == brand_id,
                        BrandDataset.job_id.is_not(None),
                    )
                    .order_by(BrandDataset.created_at.asc())
                    .limit(MAX_BRAND_DATASETS + 1)
                )
            ).all()
        )
        if len(datasets) > MAX_BRAND_DATASETS:
            raise AnnotationLimitExceeded

        sources: list[tuple[UUID, object]] = []
        for dataset in datasets:
            if dataset.job_id is None:
                continue
            try:
                _job, manifest = await self.results.annotation_source(dataset.job_id)
            except (
                ResultJobNotFoundError,
                ResultNotReadyError,
                FailedResultUnavailableError,
            ):
                continue
            sources.append((dataset.job_id, manifest))

        if not sources:
            raise AnnotationNotAvailable

        project = await self.session.scalar(
            select(AnnotationProject).where(AnnotationProject.brand_id == brand_id)
        )
        created = False

        if project is None:
            candidates: list[AnnotationProject] = []
            seen_ids: set[UUID] = set()
            for source_job_id, manifest in sources:
                candidate = await self._project(source_job_id, manifest.run_token)
                if candidate is not None and candidate.brand_id not in {None, brand_id}:
                    raise AnnotationBrandConflict
                if candidate is not None and candidate.id not in seen_ids:
                    candidates.append(candidate)
                    seen_ids.add(candidate.id)

            if len(candidates) > 1:
                raise AnnotationNotAvailable

            if candidates:
                project = candidates[0]
            else:
                project, created = await self.get_or_create(sources[0][0])

            project = await self.session.scalar(
                select(AnnotationProject)
                .where(AnnotationProject.id == project.id)
                .with_for_update()
            )
            if project is None or project.brand_id not in {None, brand_id}:
                raise AnnotationBrandConflict
            project.brand_id = brand_id

        rows = list(
            (
                await self.session.scalars(
                    select(AnnotationImage)
                    .where(AnnotationImage.project_id == project.id)
                    .order_by(AnnotationImage.image_index)
                    .limit(MAX_BRAND_IMAGES + 1)
                )
            ).all()
        )
        if len(rows) > MAX_BRAND_IMAGES:
            raise AnnotationLimitExceeded

        for image in rows:
            if image.source_job_id is None:
                image.source_job_id = project.job_id
                image.source_result_run_token = project.result_run_token
                image.source_image_index = image.image_index

        existing_sources = {
            (
                image.source_job_id,
                image.source_result_run_token,
                image.source_image_index,
            )
            for image in rows
            if image.source_job_id is not None
            and image.source_result_run_token is not None
            and image.source_image_index is not None
        }
        next_index = max((image.image_index for image in rows), default=-1) + 1
        now = datetime.now(UTC)

        additions: list[AnnotationImage] = []
        for source_job_id, manifest in sources:
            for (
                source_index,
                filename,
                image_sha256,
                preview_sha256,
            ) in self._manifest_images(manifest):
                source_key = (source_job_id, manifest.run_token, source_index)
                if source_key in existing_sources:
                    continue
                additions.append(
                    AnnotationImage(
                        project_id=project.id,
                        image_index=next_index,
                        source_job_id=source_job_id,
                        source_result_run_token=manifest.run_token,
                        source_image_index=source_index,
                        image_filename=filename,
                        image_sha256=image_sha256,
                        yolo_sha256=preview_sha256,
                        completed=False,
                        updated_at=now,
                    )
                )
                existing_sources.add(source_key)
                next_index += 1
                if len(rows) + len(additions) > MAX_BRAND_IMAGES:
                    raise AnnotationLimitExceeded

        if additions:
            self.session.add_all(additions)

        brand_classes = list(
            (
                await self.session.scalars(
                    select(BrandClass)
                    .where(BrandClass.brand_id == brand_id)
                    .order_by(BrandClass.created_at.asc())
                    .limit(MAX_BRAND_CLASSES + 1)
                )
            ).all()
        )
        if len(brand_classes) > MAX_BRAND_CLASSES:
            raise AnnotationLimitExceeded
        project_classes = list(
            (
                await self.session.scalars(
                    select(AnnotationClass)
                    .where(AnnotationClass.project_id == project.id)
                    .order_by(AnnotationClass.yolo_index)
                )
            ).all()
        )
        class_names = {item.normalized_name for item in project_classes}
        next_yolo_index = (
            max((item.yolo_index for item in project_classes), default=-1) + 1
        )

        for brand_class in brand_classes:
            normalized = self._name_key(brand_class.name)
            if normalized in class_names:
                continue
            self.session.add(
                AnnotationClass(
                    id=uuid4(),
                    project_id=project.id,
                    yolo_index=next_yolo_index,
                    name=brand_class.name,
                    normalized_name=normalized,
                    color=brand_class.color.upper(),
                    created_at=now,
                    updated_at=now,
                )
            )
            class_names.add(normalized)
            next_yolo_index += 1

        project.updated_at = now
        await self.session.commit()
        return project, created

    async def get_brand(self, brand_id: UUID) -> AnnotationProject:
        project = await self.session.scalar(
            select(AnnotationProject).where(AnnotationProject.brand_id == brand_id)
        )
        if project is None:
            raise AnnotationNotAvailable
        return project

    async def get(self, job_id: UUID) -> AnnotationProject:
        job, manifest = await self.results.annotation_source(job_id)
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

        if project.brand_id is None:
            _job, manifest = await self.results.annotation_source(project.job_id)
            if manifest.run_token != project.result_run_token:
                raise AnnotationSourceChanged
            metadata = (
                {
                    image.index: (image.output_width, image.output_height, None)
                    for image in manifest.images
                    if image.quality_category in {"normal", "challenging"}
                    and image.object_key is not None
                    and image.yolo_object_key is not None
                    and image.output_width == 640
                    and image.output_height == 640
                }
                if isinstance(manifest, StoredDatasetManifestV1)
                else {
                    frame.index: (frame.width, frame.height, frame.timestamp_ms)
                    for frame in manifest.frames
                }
            )
            if any(image.image_index not in metadata for image, _count in rows):
                raise AnnotationSourceChanged
        else:
            metadata: dict[int, tuple[int, int, int | None]] = {}
            manifest_cache: dict[UUID, object] = {}

            for image, _count in rows:
                source_job_id = image.source_job_id
                source_run_token = image.source_result_run_token
                source_index = image.source_image_index
                if (
                    source_job_id is None
                    or source_run_token is None
                    or source_index is None
                ):
                    raise AnnotationSourceChanged

                manifest = manifest_cache.get(source_job_id)
                if manifest is None:
                    _job, manifest = await self.results.annotation_source(source_job_id)
                    manifest_cache[source_job_id] = manifest

                if manifest.run_token != source_run_token:
                    raise AnnotationSourceChanged

                if isinstance(manifest, StoredDatasetManifestV1):
                    source_item = next(
                        (
                            item
                            for item in manifest.images
                            if item.index == source_index
                            and item.quality_category in {"normal", "challenging"}
                            and item.object_key is not None
                            and item.yolo_object_key is not None
                            and item.output_width == 640
                            and item.output_height == 640
                        ),
                        None,
                    )
                    if source_item is None:
                        raise AnnotationSourceChanged
                    metadata[image.image_index] = (
                        source_item.output_width,
                        source_item.output_height,
                        None,
                    )
                else:
                    source_item = next(
                        (
                            item
                            for item in manifest.frames
                            if item.index == source_index
                        ),
                        None,
                    )
                    if source_item is None:
                        raise AnnotationSourceChanged
                    metadata[image.image_index] = (
                        source_item.width,
                        source_item.height,
                        source_item.timestamp_ms,
                    )

        return AnnotationProjectResponse(
            id=project.id,
            job_id=project.job_id,
            revision=project.revision,
            classes=[self._class_response(item) for item in classes],
            images=[
                AnnotationImageSummary(
                    index=image.image_index,
                    filename=image.image_filename,
                    width=metadata[image.image_index][0],
                    height=metadata[image.image_index][1],
                    timestamp_ms=metadata[image.image_index][2],
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

        source_job_id = image.source_job_id or project.job_id
        source_run_token = image.source_result_run_token or project.result_run_token
        source_image_index = (
            image.source_image_index
            if image.source_image_index is not None
            else image_index
        )

        try:
            return await self.results.annotation_preview(
                source_job_id, source_run_token, source_image_index
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

    @staticmethod
    def _manifest_images(manifest):
        if isinstance(manifest, StoredDatasetManifestV1):
            return [
                (image.index, image.filename, image.sha256, image.yolo_sha256)
                for image in manifest.images
                if image.quality_category in {"normal", "challenging"}
                and image.object_key is not None
                and image.yolo_object_key is not None
                and image.output_width == 640
                and image.output_height == 640
            ]
        return [
            (frame.index, frame.filename, frame.sha256, frame.sha256)
            for frame in manifest.frames
        ]

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
