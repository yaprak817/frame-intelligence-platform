import asyncio

import pytest

from app.schemas.jobs import ProcessingConfigRequest
from app.services.job_service import IdempotencyConflictError, JobService


def test_job_and_minimal_outbox_are_created_atomically(repository, cipher) -> None:
    service = JobService(repository, cipher)
    raw_url = "https://example.com/watch?token=private"

    job, created = asyncio.run(
        service.create_url_job(raw_url, ProcessingConfigRequest(), "request-key-0001")
    )

    assert created is True
    assert job.source_display == "https://example.com/watch"
    assert raw_url not in job.source_secret
    assert repository.jobs == {job.id: job}
    assert len(repository.outbox) == 1
    event = repository.outbox[0]
    assert event.payload == {"job_id": str(job.id)}
    assert raw_url not in str(event.payload)
    assert set(event.payload) == {"job_id"}


def test_same_idempotent_request_reuses_job_and_outbox(repository, cipher) -> None:
    service = JobService(repository, cipher)
    arguments = (
        "https://example.com/watch?token=private",
        ProcessingConfigRequest(candidate_fps=4),
        "request-key-0002",
    )

    first, first_created = asyncio.run(service.create_url_job(*arguments))
    second, second_created = asyncio.run(service.create_url_job(*arguments))

    assert first_created is True
    assert second_created is False
    assert second.id == first.id
    assert len(repository.outbox) == 1


def test_same_key_with_different_payload_conflicts(repository, cipher) -> None:
    service = JobService(repository, cipher)
    asyncio.run(
        service.create_url_job(
            "https://example.com/one", ProcessingConfigRequest(), "shared-key-01"
        )
    )

    with pytest.raises(IdempotencyConflictError):
        asyncio.run(
            service.create_url_job(
                "https://example.com/two",
                ProcessingConfigRequest(),
                "shared-key-01",
            )
        )


def test_failed_atomic_write_leaves_no_job_or_outbox(repository, cipher) -> None:
    repository.fail_write = True
    service = JobService(repository, cipher)

    with pytest.raises(RuntimeError, match="simulated transaction failure"):
        asyncio.run(
            service.create_url_job(
                "https://example.com/video",
                ProcessingConfigRequest(),
                "request-key-0003",
            )
        )

    assert repository.jobs == {}
    assert repository.outbox == []
