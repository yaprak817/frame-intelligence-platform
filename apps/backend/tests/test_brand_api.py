import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api.routes.brands import (
    attach_brand_dataset,
    create_brand,
    create_brand_class,
    get_brand,
    list_brands,
)
from app.db.session import get_session
from app.main import app
from app.models.annotations import AnnotationProject
from app.models.brands import Brand, BrandDataset
from app.schemas.brands import BrandClassCreate, BrandCreate, BrandDatasetAttach
from app.services.annotation_training import (
    AnnotationTrainingService,
    AnnotationTrainingSourceChanged,
)
from app.services.annotations import AnnotationLimitExceeded, AnnotationService


class Rows:
    def __init__(self, items):
        self.items = items

    def all(self):
        return self.items


class Session:
    def __init__(self, brand=None, project=None, fail_commit=False, constraint=None):
        self.brand = brand
        self.project = project
        self.fail_commit = fail_commit
        self.constraint = constraint
        self.added = []
        self.rollbacks = 0

    async def get(self, model, identifier):
        if model is Brand and self.brand and self.brand.id == identifier:
            return self.brand
        if (
            model is AnnotationProject
            and self.project
            and self.project.id == identifier
        ):
            return self.project
        return None

    async def scalars(self, _query):
        return Rows([])

    async def scalar(self, _query):
        selected = _query.column_descriptions[0]["expr"]
        if selected is AnnotationProject.brand_id:
            return None
        if selected is BrandDataset:
            return None
        return 0

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        if self.fail_commit:
            cause = Exception("duplicate")
            cause.diag = SimpleNamespace(constraint_name=self.constraint)
            raise IntegrityError("duplicate", {}, cause)

    async def rollback(self):
        self.rollbacks += 1


def sample_brand():
    now = datetime.now(UTC)
    return Brand(
        id=uuid4(),
        name="One",
        normalized_name="one",
        status="DRAFT",
        created_at=now,
        updated_at=now,
    )


def test_brand_create_list_and_detail_are_public_safe():
    session = Session()
    created = asyncio.run(create_brand(BrandCreate(name="  Example  "), session))
    assert created.name == "Example"
    assert created.class_count == created.dataset_count == 0
    assert set(created.model_dump()) == {
        "id",
        "name",
        "status",
        "class_count",
        "dataset_count",
        "updated_at",
    }
    assert asyncio.run(list_brands(limit=10, offset=0, session=session)) == []
    brand = sample_brand()
    detail = asyncio.run(
        get_brand(brand.id, class_limit=10, dataset_limit=10, session=Session(brand))
    )
    assert detail.id == brand.id and detail.classes == detail.datasets == []


def test_duplicate_brand_rolls_back():
    session = Session(fail_commit=True, constraint="uq_brands_normalized_name")
    with pytest.raises(HTTPException) as error:
        asyncio.run(create_brand(BrandCreate(name="Example"), session))
    assert error.value.status_code == 409
    assert session.rollbacks == 1


def test_duplicate_class_and_dataset_roll_back():
    brand = sample_brand()
    class_session = Session(
        brand, fail_commit=True, constraint="uq_brand_classes_brand_normalized_name"
    )
    with pytest.raises(HTTPException) as class_error:
        asyncio.run(
            create_brand_class(
                brand.id, BrandClassCreate(name="Vehicle"), class_session
            )
        )
    assert class_error.value.status_code == 409
    assert class_session.rollbacks == 1

    dataset_session = Session(
        brand, fail_commit=True, constraint="uq_brand_datasets_job"
    )
    with pytest.raises(HTTPException) as dataset_error:
        asyncio.run(
            attach_brand_dataset(
                brand.id,
                BrandDatasetAttach(name="Frames", job_id=uuid4()),
                dataset_session,
            )
        )
    assert dataset_error.value.status_code == 409
    assert dataset_session.rollbacks == 1


def test_unrelated_integrity_error_is_not_mapped_to_conflict():
    session = Session(fail_commit=True, constraint="unexpected_foreign_key")
    with pytest.raises(IntegrityError):
        asyncio.run(create_brand(BrandCreate(name="Example"), session))
    assert session.rollbacks == 1


def test_brand_collection_caps_reject_new_class_and_dataset():
    brand = sample_brand()

    class FullSession(Session):
        async def scalar(self, _query):
            return 100

    session = FullSession(brand)
    with pytest.raises(HTTPException) as class_error:
        asyncio.run(
            create_brand_class(brand.id, BrandClassCreate(name="Vehicle"), session)
        )
    assert class_error.value.status_code == 409
    with pytest.raises(HTTPException) as dataset_error:
        asyncio.run(
            attach_brand_dataset(
                brand.id, BrandDatasetAttach(name="Frames", job_id=uuid4()), session
            )
        )
    assert dataset_error.value.status_code == 409
    assert session.added == []


