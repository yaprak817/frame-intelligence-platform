import io
import json
from subprocess import CompletedProcess

import numpy as np
import pytest

import frame_worker.extraction.ffmpeg as ffmpeg_module
from frame_worker.extraction.ffmpeg import (
    FFmpegExtractionError,
    StreamingFFmpegExtractor,
)
from frame_worker.processing.pipeline import ProcessingConfig, VideoProcessor


class ChunkedReader(io.BytesIO):
    def __init__(self, value: bytes, chunk_size: int | None = None) -> None:
        super().__init__(value)
        self.chunk_size = chunk_size

    def read(self, size: int = -1) -> bytes:
        if self.chunk_size is not None and size > self.chunk_size:
            size = self.chunk_size
        return super().read(size)


class FakeProcess:
    def __init__(
        self,
        stdout: bytes,
        stderr: bytes = b"",
        return_code: int = 0,
        chunk_size: int | None = None,
    ) -> None:
        self.stdout = ChunkedReader(stdout, chunk_size)
        self.stderr = io.BytesIO(stderr)
        self.return_code = return_code
        self.terminated = False

    def wait(self) -> int:
        return self.return_code

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True


def make_extractor(tmp_path, monkeypatch, process: FakeProcess):
    binary_directory = tmp_path / "araçlar boşluk"
    binary_directory.mkdir()
    ffmpeg = binary_directory / "ffmpeg.exe"
    ffprobe = binary_directory / "ffprobe.exe"
    ffmpeg.write_bytes(b"")
    ffprobe.write_bytes(b"")
    commands = []

    def fake_run(command, **kwargs):
        payload = {
            "streams": [
                {
                    "avg_frame_rate": "25/1",
                    "nb_frames": "10",
                    "duration": "0.4",
                    "width": 2,
                    "height": 2,
                }
            ],
            "format": {"duration": "0.4"},
        }
        return CompletedProcess(command, 0, json.dumps(payload), "")

    def fake_popen(command, **kwargs):
        commands.append((command, kwargs))
        return process

    monkeypatch.setattr(ffmpeg_module.subprocess, "run", fake_run)
    monkeypatch.setattr(ffmpeg_module.subprocess, "Popen", fake_popen)
    return StreamingFFmpegExtractor(ffmpeg, ffprobe), commands


def test_streams_multiple_raw_frames_and_handles_partial_reads(
    tmp_path,
    monkeypatch,
) -> None:
    first = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
    second = np.arange(12, 24, dtype=np.uint8).reshape(2, 2, 3)
    process = FakeProcess(first.tobytes() + second.tobytes(), chunk_size=5)
    extractor, commands = make_extractor(tmp_path, monkeypatch, process)
    video_path = tmp_path / "Fenerbahçe maç videosu.mp4"
    video_path.write_bytes(b"fake")

    with extractor.extract(video_path, 5.0) as extraction:
        frames = list(extraction)

    assert len(frames) == 2
    np.testing.assert_array_equal(frames[0].frame, first)
    np.testing.assert_array_equal(frames[1].frame, second)
    assert frames[1].timestamp == 0.2
    command, kwargs = commands[0]
    assert command == [
        str(extractor.ffmpeg_binary),
        "-v",
        "error",
        "-i",
        str(video_path),
        "-vf",
        "fps=5.0",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "bgr24",
        "pipe:1",
    ]
    assert "shell" not in kwargs


def test_incomplete_raw_frame_raises_clear_error(tmp_path, monkeypatch) -> None:
    process = FakeProcess(b"only five"[:5], stderr=b"truncated stream")
    extractor, _ = make_extractor(tmp_path, monkeypatch, process)
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake")

    with extractor.extract(video_path, 5.0) as extraction:
        with pytest.raises(FFmpegExtractionError, match="expected 12 bytes"):
            list(extraction)


def test_nonzero_stream_exit_includes_stderr(tmp_path, monkeypatch) -> None:
    process = FakeProcess(b"", stderr="codec hatası".encode(), return_code=7)
    extractor, _ = make_extractor(tmp_path, monkeypatch, process)
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake")

    with extractor.extract(video_path, 5.0) as extraction:
        with pytest.raises(FFmpegExtractionError) as error:
            list(extraction)

    assert "exit code 7" in str(error.value)
    assert "codec hatası" in str(error.value)


def test_pipeline_uses_stream_without_temporary_pngs(tmp_path, monkeypatch) -> None:
    frame = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
    process = FakeProcess(frame.tobytes())
    extractor, _ = make_extractor(tmp_path, monkeypatch, process)
    video_path = tmp_path / "video.mp4"
    video_path.write_bytes(b"fake")

    def fail_if_temp_directory_is_created(*args, **kwargs):
        raise AssertionError("streaming must not create a temporary PNG directory")

    monkeypatch.setattr(
        ffmpeg_module.tempfile,
        "TemporaryDirectory",
        fail_if_temp_directory_is_created,
    )
    processor = VideoProcessor(
        ProcessingConfig(min_size=2),
        extractor=extractor,
    )

    summary = processor.process(video_path, tmp_path / "output")

    assert summary.candidate_frames == 1
    assert summary.selected_frames == 1
