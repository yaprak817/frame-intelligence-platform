import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from frame_worker.ingestion.adapters.base import AdapterMatch
from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.errors import (
    InvalidVideoSourceError,
    UnsupportedVideoSourceError,
    VideoDownloadError,
    VideoTooLargeError,
)
from frame_worker.ingestion.models import AcquiredVideo, URLSourceRequest
from frame_worker.ingestion.security import safe_url_label, validate_safe_http_url
from frame_worker.ingestion.validation import ALLOWED_VIDEO_SUFFIXES

PROGRESS_PREFIX = "FRAME_WORKER_PROGRESS:"
FILE_PREFIX = "FRAME_WORKER_FILE:"
MAX_ERROR_LENGTH = 500


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str
    exceeded_size: bool = False
    timed_out: bool = False


class ProcessRunner(Protocol):
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
        max_bytes: int,
        terminate_grace_seconds: float,
    ) -> ProcessResult: ...


class SubprocessRunner:
    def run(
        self,
        command: list[str],
        timeout_seconds: float,
        max_bytes: int,
        terminate_grace_seconds: float,
    ) -> ProcessResult:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        exceeded_size = threading.Event()

        def read_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                stdout_lines.append(line)
                if line.startswith(PROGRESS_PREFIX):
                    value = line.removeprefix(PROGRESS_PREFIX).strip()
                    try:
                        if int(float(value)) > max_bytes:
                            exceeded_size.set()
                    except ValueError:
                        continue

        def read_stderr() -> None:
            assert process.stderr is not None
            stderr_lines.extend(process.stderr)

        readers = [
            threading.Thread(target=read_stdout, daemon=True),
            threading.Thread(target=read_stderr, daemon=True),
        ]
        for reader in readers:
            reader.start()

        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        while process.poll() is None:
            if exceeded_size.is_set():
                self._stop(process, terminate_grace_seconds)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._stop(process, terminate_grace_seconds)
                break
            time.sleep(0.05)
        for reader in readers:
            reader.join(timeout=terminate_grace_seconds)
        return ProcessResult(
            process.returncode if process.returncode is not None else -1,
            "".join(stdout_lines),
            "".join(stderr_lines),
            exceeded_size.is_set(),
            timed_out,
        )

    @staticmethod
    def _stop(process: subprocess.Popen[str], grace_seconds: float) -> None:
        process.terminate()
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class YtDlpURLAdapter:
    name = "yt-dlp"

    def __init__(
        self,
        runner: ProcessRunner | None = None,
        url_validator=validate_safe_http_url,
        ffmpeg_binary: str | Path | None = None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._url_validator = url_validator
        self._ffmpeg_binary = self._resolve_optional_ffmpeg(ffmpeg_binary)

    def match(self, request: URLSourceRequest) -> AdapterMatch:
        if urlsplit(request.url).scheme.lower() not in {"http", "https"}:
            return AdapterMatch.NO_MATCH
        return AdapterMatch.POSSIBLE

    def supports(self, request: URLSourceRequest) -> bool:
        return self.match(request) is not AdapterMatch.NO_MATCH

    def acquire(
        self,
        request: URLSourceRequest,
        workspace: Path,
        config: IngestionConfig,
    ) -> AcquiredVideo:
        self._url_validator(request.url)
        workspace = workspace.resolve()
        workspace.mkdir(parents=True, exist_ok=True)
        command = self._build_command(request.url, workspace, config)
        try:
            result = self._runner.run(
                command,
                config.total_timeout_seconds,
                config.max_download_bytes,
                config.subprocess_terminate_grace_seconds,
            )
            if result.exceeded_size:
                raise VideoTooLargeError(
                    "Platform video exceeded the configured byte limit"
                )
            if result.timed_out:
                raise VideoDownloadError(
                    f"Platform download timed out for {safe_url_label(request.url)}"
                )
            if result.returncode != 0:
                self._raise_process_error(result.stderr, request.url)
            path = self._select_output(result.stdout, workspace)
            if path.stat().st_size > config.max_download_bytes:
                raise VideoTooLargeError(
                    "Platform video exceeded the configured byte limit"
                )
            return AcquiredVideo(path, None, path.name)
        except BaseException:
            self._clean_workspace(workspace)
            raise

    def _build_command(
        self,
        url: str,
        workspace: Path,
        config: IngestionConfig,
    ) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-config",
            "--no-playlist",
            "--no-cookies-from-browser",
            "--no-cache-dir",
            "--no-write-subs",
            "--no-write-auto-subs",
            "--no-write-thumbnail",
            "--no-write-info-json",
            "--no-write-comments",
            "--socket-timeout",
            str(config.yt_dlp_socket_timeout_seconds),
            "--max-filesize",
            str(config.max_download_bytes),
            "--newline",
            "--progress-template",
            f"download:{PROGRESS_PREFIX}%(progress.downloaded_bytes)s",
            "--print",
            f"after_move:{FILE_PREFIX}%(filepath)s",
            "--paths",
            str(workspace),
            "--output",
            "source.%(ext)s",
            "--format",
            "bestvideo*+bestaudio/best",
        ]
        if self._ffmpeg_binary is not None:
            command.extend(["--ffmpeg-location", self._ffmpeg_binary])
        command.extend(["--", url])
        return command

    def _select_output(self, stdout: str, workspace: Path) -> Path:
        reported = [
            Path(line.removeprefix(FILE_PREFIX).strip())
            for line in stdout.splitlines()
            if line.startswith(FILE_PREFIX)
        ]
        raw_candidates = [
            path
            for path in reported
            if path.suffix.lower() in ALLOWED_VIDEO_SUFFIXES and path.exists()
        ]
        raw_candidates.extend(
            path
            for path in workspace.iterdir()
            if path.is_file() and path.suffix.lower() in ALLOWED_VIDEO_SUFFIXES
        )
        candidates: set[Path] = set()
        for raw_path in raw_candidates:
            if raw_path.is_symlink():
                raise InvalidVideoSourceError(
                    "yt-dlp returned a path outside its workspace"
                )
            path = raw_path.resolve()
            if not path.is_relative_to(workspace):
                raise InvalidVideoSourceError(
                    "yt-dlp returned a path outside its workspace"
                )
            candidates.add(path)
        if len(candidates) != 1:
            raise InvalidVideoSourceError(
                "yt-dlp did not produce exactly one final video file"
            )
        return candidates.pop()

    @staticmethod
    def _raise_process_error(stderr: str, url: str) -> None:
        message = stderr.lower()
        if "playlist" in message:
            raise UnsupportedVideoSourceError("Playlist URLs are not supported")
        if any(value in message for value in ("login", "cookies", "authentication")):
            raise UnsupportedVideoSourceError(
                "This video source requires authentication, which is not supported"
            )
        if "unsupported url" in message or "no suitable extractor" in message:
            raise UnsupportedVideoSourceError(
                f"No platform adapter supports host {safe_url_label(url)}"
            )
        if "ffmpeg" in message and any(
            value in message for value in ("not found", "not installed")
        ):
            raise VideoDownloadError(
                "This platform video requires FFmpeg; set FFMPEG_BINARY or add "
                "ffmpeg to PATH"
            )
        sanitized = YtDlpURLAdapter._sanitize_error(stderr, url)
        raise VideoDownloadError(
            f"Platform video download failed for {safe_url_label(url)}: {sanitized}"
        )

    @staticmethod
    def _sanitize_error(stderr: str, url: str) -> str:
        sanitized = stderr.replace(url, "[redacted-url]")
        parsed = urlsplit(url)
        if parsed.query:
            sanitized = sanitized.replace(parsed.query, "[redacted-query]")
        sanitized = re.sub(
            r"(?i)\b(token|cookie|password|authorization)\s*[:=]\s*\S+",
            r"\1=[redacted]",
            sanitized,
        )
        compact = " ".join(sanitized.split())
        return compact[:MAX_ERROR_LENGTH] or "No safe error details were available"

    @staticmethod
    def _resolve_optional_ffmpeg(value: str | Path | None) -> str | None:
        configured = value or os.environ.get("FFMPEG_BINARY")
        if configured:
            path = Path(configured).expanduser()
            return str(path) if path.is_file() else None
        return shutil.which("ffmpeg")

    @staticmethod
    def _clean_workspace(workspace: Path) -> None:
        for path in workspace.iterdir():
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                shutil.rmtree(path)
