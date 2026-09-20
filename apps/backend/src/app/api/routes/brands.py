from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.models.annotations import AnnotationProject
from app.models.brands import Brand, BrandClass, BrandDataset
from app.schemas.brands import (
    BrandClassCreate,
    BrandClassView,
    BrandCreate,
    BrandDatasetAttach,
    BrandDatasetView,
    BrandDetail,
    BrandSummary,
    CanonicalUUID,
)
from app.services.annotations import MAX_BRAND_CLASSES, MAX_BRAND_DATASETS

router = APIRouter(prefix="/brands")
Session = Annotated[AsyncSession, Depends(get_session)]


def _constraint_name(error: IntegrityError) -> str | None:
    return getattr(getattr(error.orig, "diag", None), "constraint_name", None)


def _normalize(value: str) -> str:
    return " ".join(value.strip().casefold().split())


async def _brand_or_404(session: AsyncSession, brand_id: UUID) -> Brand:
    brand = await session.get(Brand, brand_id)
    if brand is None:
        raise HTTPException(status_code=404, detail="Marka bulunamadı.")
    return brand


@router.get("", response_model=list[BrandSummary])
async def list_brands(
    session: Session,
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> list[BrandSummary]:
    brands = (
        await session.scalars(
            select(Brand)
            .order_by(Brand.updated_at.desc(), Brand.name.asc(), Brand.id)
            .offset(offset)
            .limit(limit)
        )
    ).all()

    result: list[BrandSummary] = []
    for brand in brands:
        class_count = await session.scalar(
            select(func.count())
            .select_from(BrandClass)
            .where(BrandClass.brand_id == brand.id)
        )
        dataset_count = await session.scalar(
            select(func.count())
            .select_from(BrandDataset)
            .where(BrandDataset.brand_id == brand.id)
        )
        result.append(
            BrandSummary(
                id=brand.id,
                name=brand.name,
                status=brand.status,
                class_count=int(class_count or 0),
                dataset_count=int(dataset_count or 0),
                updated_at=brand.updated_at,
            )
        )
    return result


@router.post("", response_model=BrandSummary, status_code=status.HTTP_201_CREATED)
async def create_brand(
    payload: BrandCreate,
    session: Session,
) -> BrandSummary:
    name = " ".join(payload.name.strip().split())
    normalized = _normalize(name)
    if not normalized:
        raise HTTPException(status_code=422, detail="Marka adı boş olamaz.")

    now = datetime.now(timezone.utc)
    brand = Brand(
        id=uuid4(),
        name=name,
        normalized_name=normalized,
        status="DRAFT",
        created_at=now,
        updated_at=now,
    )
    session.add(brand)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _constraint_name(exc) != "uq_brands_normalized_name":
            raise
        raise HTTPException(status_code=409, detail="Bu marka zaten mevcut.") from exc

    return BrandSummary(
        id=brand.id,
        name=brand.name,
        status=brand.status,
        class_count=0,
        dataset_count=0,
        updated_at=brand.updated_at,
    )


@router.get("/{brand_id}", response_model=BrandDetail)
async def get_brand(
    brand_id: CanonicalUUID,
    session: Session,
    class_limit: int = Query(default=100, ge=1, le=100),
    dataset_limit: int = Query(default=100, ge=1, le=100),
) -> BrandDetail:
    brand = await _brand_or_404(session, brand_id)
    classes = (
        await session.scalars(
            select(BrandClass)
            .where(BrandClass.brand_id == brand.id)
            .order_by(BrandClass.created_at.asc(), BrandClass.id)
            .limit(class_limit)
        )
    ).all()
    datasets = (
        await session.scalars(
            select(BrandDataset)
            .where(BrandDataset.brand_id == brand.id)
            .order_by(BrandDataset.created_at.asc(), BrandDataset.id)
            .limit(dataset_limit)
        )
    ).all()

    return BrandDetail(
        id=brand.id,
        name=brand.name,
        status=brand.status,
        created_at=brand.created_at,
        updated_at=brand.updated_at,
        classes=[
            BrandClassView(id=item.id, name=item.name, color=item.color)
            for item in classes
        ],
        datasets=[
            BrandDatasetView(
                id=item.id,
                name=item.name,
                job_id=item.job_id,
                annotation_project_id=item.annotation_project_id,
            )
            for item in datasets
        ],
    )


@router.post(
    "/{brand_id}/classes",
    response_model=BrandClassView,
    status_code=status.HTTP_201_CREATED,
)
async def create_brand_class(
    brand_id: CanonicalUUID,
    payload: BrandClassCreate,
    session: Session,
) -> BrandClassView:
    brand = await _brand_or_404(session, brand_id)
    class_count = await session.scalar(
        select(func.count())
        .select_from(BrandClass)
        .where(BrandClass.brand_id == brand.id)
    )
    if int(class_count or 0) >= MAX_BRAND_CLASSES:
        raise HTTPException(status_code=409, detail="Sınıf sınırına ulaşıldı.")
    name = " ".join(payload.name.strip().split())
    normalized = _normalize(name)
    if not normalized:
        raise HTTPException(status_code=422, detail="Sınıf adı boş olamaz.")

    now = datetime.now(timezone.utc)
    item = BrandClass(
        id=uuid4(),
        brand_id=brand.id,
        name=name,
        normalized_name=normalized,
        color=payload.color.upper(),
        created_at=now,
        updated_at=now,
    )
    brand.updated_at = now
    session.add(item)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if _constraint_name(exc) != "uq_brand_classes_brand_normalized_name":
            raise
        raise HTTPException(
            status_code=409,
            detail="Bu sınıf bu markada zaten mevcut.",
        ) from exc

    return BrandClassView(id=item.id, name=item.name, color=item.color)


@router.post(
    "/{brand_id}/datasets",
    response_model=BrandDatasetView,
    status_code=status.HTTP_201_CREATED,
)
async def attach_brand_dataset(
    brand_id: CanonicalUUID,
    payload: BrandDatasetAttach,
    session: Session,
) -> BrandDatasetView:
    brand = await _brand_or_404(session, brand_id)
    name = " ".join(payload.name.strip().split())
    if not name:
        raise HTTPException(status_code=422, detail="Veri seti adı boş olamaz.")

    if payload.job_id is None and payload.annotation_project_id is None:
        raise HTTPException(status_code=422, detail="Veri seti kaynağı gerekli.")

    source_job_id = payload.job_id
    if payload.annotation_project_id is not None:
        project = await session.get(AnnotationProject, payload.annotation_project_id)
        if project is None or project.brand_id != brand.id:
            raise HTTPException(
                status_code=409, detail="Etiketleme alanı bu markaya ait değil."
            )
        if payload.job_id is not None and payload.job_id != project.job_id:
            raise HTTPException(status_code=409, detail="Veri seti kaynağı uyuşmuyor.")
        source_job_id = project.job_id

    if source_job_id is not None:
        owner = await session.scalar(
            select(AnnotationProject.brand_id)
            .where(
                AnnotationProject.job_id == source_job_id,
                AnnotationProject.brand_id.is_not(None),
                AnnotationProject.brand_id != brand.id,
            )
            .limit(1)
        )
        if owner is not None:
            raise HTTPException(status_code=409, detail="İş başka bir markaya ait.")
        existing = await session.scalar(
            select(BrandDataset).where(BrandDataset.job_id == source_job_id)
        )
        if existing is not None:
            if existing.brand_id != brand.id:
                raise HTTPException(status_code=409, detail="İş başka bir markaya ait.")
            return BrandDatasetView(
                id=existing.id,
                name=existing.name,
                job_id=existing.job_id,
                annotation_project_id=existing.annotation_project_id,
            )

    dataset_count = await session.scalar(
        select(func.count())
        .select_from(BrandDataset)
        .where(BrandDataset.brand_id == brand.id)
    )
    if int(dataset_count or 0) >= MAX_BRAND_DATASETS:
        raise HTTPException(status_code=409, detail="Veri seti sınırına ulaşıldı.")

    now = datetime.now(timezone.utc)
    item = BrandDataset(
        id=uuid4(),
        brand_id=brand.id,
        name=name,
        job_id=source_job_id,
        annotation_project_id=payload.annotation_project_id,
        created_at=now,
        updated_at=now,
    )
    brand.updated_at = now
    session.add(item)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        constraint = _constraint_name(exc)
        if constraint not in {
            "uq_brand_datasets_job",
            "uq_brand_datasets_annotation_project",
        }:
            raise
        if constraint == "uq_brand_datasets_job" and source_job_id is not None:
            winner = await session.scalar(
                select(BrandDataset).where(BrandDataset.job_id == source_job_id)
            )
            if winner is not None and winner.brand_id == brand.id:
                return BrandDatasetView(
                    id=winner.id,
                    name=winner.name,
                    job_id=winner.job_id,
                    annotation_project_id=winner.annotation_project_id,
                )
        raise HTTPException(
            status_code=409,
            detail="Bu veri seti başka bir kayıtla çakışıyor.",
        ) from exc

    return BrandDatasetView(
        id=item.id,
        name=item.name,
        job_id=item.job_id,
        annotation_project_id=item.annotation_project_id,
    )
