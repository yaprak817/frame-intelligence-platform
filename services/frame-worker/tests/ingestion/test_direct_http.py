import httpx
import pytest

from frame_worker.ingestion.adapters.direct_http import DirectHTTPVideoAdapter
from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.errors import (
    UnsafeVideoURLError,
    UnsupportedVideoSourceError,
    VideoDownloadError,
    VideoTooLargeError,
)
from frame_worker.ingestion.models import URLSourceRequest
from frame_worker.ingestion.security import validate_safe_http_url


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes | BaseException] | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self.chunks = chunks or []

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        return None

    def iter_bytes(self, _chunk_size: int):
        for chunk in self.chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield chunk


class FakeClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.urls = []

    def stream(self, _method: str, url: str, **_kwargs):
        self.urls.append(url)
        return self.responses.pop(0)


def safe_adapter(responses: list[FakeResponse]) -> DirectHTTPVideoAdapter:
    return DirectHTTPVideoAdapter(
        client=FakeClient(responses),
        url_validator=lambda _url: None,
    )


def test_valid_direct_http_video_downloads_in_chunks(tmp_path) -> None:
    adapter = safe_adapter(
        [
            FakeResponse(
                headers={"content-type": "video/mp4", "content-length": "6"},
                chunks=[b"abc", b"def"],
            )
        ]
    )

    acquired = adapter.acquire(
        URLSourceRequest("https://cdn.example/futbol maç.mp4"),
        tmp_path,
        IngestionConfig(max_download_bytes=10, chunk_size_bytes=3),
    )

    assert acquired.path.read_bytes() == b"abcdef"
    assert acquired.path.suffix == ".mp4"
    assert not list(tmp_path.glob("*.part"))


def test_content_length_over_limit_is_rejected(tmp_path) -> None:
    adapter = safe_adapter(
        [FakeResponse(headers={"content-type": "video/mp4", "content-length": "11"})]
    )

    with pytest.raises(VideoTooLargeError):
        adapter.acquire(
            URLSourceRequest("https://cdn.example/video.mp4"),
            tmp_path,
            IngestionConfig(max_download_bytes=10),
        )


def test_streaming_size_limit_removes_partial_file(tmp_path) -> None:
    adapter = safe_adapter(
        [FakeResponse(headers={"content-type": "video/mp4"}, chunks=[b"123", b"456"])]
    )

    with pytest.raises(VideoTooLargeError):
        adapter.acquire(
            URLSourceRequest("https://cdn.example/video.mp4"),
            tmp_path,
            IngestionConfig(max_download_bytes=5),
        )

    assert not list(tmp_path.iterdir())


def test_html_response_is_not_accepted_as_video(tmp_path) -> None:
    adapter = safe_adapter(
        [FakeResponse(headers={"content-type": "text/html"}, chunks=[b"<html>"])]
    )

    with pytest.raises(UnsupportedVideoSourceError, match="HTML"):
        adapter.acquire(
            URLSourceRequest("https://platform.example/watch/123"),
            tmp_path,
            IngestionConfig(),
        )


def test_timeout_is_wrapped_and_partial_file_is_removed(tmp_path) -> None:
    timeout = httpx.ReadTimeout("read timed out")
    adapter = safe_adapter(
        [
            FakeResponse(
                headers={"content-type": "video/mp4"},
                chunks=[b"partial", timeout],
            )
        ]
    )

    with pytest.raises(VideoDownloadError, match="timed out"):
        adapter.acquire(
            URLSourceRequest("https://cdn.example/video.mp4"),
            tmp_path,
            IngestionConfig(),
        )

    assert not list(tmp_path.iterdir())


def test_redirect_to_private_ip_is_blocked(tmp_path) -> None:
    client = FakeClient(
        [FakeResponse(status_code=302, headers={"location": "http://127.0.0.1/v.mp4"})]
    )

    def validator(url: str) -> None:
        validate_safe_http_url(
            url,
            lambda hostname, _port: [
                hostname if hostname == "127.0.0.1" else "93.184.216.34"
            ],
        )

    adapter = DirectHTTPVideoAdapter(client=client, url_validator=validator)

    with pytest.raises(UnsafeVideoURLError):
        adapter.acquire(
            URLSourceRequest("https://cdn.example/video.mp4"),
            tmp_path,
            IngestionConfig(),
        )


def test_direct_media_suffix_is_a_definite_match() -> None:
    adapter = DirectHTTPVideoAdapter()

    assert adapter.match(URLSourceRequest("https://cdn.example/video.mp4")).name == (
        "DEFINITE"
    )


def test_platform_page_is_only_a_possible_direct_match() -> None:
    adapter = DirectHTTPVideoAdapter()

    assert adapter.match(URLSourceRequest("https://platform.example/watch/1")).name == (
        "POSSIBLE"
    )


def test_ambiguous_403_allows_platform_fallback(tmp_path) -> None:
    adapter = safe_adapter([FakeResponse(status_code=403)])

    with pytest.raises(UnsupportedVideoSourceError, match="not confirmed"):
        adapter.acquire(
            URLSourceRequest("https://platform.example/watch/1"),
            tmp_path,
            IngestionConfig(),
        )


def test_definite_direct_403_remains_download_error(tmp_path) -> None:
    adapter = safe_adapter([FakeResponse(status_code=403)])

    with pytest.raises(VideoDownloadError, match="HTTP status 403"):
        adapter.acquire(
            URLSourceRequest("https://cdn.example/video.mp4"),
            tmp_path,
            IngestionConfig(),
        )
