import sys
from pathlib import Path

import pytest

import frame_worker.ingestion.adapters.yt_dlp as yt_dlp_module
from frame_worker.ingestion.adapters.yt_dlp import (
    FILE_PREFIX,
    ProcessResult,
    SubprocessRunner,
    YtDlpURLAdapter,
)
from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.errors import (
    InvalidVideoSourceError,
    UnsupportedVideoSourceError,
    VideoDownloadError,
    VideoTooLargeError,
)
from frame_worker.ingestion.models import URLSourceRequest
from frame_worker.ingestion.resolver import SourceResolver
from frame_worker.ingestion.url import GenericURLVideoSource


class FakeRunner:
    def __init__(
        self,
        result: ProcessResult | None = None,
        files: tuple[str, ...] = ("source.mp4",),
        file_bytes: bytes = b"video",
    ) -> None:
        self.result = result
        self.files = files
        self.file_bytes = file_bytes
        self.command: list[str] | None = None
        self.arguments = None

    def run(self, command, timeout, max_bytes, grace) -> ProcessResult:
        self.command = command
        self.arguments = (timeout, max_bytes, grace)
        workspace = Path(command[command.index("--paths") + 1])
        paths = []
        for filename in self.files:
            path = workspace / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self.file_bytes)
            paths.append(path)
        if self.result is not None:
            return self.result
        stdout = "".join(f"{FILE_PREFIX}{path}\n" for path in paths)
        return ProcessResult(0, stdout, "")


def adapter_for(runner: FakeRunner, ffmpeg_binary=None) -> YtDlpURLAdapter:
    return YtDlpURLAdapter(
        runner=runner,
        url_validator=lambda _url: None,
        ffmpeg_binary=ffmpeg_binary,
    )


def test_command_uses_python_module_and_safe_flags(tmp_path) -> None:
    runner = FakeRunner()
    adapter = adapter_for(runner)
    url = "https://platform.example/Fenerbahçe maç?id=1"

    acquired = adapter.acquire(URLSourceRequest(url), tmp_path, IngestionConfig())

    assert acquired.path.name == "source.mp4"
    assert runner.command is not None
    assert runner.command[:3] == [sys.executable, "-m", "yt_dlp"]
    assert "--no-config" in runner.command
    assert "--no-playlist" in runner.command
    assert "--no-cookies-from-browser" in runner.command
    assert "--no-cache-dir" in runner.command
    assert "--max-filesize" in runner.command
    assert runner.command[-2:] == ["--", url]
    assert str(tmp_path.resolve()) in runner.command


def test_single_platform_video_is_acquired(tmp_path) -> None:
    adapter = adapter_for(FakeRunner(files=("source.webm",)))

    acquired = adapter.acquire(
        URLSourceRequest("https://platform.example/watch/1"),
        tmp_path,
        IngestionConfig(),
    )

    assert acquired.path == (tmp_path / "source.webm").resolve()


def test_playlist_is_rejected_and_workspace_cleaned(tmp_path) -> None:
    runner = FakeRunner(ProcessResult(1, "", "ERROR: playlist is not supported"))

    with pytest.raises(UnsupportedVideoSourceError, match="Playlist"):
        adapter_for(runner).acquire(
            URLSourceRequest("https://platform.example/playlist/1"),
            tmp_path,
            IngestionConfig(),
        )

    assert not list(tmp_path.iterdir())


def test_multiple_final_media_are_rejected(tmp_path) -> None:
    runner = FakeRunner(files=("source.mp4", "other.webm"))

    with pytest.raises(InvalidVideoSourceError, match="exactly one"):
        adapter_for(runner).acquire(
            URLSourceRequest("https://platform.example/watch/1"),
            tmp_path,
            IngestionConfig(),
        )


