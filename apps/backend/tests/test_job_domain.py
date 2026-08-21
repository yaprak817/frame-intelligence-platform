import pytest

from app.domain.jobs import JobStatus


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (JobStatus.PENDING_DISPATCH, JobStatus.QUEUED),
        (JobStatus.QUEUED, JobStatus.RUNNING),
        (JobStatus.RUNNING, JobStatus.SUCCEEDED),
        (JobStatus.RUNNING, JobStatus.FAILED),
        (JobStatus.RUNNING, JobStatus.QUEUED),
    ],
)
def test_allowed_job_transitions(source: JobStatus, target: JobStatus) -> None:
    assert source.can_transition_to(target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (JobStatus.PENDING_DISPATCH, JobStatus.RUNNING),
        (JobStatus.QUEUED, JobStatus.SUCCEEDED),
        (JobStatus.SUCCEEDED, JobStatus.RUNNING),
        (JobStatus.FAILED, JobStatus.QUEUED),
    ],
)
def test_disallowed_job_transitions(source: JobStatus, target: JobStatus) -> None:
    assert not source.can_transition_to(target)
