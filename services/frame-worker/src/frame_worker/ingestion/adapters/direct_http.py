import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit
from uuid import uuid4

import httpx

from frame_worker.ingestion.adapters.base import AdapterMatch
from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.errors import (
    UnsupportedVideoSourceError,
    VideoDownloadError,
    VideoTooLargeError,
)
from frame_worker.ingestion.models import AcquiredVideo, URLSourceRequest
from frame_worker.ingestion.security import safe_url_label, validate_safe_http_url
from frame_worker.ingestion.validation import ALLOWED_VIDEO_SUFFIXES

MEDIA_TYPE_SUFFIXES = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "video/x-msvideo": ".avi",
}
REDIRECT_STATUS_CODES = {301, 302, 303, 307, 308}


class DirectHTTPVideoAdapter:
    name = "direct-http"

    def __init__(
        self,
        client: Any | None = None,
        url_validator=validate_safe_http_url,
        clock=time.monotonic,
    ) -> None:
        self._client = client
        self._url_validator = url_validator
        self._clock = clock

    def match(self, request: URLSourceRequest) -> AdapterMatch:
        parsed = urlsplit(request.url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return AdapterMatch.NO_MATCH
        suffix = Path(unquote(parsed.path)).suffix.lower()
        if suffix in ALLOWED_VIDEO_SUFFIXES:
            return AdapterMatch.DEFINITE
        return AdapterMatch.POSSIBLE

    def supports(self, request: URLSourceRequest) -> bool:
        return self.match(request) is not AdapterMatch.NO_MATCH

    def acquire(
        self,
        request: URLSourceRequest,
        workspace: Path,
        config: IngestionConfig,
    ) -> AcquiredVideo:
        if self._client is not None:
            return self._download(self._client, request, workspace, config)

        timeout = httpx.Timeout(
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
            write=config.read_timeout_seconds,
            pool=config.connect_timeout_seconds,
        )
        with httpx.Client(verify=True, trust_env=False, timeout=timeout) as client:
            return self._download(client, request, workspace, config)

    def _download(
        self,
        client: Any,
        request: URLSourceRequest,
        workspace: Path,
        config: IngestionConfig,
    ) -> AcquiredVideo:
        current_url = request.url
        started_at = self._clock()
        for redirect_count in range(config.max_redirects + 1):
            self._url_validator(current_url)
            try:
                with client.stream(
                    "GET",
                    current_url,
                    follow_redirects=False,
                ) as response:
                    if response.status_code in REDIRECT_STATUS_CODES:
                        location = response.headers.get("location")
                        if not location:
                            raise VideoDownloadError(
                                "Video URL redirect did not include a location"
                            )
                        if redirect_count >= config.max_redirects:
                            raise VideoDownloadError(
                                "Video URL exceeded redirect limit"
                            )
                        current_url = urljoin(current_url, location)
                        self._url_validator(current_url)
                        continue

                    if response.status_code in {401, 403} and self.match(
                        URLSourceRequest(current_url)
                    ) is AdapterMatch.POSSIBLE:
                        raise UnsupportedVideoSourceError(
                            "Ambiguous URL was not confirmed as direct video media"
                        )
                    if response.status_code < 200 or response.status_code >= 300:
                        raise VideoDownloadError(
                            "Video download failed for "
                            f"{safe_url_label(current_url)} with HTTP status "
                            f"{response.status_code}"
                        )
                    return self._write_response(
                        response,
                        current_url,
                        workspace,
                        config,
                        started_at,
                    )
            except httpx.TimeoutException as error:
                raise VideoDownloadError(
                    f"Video download timed out for {safe_url_label(current_url)}"
                ) from error
            except httpx.HTTPError as error:
                raise VideoDownloadError(
                    f"Video download failed for {safe_url_label(current_url)}"
                ) from error
        raise VideoDownloadError("Video URL exceeded redirect limit")

    def _write_response(
        self,
        response: Any,
        current_url: str,
        workspace: Path,
        config: IngestionConfig,
        started_at: float,
    ) -> AcquiredVideo:
        media_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if media_type in {"text/html", "application/xhtml+xml"}:
            raise UnsupportedVideoSourceError(
                f"URL host {safe_url_label(current_url)} returned an HTML page, "
                "not a direct video"
            )
        content_encoding = response.headers.get("content-encoding", "identity").lower()
        if content_encoding not in {"", "identity"}:
            raise VideoDownloadError("Encoded HTTP video responses are not supported")

        suffix = Path(unquote(urlsplit(current_url).path)).suffix.lower()
        if suffix not in ALLOWED_VIDEO_SUFFIXES:
            suffix = MEDIA_TYPE_SUFFIXES.get(media_type, "")
        if not suffix:
            raise UnsupportedVideoSourceError(
                f"URL host {safe_url_label(current_url)} did not return a direct video"
            )

        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError as error:
                raise VideoDownloadError(
                    "Video response has invalid Content-Length"
                ) from error
            if declared_size > config.max_download_bytes:
                raise VideoTooLargeError(
                    f"Video download exceeds the {config.max_download_bytes} byte limit"
                )

        workspace.mkdir(parents=True, exist_ok=True)
        stem = uuid4().hex
        part_path = workspace / f"{stem}{suffix}.part"
        final_path = workspace / f"{stem}{suffix}"
        downloaded = 0
        try:
            with part_path.open("xb") as output:
                for chunk in response.iter_bytes(config.chunk_size_bytes):
                    if self._clock() - started_at > config.total_timeout_seconds:
                        raise VideoDownloadError(
                            "Video download exceeded total timeout"
                        )
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > config.max_download_bytes:
                        raise VideoTooLargeError(
                            "Video download exceeded the configured byte limit"
                        )
                    output.write(chunk)
            if downloaded == 0:
                raise VideoDownloadError("Video download returned an empty response")
            part_path.replace(final_path)
        except BaseException:
            part_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
            raise

        original_name = Path(unquote(urlsplit(current_url).path)).name or None
        return AcquiredVideo(final_path, media_type or None, original_name)
