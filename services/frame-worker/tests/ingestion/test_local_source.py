from pathlib import Path

import pytest

from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.errors import (
    InvalidVideoSourceError,
    VideoTooLargeError,
)
from frame_worker.ingestion.local import LocalUploadedVideoSource
from frame_worker.ingestion.validation import validate_file_basics


class BasicValidator:
    def validate(self, path: Path, max_bytes: int) -> None:
        validate_file_basics(path, max_bytes)


def test_local_file_is_preserved(tmp_path) -> None:
    path = tmp_path / "video.mp4"
    path.write_bytes(b"video")
    source = LocalUploadedVideoSource(path, validator=BasicValidator())

    with source.materialize() as video:
        assert video.path == path.resolve()
        assert video.temporary is False

    assert path.exists()


def test_owned_local_file_is_cleaned_up(tmp_path) -> None:
    path = tmp_path / "upload.mp4"
    path.write_bytes(b"video")
    source = LocalUploadedVideoSource(
        path,
        owns_file=True,
        validator=BasicValidator(),
    )

    with source.materialize() as video:
        assert video.path.exists()

    assert not path.exists()


def test_missing_local_file_is_rejected(tmp_path) -> None:
    source = LocalUploadedVideoSource(
        tmp_path / "missing.mp4",
        validator=BasicValidator(),
    )

    with pytest.raises(InvalidVideoSourceError, match="does not exist"):
        with source.materialize():
            pass


def test_zero_byte_local_file_is_rejected(tmp_path) -> None:
    path = tmp_path / "empty.mp4"
    path.touch()
    source = LocalUploadedVideoSource(path, validator=BasicValidator())

    with pytest.raises(InvalidVideoSourceError, match="empty"):
        with source.materialize():
            pass


def test_oversized_local_file_is_rejected(tmp_path) -> None:
    path = tmp_path / "large.mp4"
    path.write_bytes(b"12345")
    source = LocalUploadedVideoSource(
        path,
        config=IngestionConfig(max_download_bytes=4),
        validator=BasicValidator(),
    )

    with pytest.raises(VideoTooLargeError):
        with source.materialize():
            pass


def test_unicode_and_space_local_path_is_supported(tmp_path) -> None:
    path = tmp_path / "Fenerbahçe maç videosu.mp4"
    path.write_bytes(b"video")
    source = LocalUploadedVideoSource(path, validator=BasicValidator())

    with source.materialize() as video:
        assert video.path.name == "Fenerbahçe maç videosu.mp4"
