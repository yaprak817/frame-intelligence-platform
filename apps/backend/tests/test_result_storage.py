import asyncio
from datetime import UTC, datetime, timedelta

import pytest

import app.storage.s3 as s3_module
from app.storage.s3 import (
    ObjectMetadata,
    ObjectStorageError,
    ObjectStream,
    ObjectTooLargeError,
    S3ResultObjectStorage,
)


class FakeBody:
    def __init__(self, payload: bytes = b"", error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.read_sizes: list[int] = []
        self.close_calls = 0

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if self.error is not None:
            raise self.error
        return self.payload

    def close(self) -> None:
        self.close_calls += 1


class FakeS3Client:
    def __init__(
        self,
        *,
        head_size: int = 4,
        get_size: int = 4,
        body: FakeBody | None = None,
    ) -> None:
        self.head_size = head_size
        self.get_size = get_size
        self.body = body or FakeBody(b"data")
        self.head_calls = 0
        self.get_calls = 0
        self.close_calls = 0

    def head_object(self, **_kwargs):
        self.head_calls += 1
        return {"ContentLength": self.head_size, "ContentType": "application/json"}

    def get_object(self, **_kwargs):
        self.get_calls += 1
        return {"ContentLength": self.get_size, "Body": self.body}

    def generate_presigned_url(self, *_args, **_kwargs) -> str:
        return "https://public.example/signed"

    def close(self) -> None:
        self.close_calls += 1


def test_stream_cancellation_closes_body_without_buffering_the_object() -> None:
    body = FakeBody(b"chunk")

    async def consume_then_disconnect() -> None:
        iterator = ObjectStream(body, ObjectMetadata(5, "application/zip")).chunks(2)
        assert await anext(iterator) == b"chunk"
        await iterator.aclose()

    asyncio.run(consume_then_disconnect())
    assert body.read_sizes == [2]
    assert body.close_calls == 1


def _storage(client: FakeS3Client) -> S3ResultObjectStorage:
    return S3ResultObjectStorage(
        internal_endpoint="http://internal",
        external_endpoint="https://public.example",
        access_key="access",
        secret_key="secret",
        bucket="results",
        region="us-east-1",
        addressing_style="path",
        internal_client=client,
        signing_client=client,
    )


def test_bounded_read_accepts_object_exactly_at_limit_and_closes_body() -> None:
    body = FakeBody(b"data")
    client = FakeS3Client(head_size=4, get_size=4, body=body)

    result = asyncio.run(_storage(client).read_bounded("manifest.json", 4))

    assert result.payload == b"data"
    assert body.read_sizes == [5]
    assert body.close_calls == 1


def test_bounded_read_rejects_oversized_head_without_get() -> None:
    client = FakeS3Client(head_size=5)

    with pytest.raises(ObjectTooLargeError):
        asyncio.run(_storage(client).read_bounded("manifest.json", 4))

    assert client.get_calls == 0


@pytest.mark.parametrize(("head_size", "get_size"), [(3, 4), (4, 3)])
def test_bounded_read_classifies_in_limit_metadata_mismatch_as_storage_failure(
    head_size: int, get_size: int
) -> None:
    body = FakeBody(b"data")
    client = FakeS3Client(head_size=head_size, get_size=get_size, body=body)

    with pytest.raises(ObjectStorageError, match="request failed"):
        asyncio.run(_storage(client).read_bounded("manifest.json", 4))

    assert body.read_sizes == []
    assert body.close_calls == 1


def test_bounded_read_rejects_oversized_get_and_closes_body() -> None:
    body = FakeBody(b"data")
    client = FakeS3Client(head_size=4, get_size=5, body=body)

    with pytest.raises(ObjectTooLargeError):
        asyncio.run(_storage(client).read_bounded("manifest.json", 4))

    assert body.read_sizes == []
    assert body.close_calls == 1


def test_bounded_read_rejects_max_plus_one_result_and_closes_body() -> None:
    body = FakeBody(b"12345")
    client = FakeS3Client(head_size=4, get_size=4, body=body)

    with pytest.raises(ObjectTooLargeError):
        asyncio.run(_storage(client).read_bounded("manifest.json", 4))

    assert body.read_sizes == [5]
    assert body.close_calls == 1


def test_bounded_read_classifies_short_read_as_storage_failure_and_closes_body() -> (
    None
):
    body = FakeBody(b"123")
    client = FakeS3Client(head_size=4, get_size=4, body=body)

    with pytest.raises(ObjectStorageError, match="request failed"):
        asyncio.run(_storage(client).read_bounded("manifest.json", 4))

    assert body.read_sizes == [5]
    assert body.close_calls == 1


def test_bounded_read_wraps_read_error_and_closes_body() -> None:
    body = FakeBody(error=ConnectionError("internal endpoint detail"))
    client = FakeS3Client(head_size=4, get_size=4, body=body)

    with pytest.raises(ObjectStorageError, match="request failed") as caught:
        asyncio.run(_storage(client).read_bounded("manifest.json", 4))

    assert "endpoint detail" not in str(caught.value)
    assert body.close_calls == 1


def test_owned_clients_close_exactly_once(monkeypatch) -> None:
    clients = [FakeS3Client(), FakeS3Client()]
    monkeypatch.setattr(
        s3_module.boto3, "client", lambda *_args, **_kwargs: clients.pop(0)
    )
    storage = S3ResultObjectStorage(
        internal_endpoint="http://internal",
        external_endpoint="https://public.example",
        access_key="access",
        secret_key="secret",
        bucket="results",
        region="us-east-1",
        addressing_style="path",
    )
    owned = [storage._internal_client, storage._signing_client]

    asyncio.run(storage.close())
    asyncio.run(storage.close())

    assert [client.close_calls for client in owned] == [1, 1]


def test_partial_owned_client_creation_failure_closes_internal_once(
    monkeypatch,
) -> None:
    internal = FakeS3Client()
    creation_error = RuntimeError("signing client creation failed")
    calls = 0

    def create_client(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return internal
        raise creation_error

    monkeypatch.setattr(s3_module.boto3, "client", create_client)

    with pytest.raises(RuntimeError) as caught:
        S3ResultObjectStorage(
            internal_endpoint="http://internal",
            external_endpoint="https://public.example",
            access_key="access",
            secret_key="secret",
            bucket="results",
            region="us-east-1",
            addressing_style="path",
        )

    assert caught.value is creation_error
    assert calls == 2
    assert internal.close_calls == 1


def test_partial_creation_cleanup_error_does_not_mask_original(monkeypatch) -> None:
    class CloseFailureClient(FakeS3Client):
        def close(self) -> None:
            self.close_calls += 1
            raise RuntimeError("cleanup failed")

    internal = CloseFailureClient()
    creation_error = RuntimeError("signing client creation failed")
    clients = iter((internal, creation_error))

    def create_client(*_args, **_kwargs):
        result = next(clients)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(s3_module.boto3, "client", create_client)

    with pytest.raises(RuntimeError) as caught:
        S3ResultObjectStorage(
            internal_endpoint="http://internal",
            external_endpoint="https://public.example",
            access_key="access",
            secret_key="secret",
            bucket="results",
            region="us-east-1",
            addressing_style="path",
        )

    assert caught.value is creation_error
    assert internal.close_calls == 1


def test_partial_creation_does_not_close_injected_internal_client(monkeypatch) -> None:
    internal = FakeS3Client()
    creation_error = RuntimeError("signing client creation failed")
    monkeypatch.setattr(
        s3_module.boto3,
        "client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(creation_error),
    )

    with pytest.raises(RuntimeError) as caught:
        S3ResultObjectStorage(
            internal_endpoint="http://internal",
            external_endpoint="https://public.example",
            access_key="access",
            secret_key="secret",
            bucket="results",
            region="us-east-1",
            addressing_style="path",
            internal_client=internal,
        )

    assert caught.value is creation_error
    assert internal.close_calls == 0


def test_injected_clients_are_not_closed() -> None:
    client = FakeS3Client()
    storage = _storage(client)

    asyncio.run(storage.close())

    assert client.close_calls == 0


def test_presign_expiration_uses_time_captured_before_signing(monkeypatch) -> None:
    signed_at = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    calls = 0

    class Clock:
        @classmethod
        def now(cls, timezone):
            nonlocal calls
            assert timezone is UTC
            calls += 1
            return signed_at

    class SigningClient(FakeS3Client):
        def generate_presigned_url(self, *_args, **_kwargs) -> str:
            assert calls == 1
            return "https://public.example/signed"

    monkeypatch.setattr(s3_module, "datetime", Clock)

    result = asyncio.run(_storage(SigningClient()).presign("frame.jpg", 300))

    assert result.expires_at == signed_at + timedelta(seconds=300)
    assert calls == 1
