from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from app.domain.jobs import FailureCode, JobStatus
from app.models.processing_job import ProcessingJob

VALID_HEADERS = {"Idempotency-Key": "browser-request-0001"}


def test_create_url_job_returns_202_without_raw_url(
    client: TestClient,
    repository,
) -> None:
    raw_url = "https://example.com/video-page?token=super-secret"

    response = client.post(
        "/api/v1/jobs/url",
        headers=VALID_HEADERS,
        json={
            "url": raw_url,
            "processing": {
                "candidate_fps": 5,
                "selection_window_seconds": 1.0,
            },
        },
    )

    assert response.status_code == 202
    payload = response.json()
    UUID(payload["job_id"])
    assert payload["status"] == "PENDING_DISPATCH"
    assert response.headers["location"] == payload["status_url"]
    assert "super-secret" not in response.text
    job = next(iter(repository.jobs.values()))
    assert raw_url not in job.source_secret


def test_same_request_returns_same_job(client: TestClient, repository) -> None:
    request = {"url": "https://example.com/video?token=value"}

    first = client.post("/api/v1/jobs/url", headers=VALID_HEADERS, json=request)
    second = client.post("/api/v1/jobs/url", headers=VALID_HEADERS, json=request)

    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert len(repository.jobs) == 1
    assert len(repository.outbox) == 1


def test_same_key_different_request_returns_conflict(client: TestClient) -> None:
    first = client.post(
        "/api/v1/jobs/url",
        headers=VALID_HEADERS,
        json={"url": "https://example.com/one"},
    )
    second = client.post(
        "/api/v1/jobs/url",
        headers=VALID_HEADERS,
        json={"url": "https://example.com/two"},
    )

    assert first.status_code == 202
    assert second.status_code == 409


@pytest.mark.parametrize(
    ("url", "expected_fragment"),
    [
        ("ftp://example.com/video", "scheme"),
        ("https://user:password@example.com/video", "credentials"),
        ("not a url", "scheme"),
        ("https://", "host"),
    ],
)
def test_invalid_url_is_rejected(
    client: TestClient,
    url: str,
    expected_fragment: str,
) -> None:
    response = client.post("/api/v1/jobs/url", headers=VALID_HEADERS, json={"url": url})

    assert response.status_code == 422
    assert expected_fragment in response.text


@pytest.mark.parametrize(
    "processing",
    [
        {"candidate_fps": 0},
        {"candidate_fps": 61},
        {"selection_window_seconds": 0},
        {"selection_window_seconds": 3601},
    ],
)
def test_invalid_processing_config_is_rejected(
    client: TestClient,
    processing: dict[str, float],
) -> None:
    response = client.post(
        "/api/v1/jobs/url",
        headers=VALID_HEADERS,
        json={"url": "https://example.com/video", "processing": processing},
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "headers",
    [{}, {"Idempotency-Key": "short"}, {"Idempotency-Key": "invalid key"}],
)
def test_missing_or_invalid_idempotency_key_is_rejected(
    client: TestClient,
    headers: dict[str, str],
) -> None:
    response = client.post(
        "/api/v1/jobs/url",
        headers=headers,
        json={"url": "https://example.com/video"},
    )

    assert response.status_code == 422


def test_get_existing_pending_job(client: TestClient) -> None:
    created = client.post(
        "/api/v1/jobs/url",
        headers=VALID_HEADERS,
        json={"url": "https://example.com/watch?token=secret"},
    )

    response = client.get(created.json()["status_url"])

    assert response.status_code == 200
    assert response.json() == {
        "id": created.json()["job_id"],
        "status": "PENDING_DISPATCH",
        "source_type": "URL",
        "source": "https://example.com/watch",
        "created_at": response.json()["created_at"],
        "started_at": None,
        "completed_at": None,
        "failure": None,
        "result": None,
    }
    assert "secret" not in response.text


@pytest.mark.parametrize("job_id", [str(uuid4()), "not-a-uuid"])
def test_unknown_or_invalid_job_id_returns_404(client: TestClient, job_id: str) -> None:
    response = client.get(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Job not found"}


def test_failed_job_returns_only_safe_failure(
    client: TestClient,
    repository,
) -> None:
    job_id = uuid4()
    now = datetime.now(UTC)
    repository.jobs[job_id] = ProcessingJob(
        id=job_id,
        status=JobStatus.FAILED,
        source_type="URL",
        source_display="https://example.com/video",
        source_secret="protected",
        processing_config={},
        created_at=now,
        started_at=now,
        completed_at=now,
        failure_code=FailureCode.DOWNLOAD_TIMEOUT,
        failure_message="The video source timed out.",
        attempt_count=3,
        idempotency_scope="POST:/api/v1/jobs/url",
        idempotency_key="failed-request-01",
        request_fingerprint="0" * 64,
        result_reference=None,
        version=3,
    )

    response = client.get(f"/api/v1/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["failure"] == {
        "code": "DOWNLOAD_TIMEOUT",
        "message": "The video source timed out.",
    }
    assert "protected" not in response.text
