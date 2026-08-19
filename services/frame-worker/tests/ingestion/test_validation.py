import json
from subprocess import CompletedProcess

import pytest

import frame_worker.ingestion.validation as validation_module
from frame_worker.ingestion.errors import InvalidVideoSourceError
from frame_worker.ingestion.validation import VideoFileValidator


def test_ffprobe_invalid_video_is_rejected(tmp_path, monkeypatch) -> None:
    ffprobe = tmp_path / "ffprobe.exe"
    ffprobe.write_bytes(b"")
    video = tmp_path / "not-really-video.mp4"
    video.write_bytes(b"fake")
    monkeypatch.setattr(
        validation_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(
            args,
            0,
            json.dumps({"streams": []}),
            "",
        ),
    )

    validator = VideoFileValidator(ffprobe)

    with pytest.raises(InvalidVideoSourceError, match="video stream"):
        validator.validate(video, 1024)


def test_ffprobe_valid_metadata_is_accepted(tmp_path, monkeypatch) -> None:
    ffprobe = tmp_path / "ffprobe.exe"
    ffprobe.write_bytes(b"")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake")
    payload = {
        "streams": [{"avg_frame_rate": "25/1", "width": 1920, "height": 1080}],
        "format": {"duration": "10.5"},
    }
    monkeypatch.setattr(
        validation_module.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args, 0, json.dumps(payload), ""),
    )

    VideoFileValidator(ffprobe).validate(video, 1024)
