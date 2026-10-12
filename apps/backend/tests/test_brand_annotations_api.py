import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response

from app.api.routes.brand_annotations import (
    create_brand_annotation_project,
    get_brand_annotation_project,
)
from app.models.brands import Brand


class Session:
    def __init__(self, brand):
        self.brand = brand

    async def get(self, model, identifier):
        if model is Brand and self.brand is not None and self.brand.id == identifier:
            return self.brand
        return None


class Service:
    def __init__(self):
        self.create_calls = 0
        self.get_calls = 0

    async def get_or_create_brand(self, brand_id):
        self.create_calls += 1
        return SimpleNamespace(id=brand_id), True

    async def get_brand(self, brand_id):
        self.get_calls += 1
        return SimpleNamespace(id=brand_id)

    async def response(self, project, page, page_size):
        return {
            "id": project.id,
            "page": page,
            "page_size": page_size,
        }


def test_owned_brand_annotation_project_is_accessible():
    user = SimpleNamespace(id=uuid4())
    brand = SimpleNamespace(id=uuid4(), owner_user_id=user.id)
    service = Service()
    response = Response()

    result = asyncio.run(
        create_brand_annotation_project(
            brand.id,
            response,
            service,
            Session(brand),
            user,
            1,
            50,
        )
    )

    assert service.create_calls == 1
    assert response.status_code == 201
    assert result["id"] == brand.id


@pytest.mark.parametrize("operation", ["create", "get"])
def test_other_users_brand_annotation_project_is_hidden(operation):
    user = SimpleNamespace(id=uuid4())
    brand = SimpleNamespace(id=uuid4(), owner_user_id=uuid4())
    service = Service()

    with pytest.raises(HTTPException) as error:
        if operation == "create":
            asyncio.run(
                create_brand_annotation_project(
                    brand.id,
                    Response(),
                    service,
                    Session(brand),
                    user,
                    1,
                    50,
                )
            )
        else:
            asyncio.run(
                get_brand_annotation_project(
                    brand.id,
                    service,
                    Session(brand),
                    user,
                    1,
                    50,
                )
            )

    assert error.value.status_code == 404
    assert service.create_calls == 0
    assert service.get_calls == 0
