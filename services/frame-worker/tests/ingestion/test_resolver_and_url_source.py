from pathlib import Path

import pytest

from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.errors import (
    InvalidVideoSourceError,
    UnsupportedVideoSourceError,
)
from frame_worker.ingestion.models import AcquiredVideo, URLSourceRequest
from frame_worker.ingestion.resolver import SourceResolver
from frame_worker.ingestion.service import SourceProcessingService
from frame_worker.ingestion.url import GenericURLVideoSource


class AcceptingValidator:
    def validate(self, path: Path, _max_bytes: int) -> None:
        if not path.is_file():
            raise AssertionError("adapter did not create a file")


class FakeAdapter:
    name = "fake"

    def __init__(
        self,
        supported: bool = True,
        outside_path: Path | None = None,
    ) -> None:
        self.supported = supported
        self.outside_path = outside_path
        self.acquired_path: Path | None = None

    def supports(self, _request: URLSourceRequest) -> bool:
        return self.supported

    def acquire(self, _request, workspace, _config) -> AcquiredVideo:
        path = self.outside_path or workspace / "video.mp4"
        path.write_bytes(b"video")
        self.acquired_path = path
        return AcquiredVideo(path, "video/mp4", "video.mp4")


def test_resolver_uses_first_supporting_adapter(tmp_path) -> None:
    skipped = FakeAdapter(supported=False)
    selected = FakeAdapter()
    resolver = SourceResolver([skipped, selected])

    acquired = resolver.resolve(
        URLSourceRequest("https://cdn.example/video.mp4"),
        tmp_path,
        IngestionConfig(),
    )

    assert acquired.path == selected.acquired_path
    assert skipped.acquired_path is None


def test_resolver_reports_unsupported_url(tmp_path) -> None:
    resolver = SourceResolver([FakeAdapter(supported=False)])

    with pytest.raises(UnsupportedVideoSourceError):
        resolver.resolve(
            URLSourceRequest("ftp://example.com/video.mp4"),
            tmp_path,
            IngestionConfig(),
        )


def test_url_source_rejects_workspace_escape(tmp_path) -> None:
    outside = tmp_path / "outside.mp4"
    source = GenericURLVideoSource(
        "https://cdn.example/video.mp4",
        SourceResolver([FakeAdapter(outside_path=outside)]),
        validator=AcceptingValidator(),
    )

    with pytest.raises(InvalidVideoSourceError, match="outside"):
        with source.materialize():
            pass


def test_url_source_cleans_temp_file_after_context() -> None:
    adapter = FakeAdapter()
    source = GenericURLVideoSource(
        "https://cdn.example/video.mp4",
        SourceResolver([adapter]),
        validator=AcceptingValidator(),
    )

    with source.materialize() as video:
        materialized_path = video.path
        assert materialized_path.exists()

    assert not materialized_path.exists()


class FailingProcessor:
    def process(self, video_path: Path, _output_directory: Path):
        assert video_path.exists()
        raise RuntimeError("processing failed")


def test_processor_error_still_cleans_remote_temp(tmp_path) -> None:
    adapter = FakeAdapter()
    source = GenericURLVideoSource(
        "https://cdn.example/video.mp4",
        SourceResolver([adapter]),
        validator=AcceptingValidator(),
    )
    service = SourceProcessingService(FailingProcessor())

    with pytest.raises(RuntimeError, match="processing failed"):
        service.process(source, tmp_path / "output")

    assert adapter.acquired_path is not None
    assert not adapter.acquired_path.exists()