def test_cross_brand_project_and_job_mismatch_cannot_attach():
    own_brand = sample_brand()
    other_brand = sample_brand()
    project = AnnotationProject(
        id=uuid4(),
        job_id=uuid4(),
        brand_id=other_brand.id,
        result_run_token=uuid4(),
        revision=0,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session = Session(own_brand, project)
    payload = BrandDatasetAttach(
        name="Dataset", job_id=project.job_id, annotation_project_id=project.id
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(attach_brand_dataset(own_brand.id, payload, session))
    assert error.value.status_code == 409
    assert session.added == []
    project.brand_id = own_brand.id
    payload.job_id = uuid4()
    with pytest.raises(HTTPException) as error:
        asyncio.run(attach_brand_dataset(own_brand.id, payload, session))
    assert error.value.status_code == 409


def test_dataset_requires_source_and_derives_project_job():
    brand = sample_brand()
    session = Session(brand)
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            attach_brand_dataset(brand.id, BrandDatasetAttach(name="Empty"), session)
        )
    assert error.value.status_code == 422

    project = AnnotationProject(
        id=uuid4(),
        job_id=uuid4(),
        brand_id=brand.id,
        result_run_token=uuid4(),
        revision=0,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.project = project
    result = asyncio.run(
        attach_brand_dataset(
            brand.id,
            BrandDatasetAttach(name="Owned", annotation_project_id=project.id),
            session,
        )
    )
    assert result.job_id == project.job_id


def test_brand_list_pagination_is_bounded_by_request_validation():
    async def session_override():
        yield Session()

    app.dependency_overrides[get_session] = session_override
    try:
        with TestClient(app) as client:
            assert client.get("/api/v1/brands?limit=101").status_code == 422
            assert client.get("/api/v1/brands?offset=-1").status_code == 422
            assert client.get("/api/v1/brands?limit=10&offset=0").json() == []
            assert (
                client.post(
                    "/api/v1/brands", json={"name": "ok", "hidden": "secret"}
                ).status_code
                == 422
            )
            assert (
                client.get(f"/api/v1/brands/{str(uuid4()).upper()}").status_code == 422
            )
    finally:
        app.dependency_overrides.clear()


def test_brand_workspace_rejects_unbounded_dataset_collection():
    brand = sample_brand()

    class TooManyDatasets(Session):
        async def scalars(self, _query):
            return Rows([object()] * 101)

    service = AnnotationService(TooManyDatasets(brand), results=None)
    with pytest.raises(AnnotationLimitExceeded):
        asyncio.run(service.get_or_create_brand(brand.id))


def test_existing_brand_workspace_is_returned_idempotently():
    brand = sample_brand()
    job_id = uuid4()
    run_token = uuid4()
    project = AnnotationProject(
        id=uuid4(),
        job_id=job_id,
        brand_id=brand.id,
        result_run_token=run_token,
        revision=0,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    class WorkspaceSession(Session):
        def __init__(self):
            super().__init__(brand)
            self.rows = iter([[SimpleNamespace(job_id=job_id)], [], [], []])
            self.commits = 0

        async def scalars(self, _query):
            return Rows(next(self.rows))

        async def scalar(self, _query):
            return project

        async def commit(self):
            self.commits += 1

    class Results:
        async def annotation_source(self, requested_job):
            assert requested_job == job_id
            return None, SimpleNamespace(run_token=run_token, frames=[])

    session = WorkspaceSession()
    returned, created = asyncio.run(
        AnnotationService(session, Results()).get_or_create_brand(brand.id)
    )
    assert returned is project and created is False and session.commits == 1


def test_brand_training_uses_each_attached_source_and_rejects_stale_run():
    first_job, second_job, first_token, second_token = (uuid4() for _ in range(4))
    project = SimpleNamespace(
        job_id=first_job, result_run_token=first_token, brand_id=uuid4()
    )

    def manifest(token, key):
        return SimpleNamespace(
            run_token=token,
            frames=[
                SimpleNamespace(
                    index=0,
                    filename="frame.jpg",
                    object_key=key,
                    size_bytes=3,
                    content_type="image/jpeg",
                    sha256="a" * 64,
                    width=2,
                    height=2,
                    timestamp_ms=0,
                )
            ],
        )

    anchor = manifest(first_token, "jobs/first/frame.jpg")
    other = manifest(second_token, "jobs/second/frame.jpg")

    class Results:
        async def annotation_source(self, job_id):
            assert job_id == second_job
            return None, other

    images = [
        SimpleNamespace(
            image_index=0,
            source_job_id=first_job,
            source_result_run_token=first_token,
            source_image_index=0,
        ),
        SimpleNamespace(
            image_index=1,
            source_job_id=second_job,
            source_result_run_token=second_token,
            source_image_index=0,
        ),
    ]
    service = AnnotationTrainingService(None, Results())
    metadata = asyncio.run(service._metadata_for_project(project, images, anchor))
    assert metadata[0][1] == "jobs/first/frame.jpg"
    assert metadata[1][1] == "jobs/second/frame.jpg"
    images[1].source_result_run_token = uuid4()
    with pytest.raises(AnnotationTrainingSourceChanged):
        asyncio.run(service._metadata_for_project(project, images, anchor))
