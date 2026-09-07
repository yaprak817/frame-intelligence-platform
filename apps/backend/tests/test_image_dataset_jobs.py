from fastapi.testclient import TestClient

from app.domain.jobs import OutboxEventType


def test_multiple_image_upload_creates_durable_dataset_job(
    client: TestClient, repository, storage
) -> None:
    response = client.post(
        "/api/v1/jobs/image-dataset",
        headers={"Idempotency-Key": "dataset-request-001"},
        files=[
            ("files", ("one.jpg", b"jpeg-one", "image/jpeg")),
            ("files", ("two.png", b"png-two", "image/png")),
            ("files", ("three.webp", b"webp-three", "image/webp")),
        ],
    )
    assert response.status_code == 202
    job = next(iter(repository.jobs.values()))
    assert job.source_type == "IMAGE_DATASET"
    assert job.source_display == "3 görsel"
    assert job.source_reference["kind"] == "images"
    assert len(job.source_reference["items"]) == 3
    assert repository.outbox[0].event_type == OutboxEventType.PROCESS_IMAGE_DATASET_JOB
    assert all("one.jpg" not in key for key in storage.uploads)


def test_zip_upload_is_mutually_exclusive(client: TestClient) -> None:
    response = client.post(
        "/api/v1/jobs/image-dataset",
        headers={"Idempotency-Key": "dataset-request-002"},
        files=[
            ("files", ("one.jpg", b"jpeg", "image/jpeg")),
            ("archive", ("images.zip", b"zip", "application/zip")),
        ],
    )
    assert response.status_code == 422


def test_zip_upload_contract_and_idempotency(
    client: TestClient, repository, storage
) -> None:
    request = {
        "headers": {"Idempotency-Key": "dataset-request-003"},
        "files": {"archive": ("images.zip", b"archive", "application/zip")},
    }
    first = client.post("/api/v1/jobs/image-dataset", **request)
    second = client.post("/api/v1/jobs/image-dataset", **request)
    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    assert len(repository.jobs) == 1
    assert len(storage.deletes) == 1


def test_dataset_rejects_unsupported_type(client: TestClient) -> None:
    response = client.post(
        "/api/v1/jobs/image-dataset",
        headers={"Idempotency-Key": "dataset-request-004"},
        files={"files": ("payload.svg", b"<svg/>", "image/svg+xml")},
    )
    assert response.status_code == 415


def test_same_idempotency_key_with_different_image_bytes_conflicts(
    client: TestClient,
) -> None:
    headers = {"Idempotency-Key": "dataset-content-conflict"}
    first = client.post(
        "/api/v1/jobs/image-dataset",
        headers=headers,
        files={"files": ("same.jpg", b"first", "image/jpeg")},
    )
    conflict = client.post(
        "/api/v1/jobs/image-dataset",
        headers=headers,
        files={"files": ("same.jpg", b"other", "image/jpeg")},
    )
    assert first.status_code == 202
    assert conflict.status_code == 409