def test_reported_workspace_escape_is_rejected(tmp_path) -> None:
    outside = tmp_path.parent / "outside.mp4"
    outside.write_bytes(b"video")
    result = ProcessResult(0, f"{FILE_PREFIX}{outside}\n", "")

    with pytest.raises(InvalidVideoSourceError, match="outside"):
        adapter_for(FakeRunner(result=result, files=())).acquire(
            URLSourceRequest("https://platform.example/watch/1"),
            tmp_path,
            IngestionConfig(),
        )


def test_symlink_output_is_rejected(tmp_path, monkeypatch) -> None:
    link = tmp_path / "source.mp4"
    link.write_bytes(b"video")
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == link or original_is_symlink(path),
    )
    result = ProcessResult(0, f"{FILE_PREFIX}{link}\n", "")

    with pytest.raises(InvalidVideoSourceError, match="outside"):
        adapter_for(FakeRunner(result=result, files=())).acquire(
            URLSourceRequest("https://platform.example/watch/1"),
            tmp_path,
            IngestionConfig(),
        )


def test_runtime_size_overflow_is_classified_and_cleaned(tmp_path) -> None:
    result = ProcessResult(1, "", "", exceeded_size=True)

    with pytest.raises(VideoTooLargeError):
        adapter_for(FakeRunner(result=result)).acquire(
            URLSourceRequest("https://platform.example/watch/1"),
            tmp_path,
            IngestionConfig(max_download_bytes=4),
        )

    assert not list(tmp_path.iterdir())


def test_final_file_size_is_checked(tmp_path) -> None:
    runner = FakeRunner(file_bytes=b"too large")

    with pytest.raises(VideoTooLargeError):
        adapter_for(runner).acquire(
            URLSourceRequest("https://platform.example/watch/1"),
            tmp_path,
            IngestionConfig(max_download_bytes=4),
        )


def test_total_timeout_is_classified_and_cleaned(tmp_path) -> None:
    result = ProcessResult(1, "", "", timed_out=True)

    with pytest.raises(VideoDownloadError, match="timed out"):
        adapter_for(FakeRunner(result=result)).acquire(
            URLSourceRequest("https://platform.example/watch/1"),
            tmp_path,
            IngestionConfig(),
        )

    assert not list(tmp_path.iterdir())


def test_error_does_not_leak_url_query_or_token(tmp_path) -> None:
    url = "https://platform.example/watch?id=1&token=very-secret"
    result = ProcessResult(1, "", f"ERROR downloading {url}")

    with pytest.raises(VideoDownloadError) as captured:
        adapter_for(FakeRunner(result=result, files=())).acquire(
            URLSourceRequest(url),
            tmp_path,
            IngestionConfig(),
        )

    assert "very-secret" not in str(captured.value)
    assert "id=1" not in str(captured.value)


def test_error_redacts_credential_like_stderr_fields(tmp_path) -> None:
    result = ProcessResult(
        1,
        "",
        "ERROR token=secret cookie:session-value password=hunter2",
    )

    with pytest.raises(VideoDownloadError) as captured:
        adapter_for(FakeRunner(result=result, files=())).acquire(
            URLSourceRequest("https://platform.example/watch/1"),
            tmp_path,
            IngestionConfig(),
        )

    message = str(captured.value)
    assert "secret" not in message
    assert "session-value" not in message
    assert "hunter2" not in message


@pytest.mark.parametrize(
    ("stderr", "message"),
    [
        ("ERROR: Unsupported URL", "No platform adapter"),
        ("ERROR: login required; provide cookies", "requires authentication"),
    ],
)
def test_known_unsupported_errors_are_classified(tmp_path, stderr, message) -> None:
    runner = FakeRunner(ProcessResult(1, "", stderr), files=())

    with pytest.raises(UnsupportedVideoSourceError, match=message):
        adapter_for(runner).acquire(
            URLSourceRequest("https://platform.example/watch/1"),
            tmp_path,
            IngestionConfig(),
        )


def test_unknown_provider_failure_is_download_error(tmp_path) -> None:
    runner = FakeRunner(ProcessResult(1, "", "ERROR: remote server failed"), files=())

    with pytest.raises(VideoDownloadError, match="remote server failed"):
        adapter_for(runner).acquire(
            URLSourceRequest("https://platform.example/watch/1"),
            tmp_path,
            IngestionConfig(),
        )


