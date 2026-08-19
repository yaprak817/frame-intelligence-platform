import json
from pathlib import Path
from subprocess import CalledProcessError, CompletedProcess

import cv2
import numpy as np
import pytest

import frame_worker.extraction.ffmpeg as ffmpeg_module
from frame_worker.extraction.ffmpeg import (
    FFmpegConfigurationError,
    FFmpegExtractionError,
    FFmpegExtractor,
)


def test_extractor_uses_safe_argument_lists_for_unicode_paths(
    tmp_path,
    monkeypatch,
) -> None:
    binary_directory = tmp_path / "araçlar boşluk"
    binary_directory.mkdir()
    ffmpeg = binary_directory / "ffmpeg.exe"
    ffprobe = binary_directory / "ffprobe.exe"
    ffmpeg.write_bytes(b"")
    ffprobe.write_bytes(b"")
    video_path = tmp_path / "futbol maç videosu.mp4"
    video_path.write_bytes(b"fake")
    commands = []

    def fake_run(command, **kwargs):
        commands.append((command, kwargs))
        if command[0] == str(ffprobe):
            payload = {
                "streams": [
                    {"avg_frame_rate": "25/1", "nb_frames": "50", "duration": "2"}
                ],
                "format": {"duration": "2"},
            }
            return CompletedProcess(command, 0, json.dumps(payload), "")

        output_directory = Path(command[-1]).parent
        frame = np.full((8, 8, 3), 128, dtype=np.uint8)
        cv2.imwrite(str(output_directory / "candidate_000000001.png"), frame)
        return CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", fake_run)
    extractor = FFmpegExtractor(ffmpeg, ffprobe)

    with extractor.extract(video_path, 5.0) as extraction:
        frames = list(extraction)

    assert len(frames) == 1
    assert extraction.metadata.total_frames == 50
    assert commands[0][0][0] == str(ffprobe)
    assert commands[1][0][0] == str(ffmpeg)
    assert str(video_path) in commands[1][0]
    assert commands[1][0][1:7] == [
        "-v",
        "error",
        "-i",
        str(video_path),
        "-vf",
        "fps=5.0",
    ]
    assert "-vsync" not in commands[1][0]
    assert "shell" not in commands[0][1]
    assert "shell" not in commands[1][1]


def test_binaries_can_be_loaded_from_environment(tmp_path, monkeypatch) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"")
    ffprobe.write_bytes(b"")
    monkeypatch.setenv("FFMPEG_BINARY", str(ffmpeg))
    monkeypatch.setenv("FFPROBE_BINARY", str(ffprobe))

    extractor = FFmpegExtractor()

    assert extractor.ffmpeg_binary == str(ffmpeg)
    assert extractor.ffprobe_binary == str(ffprobe)


def test_missing_binary_has_clear_configuration_error(monkeypatch) -> None:
    monkeypatch.delenv("FFMPEG_BINARY", raising=False)
    monkeypatch.delenv("FFPROBE_BINARY", raising=False)
    monkeypatch.setattr(ffmpeg_module.shutil, "which", lambda _: None)

    with pytest.raises(FFmpegConfigurationError, match="FFMPEG_BINARY"):
        FFmpegExtractor()


def test_extraction_error_includes_decoded_ffmpeg_stderr(
    tmp_path,
    monkeypatch,
) -> None:
    ffmpeg = tmp_path / "ffmpeg.exe"
    ffprobe = tmp_path / "ffprobe.exe"
    ffmpeg.write_bytes(b"")
    ffprobe.write_bytes(b"")
    video_path = tmp_path / "maç.mp4"
    video_path.write_bytes(b"fake")

    def fake_run(command, **kwargs):
        if command[0] == str(ffprobe):
            payload = {
                "streams": [
                    {"avg_frame_rate": "25/1", "nb_frames": "50", "duration": "2"}
                ]
            }
            return CompletedProcess(command, 0, json.dumps(payload), "")
        raise CalledProcessError(
            returncode=2880417800,
            cmd=command,
            stderr="Unrecognized option 'vsync'. Hatalı seçenek.".encode(),
        )

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", fake_run)
    extractor = FFmpegExtractor(ffmpeg, ffprobe)

    with pytest.raises(FFmpegExtractionError) as error:
        extractor.extract(video_path, 5.0)

    message = str(error.value)
    assert "exit code 2880417800" in message
    assert "Unrecognized option 'vsync'" in message
    assert "Hatalı seçenek" in message