def test_ffmpeg_path_is_passed_when_available(tmp_path) -> None:
    ffmpeg = tmp_path / "FFmpeg Unicode klasör" / "ffmpeg.exe"
    ffmpeg.parent.mkdir()
    ffmpeg.write_bytes(b"")
    runner = FakeRunner()

    adapter_for(runner, ffmpeg).acquire(
        URLSourceRequest("https://platform.example/watch/1"),
        tmp_path / "workspace",
        IngestionConfig(),
    )

    assert runner.command is not None
    index = runner.command.index("--ffmpeg-location")
    assert runner.command[index + 1] == str(ffmpeg)


def test_missing_ffmpeg_does_not_block_simple_acquisition(tmp_path) -> None:
    runner = FakeRunner()
    missing = tmp_path / "missing-ffmpeg.exe"

    acquired = adapter_for(runner, missing).acquire(
        URLSourceRequest("https://platform.example/watch/1"),
        tmp_path,
        IngestionConfig(),
    )

    assert acquired.path.exists()
    assert runner.command is not None
    assert "--ffmpeg-location" not in runner.command


def test_hls_or_dash_result_is_one_normalized_video(tmp_path) -> None:
    acquired = adapter_for(FakeRunner(files=("source.mkv",))).acquire(
        URLSourceRequest("https://platform.example/manifest/1"),
        tmp_path,
        IngestionConfig(),
    )

    assert acquired.path.suffix == ".mkv"


class AudioOnlyRejectingValidator:
    def validate(self, _path, _max_bytes) -> None:
        raise InvalidVideoSourceError("Source does not contain a video stream")


def test_audio_only_platform_result_is_rejected_by_common_validation() -> None:
    adapter = adapter_for(FakeRunner())
    source = GenericURLVideoSource(
        "https://platform.example/audio/1",
        SourceResolver([adapter]),
        validator=AudioOnlyRejectingValidator(),
    )

    with pytest.raises(InvalidVideoSourceError, match="video stream"):
        with source.materialize():
            pass


class FakePipeProcess:
    def __init__(self, running: bool = False, ignore_terminate: bool = False) -> None:
        self.stdout = iter(())
        self.stderr = iter(())
        self.returncode = None if running else 0
        self.running = running
        self.ignore_terminate = ignore_terminate
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        if not self.ignore_terminate:
            self.returncode = 1

    def wait(self, timeout=None):
        if self.returncode is None:
            raise yt_dlp_module.subprocess.TimeoutExpired("yt-dlp", timeout)
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = 1


def test_subprocess_runner_uses_shell_false(monkeypatch) -> None:
    captured = {}
    process = FakePipeProcess()

    def popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return process

    monkeypatch.setattr(yt_dlp_module.subprocess, "Popen", popen)

    result = SubprocessRunner().run(["python", "-m", "yt_dlp"], 10, 100, 0)

    assert result.returncode == 0
    assert captured["shell"] is False
    assert captured["command"] == ["python", "-m", "yt_dlp"]


def test_subprocess_timeout_terminates_process(monkeypatch) -> None:
    process = FakePipeProcess(running=True)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(yt_dlp_module.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(yt_dlp_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(yt_dlp_module.time, "sleep", lambda _seconds: None)

    result = SubprocessRunner().run(["python"], 1, 100, 0)

    assert result.timed_out is True
    assert process.terminated is True


def test_subprocess_timeout_kills_process_after_grace_period(monkeypatch) -> None:
    process = FakePipeProcess(running=True, ignore_terminate=True)
    ticks = iter([0.0, 2.0])
    monkeypatch.setattr(yt_dlp_module.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(yt_dlp_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(yt_dlp_module.time, "sleep", lambda _seconds: None)

    result = SubprocessRunner().run(["python"], 1, 100, 0.1)

    assert result.timed_out is True
    assert process.terminated is True
    assert process.killed is True
