import asyncio
import base64
import hashlib
import hmac
import ipaddress
import os
import socket
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient
from redis.asyncio import Redis

from app.core.observability import RequestLoggingMiddleware
from app.security.rate_limit import (
    MAX_FORWARDED_HEADER_LENGTH,
    RateLimitDecision,
    RateLimitMiddleware,
    RedisRateLimiter,
    _signed_proxy_client,
    client_identifier,
    derive_rate_limit_key,
    protected_group,
)

PROXY_SIGNATURE_DOMAIN = b"frame-intelligence-platform:proxy-client-ip:v1"
HMAC_VECTOR_SECRET = "fixture-only-proxy-secret-0123456789-DO-NOT-USE"
HMAC_VECTOR_IP = "203.0.113.8"
HMAC_VECTOR_EXPECTED = (
    "d513dd72d0b92b859e2130ba7c5d6cd3b9ebf8ce894cb1aa0ddd9a8349d3f204"
)

TEST_SECRET = base64.urlsafe_b64encode(bytes(range(32))).decode()
PROXY_SECRET = "proxy-secret-0123456789-ABCDEFGHIJ"


def scope_for(host: str, forwarded: str | None = None) -> dict:
    headers = []
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/jobs/upload",
        "headers": headers,
        "client": (host, 1234),
    }


def scope_with_headers(host: str, headers: list[tuple[bytes, bytes]]) -> dict:
    scope = scope_for(host)
    scope["headers"] = headers
    return scope


def identity(host: str, forwarded: str | None, networks: list[str]) -> str:
    return client_identifier(
        scope_for(host, forwarded),
        trusted_proxy_cidrs=networks,
        secret=TEST_SECRET,
    )


def signed_headers(address: str, *, signature: str | None = None) -> dict[str, str]:
    canonical = str(ipaddress.ip_address(address))
    valid = hmac.new(
        PROXY_SECRET.encode(),
        PROXY_SIGNATURE_DOMAIN + b"\0" + canonical.encode(),
        hashlib.sha256,
    ).hexdigest()
    return {
        "X-Forwarded-For": address,
        "X-Frame-Client-IP-Signature": signature or valid,
    }


def signed_identity(host: str, address: str, *, signature: str | None = None) -> str:
    headers = signed_headers(address, signature=signature)
    scope = scope_with_headers(
        host,
        [(name.lower().encode(), value.encode()) for name, value in headers.items()],
    )
    return client_identifier(
        scope,
        trusted_proxy_cidrs=[],
        secret=TEST_SECRET,
        internal_proxy_shared_secret=PROXY_SECRET,
    )


def test_signed_proxy_clients_have_distinct_hashed_ipv4_and_ipv6_identities() -> None:
    first = signed_identity("172.20.0.3", "203.0.113.8")
    second = signed_identity("172.20.0.3", "203.0.113.9")
    ipv6 = signed_identity("172.20.0.3", "2001:0db8:0:0:0:0:0:8")
    assert len(first) == 64 and first != second and first != ipv6
    assert all(
        raw not in value
        for raw in ("203.0.113.8", "203.0.113.9")
        for value in (first, second, ipv6)
    )


def test_untrusted_data_peer_cannot_spoof_signed_proxy_identity() -> None:
    direct = identity("172.21.0.8", None, [])
    forged = signed_identity("172.21.0.8", "203.0.113.8", signature="0" * 64)
    assert forged == direct


def test_proxy_signature_requires_the_versioned_domain() -> None:
    address = "203.0.113.8"
    undomained = hmac.new(
        PROXY_SECRET.encode(), address.encode(), hashlib.sha256
    ).hexdigest()
    direct = identity("172.20.0.3", None, [])
    assert signed_identity("172.20.0.3", address, signature=undomained) == direct


def test_proxy_signature_rejects_a_different_domain() -> None:
    address = "203.0.113.8"
    wrong_domain = hmac.new(
        PROXY_SECRET.encode(),
        b"frame-intelligence-platform:proxy-client-ip:v2\0" + address.encode(),
        hashlib.sha256,
    ).hexdigest()
    direct = identity("172.20.0.3", None, [])
    assert signed_identity("172.20.0.3", address, signature=wrong_domain) == direct


def test_shared_hardcoded_proxy_client_ip_hmac_vector() -> None:
    scope = scope_with_headers(
        "172.20.0.3",
        [
            (b"x-forwarded-for", HMAC_VECTOR_IP.encode()),
            (b"x-frame-client-ip-signature", HMAC_VECTOR_EXPECTED.encode()),
        ],
    )
    assert _signed_proxy_client(scope, HMAC_VECTOR_SECRET) == HMAC_VECTOR_IP


def test_untrusted_peer_ignores_forwarded_header() -> None:
    assert identity("192.0.2.1", "203.0.113.1", []) == identity(
        "192.0.2.1", "198.51.100.2", []
    )


def test_proxy_chain_selects_nearest_untrusted_client() -> None:
    networks = ["10.0.0.0/8", "2001:db8:ffff::/48"]
    expected = identity("10.0.0.2", "203.0.113.8, 10.1.1.1", networks)
    spoofed = identity("10.0.0.2", "198.51.100.99, 203.0.113.8, 10.1.1.1", networks)
    assert expected == spoofed
    assert expected != identity("10.0.0.2", "203.0.113.9, 10.1.1.1", networks)


def test_proxy_chain_supports_ipv4_and_ipv6() -> None:
    assert identity("10.0.0.2", "203.0.113.8", ["10.0.0.0/8"]) != identity(
        "10.0.0.2", "203.0.113.9", ["10.0.0.0/8"]
    )
    assert identity(
        "2001:db8:ffff::1", "2001:db8::8", ["2001:db8:ffff::/48"]
    ) != identity("2001:db8:ffff::1", "2001:db8::9", ["2001:db8:ffff::/48"])


@pytest.mark.parametrize(
    "forwarded",
    ["", "203.0.113.1,,10.0.0.1", "bad-ip", "203.0.113.1\x01", "1.1.1.1," * 17],
)
def test_invalid_proxy_chain_falls_back_to_direct_peer(forwarded: str) -> None:
    assert identity("10.0.0.2", forwarded, ["10.0.0.0/8"]) == identity(
        "10.0.0.2", None, ["10.0.0.0/8"]
    )
    assert len(forwarded) <= MAX_FORWARDED_HEADER_LENGTH or forwarded.count(",") > 16


def test_oversized_forwarded_header_is_ignored() -> None:
    oversized = "1" * (MAX_FORWARDED_HEADER_LENGTH + 1)
    assert identity("10.0.0.2", oversized, ["10.0.0.0/8"]) == identity(
        "10.0.0.2", None, ["10.0.0.0/8"]
    )


def test_duplicate_forwarded_headers_are_ignored() -> None:
    duplicate_scope = scope_with_headers(
        "10.0.0.2",
        [
            (b"x-forwarded-for", b"203.0.113.1"),
            (b"x-forwarded-for", b"198.51.100.2"),
        ],
    )
    assert client_identifier(
        duplicate_scope,
        trusted_proxy_cidrs=["10.0.0.0/8"],
        secret=TEST_SECRET,
    ) == identity("10.0.0.2", None, ["10.0.0.0/8"])


def test_domain_separation_has_stable_vector() -> None:
    assert derive_rate_limit_key(TEST_SECRET).hex() == (
        "35a24d868f380c360b5f3fa56aebd7b6281ab736fc2896235153d01f33e27d4e"
    )
    assert derive_rate_limit_key(TEST_SECRET) != bytes(range(32))


class FixedLimiter:
    def __init__(self, decision=None, error: Exception | None = None):
        self.decision = decision
        self.error = error

    async def check(self, **_kwargs):
        if self.error:
            raise self.error
        return self.decision


def middleware_app(limiter) -> FastAPI:
    app = FastAPI()
    app.state.rate_limiter = limiter
    app.state.settings = SimpleNamespace(
        trusted_proxy_cidrs=[],
        job_source_encryption_key=TEST_SECRET,
        internal_proxy_shared_secret=PROXY_SECRET,
        rate_limit_submission_requests=2,
        rate_limit_result_requests=10,
        rate_limit_window_seconds=60,
    )
    app.add_middleware(RateLimitMiddleware)

    @app.post("/api/v1/jobs/upload")
    async def upload() -> dict[str, bool]:
        return {"accepted": True}

    @app.get("/api/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "healthy"}

    return app


def test_real_http_429_and_health_exclusion() -> None:
    app = middleware_app(FixedLimiter(RateLimitDecision(False, 7)))
    with TestClient(app) as client:
        rejected = client.post("/api/v1/jobs/upload", content=b"large-body")
        health = client.get("/api/v1/health")
    assert rejected.status_code == 429
    assert rejected.headers["Retry-After"] == "7"
    assert rejected.json()["detail"]["code"] == "RATE_LIMITED"
    assert health.status_code == 200


def test_real_http_redis_outage_is_safe_503() -> None:
    app = middleware_app(
        FixedLimiter(error=ConnectionError("redis://secret@internal-redis"))
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/jobs/upload", content=b"large-body")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "RATE_LIMIT_UNAVAILABLE"
    assert "redis" not in response.text.lower()


@pytest.mark.parametrize(
    ("path", "expected_group"),
    [
        (
            "/api/v1/jobs/11111111-1111-4111-8111-111111111111/result/images/0/preview",
            "dataset-previews",
        ),
        (
            "/api/v1/jobs/11111111-1111-4111-8111-111111111111/dataset-exports/accepted/download",
            "dataset-downloads",
        ),
        (
            "/api/v1/jobs/11111111-1111-4111-8111-111111111111/dataset-exports/yolo/download",
            "dataset-downloads",
        ),
    ],
)
def test_dataset_previews_and_downloads_use_separate_buckets(
    path: str, expected_group: str
) -> None:
    assert protected_group("GET", path) == (
        expected_group,
        "rate_limit_result_requests",
    )
    assert expected_group != "results"


def test_preview_exhaustion_does_not_consume_dataset_download_quota() -> None:
    class CountingRedis:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}

        async def eval(self, _script, _numkeys, key, _ttl):
            self.counts[key] = self.counts.get(key, 0) + 1
            return [self.counts[key], 60_000]

    redis = CountingRedis()
    app = middleware_app(RedisRateLimiter(redis))
    job = "11111111-1111-4111-8111-111111111111"

    @app.get("/api/v1/jobs/{job_id}/result")
    async def result(job_id: str):
        return {"job_id": job_id}

    @app.get("/api/v1/jobs/{job_id}/result/images/{index}/preview")
    async def preview(job_id: str, index: int):
        return {"job_id": job_id, "index": index}

    @app.get("/api/v1/jobs/{job_id}/dataset-exports/{mode}/download")
    async def download(job_id: str, mode: str):
        return {"job_id": job_id, "mode": mode}

    with TestClient(app) as client:
        previews = [
            client.get(f"/api/v1/jobs/{job}/result/images/{i}/preview")
            for i in range(84)
        ]
        assert sum(response.status_code == 200 for response in previews) == 10
        assert sum(response.status_code == 429 for response in previews) == 74
        accepted = client.get(f"/api/v1/jobs/{job}/dataset-exports/accepted/download")
        yolo = client.get(f"/api/v1/jobs/{job}/dataset-exports/yolo/download")
        assert accepted.status_code == yolo.status_code == 200
        for _ in range(8):
            assert (
                client.get(
                    f"/api/v1/jobs/{job}/dataset-exports/accepted/download"
                ).status_code
                == 200
            )
        exhausted = client.get(f"/api/v1/jobs/{job}/dataset-exports/yolo/download")
        assert exhausted.status_code == 429
        assert 1 <= int(exhausted.headers["Retry-After"]) <= 60
        assert client.get(f"/api/v1/jobs/{job}/result").status_code == 200


@pytest.mark.parametrize(
    "limiter",
    [
        FixedLimiter(RateLimitDecision(False, 3)),
        FixedLimiter(error=ConnectionError("unavailable")),
    ],
)
def test_rejection_does_not_read_multipart_body(limiter) -> None:
    receive_calls = 0
    downstream_calls = 0

    async def downstream(_scope, receive, _send):
        nonlocal downstream_calls
        downstream_calls += 1
        await receive()

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"payload", "more_body": False}

    async def send(_message):
        return None

    app = SimpleNamespace(
        state=SimpleNamespace(
            rate_limiter=limiter,
            settings=SimpleNamespace(
                trusted_proxy_cidrs=[],
                job_source_encryption_key=TEST_SECRET,
                rate_limit_submission_requests=2,
                rate_limit_window_seconds=60,
            ),
        )
    )
    scope = scope_for("192.0.2.1")
    scope["app"] = app
    asyncio.run(RateLimitMiddleware(downstream)(scope, receive, send))
    assert receive_calls == 0
    assert downstream_calls == 0


@pytest.mark.parametrize(
    ("limiter", "expected_status"),
    [
        (FixedLimiter(RateLimitDecision(False, 3)), 429),
        (FixedLimiter(error=ConnectionError("redis://secret@internal")), 503),
    ],
)
def test_full_middleware_stack_rejects_without_body_and_adds_headers(
    limiter, expected_status
) -> None:
    async def scenario() -> None:
        receive_calls = 0
        route_calls = 0
        messages = []
        app = FastAPI()
        app.state.rate_limiter = limiter
        app.state.settings = SimpleNamespace(
            trusted_proxy_cidrs=[],
            job_source_encryption_key=TEST_SECRET,
            rate_limit_submission_requests=2,
            rate_limit_result_requests=10,
            rate_limit_window_seconds=60,
        )
        app.add_middleware(RateLimitMiddleware)
        app.add_middleware(RequestLoggingMiddleware)
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["https://frontend.example.test"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app.post("/api/v1/jobs/upload")
        async def upload() -> dict[str, bool]:
            nonlocal route_calls
            route_calls += 1
            return {"accepted": True}

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            return {"type": "http.request", "body": b"large", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = scope_for("192.0.2.8")
        scope.update(
            {
                "http_version": "1.1",
                "scheme": "https",
                "server": ("api.example.test", 443),
                "root_path": "",
                "query_string": b"",
                "headers": [(b"origin", b"https://frontend.example.test")],
            }
        )
        await app(scope, receive, send)
        start = next(
            message for message in messages if message["type"] == "http.response.start"
        )
        body = b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        headers = dict(start["headers"])
        assert start["status"] == expected_status
        assert receive_calls == 0 and route_calls == 0
        assert headers[b"x-request-id"]
        assert (
            headers[b"access-control-allow-origin"] == b"https://frontend.example.test"
        )
        if expected_status == 429:
            assert int(headers[b"retry-after"]) > 0
        assert not any(
            value in body.lower()
            for value in (b"192.0.2.8", b"redis", b"secret", b"internal")
        )

    asyncio.run(scenario())


def test_invalid_redis_response_fails_closed() -> None:
    class RedisStub:
        async def eval(self, *_args):
            return [1]

    with pytest.raises(RuntimeError):
        asyncio.run(
            RedisRateLimiter(RedisStub()).check(
                group="submission", client_id="client", limit=2, window_seconds=60
            )
        )


@pytest.mark.skipif(
    os.getenv("REDIS_INTEGRATION") != "1", reason="requires isolated Redis"
)
def test_redis_limiter_repairs_ttl_and_separates_window_and_identity() -> None:
    async def scenario() -> None:
        redis = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/15"))
        limiter = RedisRateLimiter(redis, namespace="fip:test:rate-limit")
        raw_ip = "192.0.2.44"
        identifier = identity(raw_ip, None, [])
        key = f"fip:test:rate-limit:v1:w1:submission:{identifier}"
        other_window = f"fip:test:rate-limit:v1:w2:submission:{identifier}"
        try:
            await redis.set(key, 1)
            first = await limiter.check(
                group="submission", client_id=identifier, limit=2, window_seconds=1
            )
            assert first.allowed and await redis.pttl(key) > 0
            concurrent = await asyncio.gather(
                *[
                    limiter.check(
                        group="submission",
                        client_id=identifier,
                        limit=2,
                        window_seconds=1,
                    )
                    for _ in range(2)
                ]
            )
            assert sum(item.allowed for item in concurrent) == 0
            assert all(item.retry_after > 0 for item in concurrent)
            other = await limiter.check(
                group="submission", client_id=identifier, limit=2, window_seconds=2
            )
            assert other.allowed and await redis.exists(other_window)
            keys = [item.decode() for item in await redis.keys("fip:test:rate-limit:*")]
            assert all(raw_ip not in item for item in keys)
            await asyncio.sleep(1.05)
            reset = await limiter.check(
                group="submission", client_id=identifier, limit=2, window_seconds=1
            )
            assert reset.allowed
        finally:
            await redis.delete(key, other_window)
            await redis.aclose()

    asyncio.run(scenario())


@pytest.mark.skipif(
    os.getenv("REDIS_INTEGRATION") != "1", reason="requires isolated Redis"
)
def test_real_redis_drives_real_http_429_and_real_outage_503() -> None:
    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/15")
    redis = Redis.from_url(redis_url)
    app = middleware_app(RedisRateLimiter(redis, namespace="fip:test:http"))
    app.state.settings.rate_limit_submission_requests = 1
    with TestClient(app) as client:
        first = client.post("/api/v1/jobs/upload")
        second = client.post("/api/v1/jobs/upload")
        keys = client.portal.call(redis.keys, "fip:test:http:*")
        assert first.status_code == 200
        assert second.status_code == 429
        assert int(second.headers["Retry-After"]) > 0
        assert keys and all(b"192.0.2" not in key for key in keys)
        client.portal.call(redis.delete, *keys)
        client.portal.call(redis.aclose)

    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        unavailable_port = reserved.getsockname()[1]
    unavailable = Redis.from_url(
        f"redis://127.0.0.1:{unavailable_port}/0",
        socket_connect_timeout=0.05,
        socket_timeout=0.05,
    )
    outage_app = middleware_app(RedisRateLimiter(unavailable))
    with TestClient(outage_app) as client:
        response = client.post("/api/v1/jobs/upload")
        client.portal.call(unavailable.aclose)
    assert response.status_code == 503
    assert not any(
        value in response.text.lower()
        for value in ("redis", "127.0.0.1", str(unavailable_port), "secret")
    )


@pytest.mark.skipif(
    os.getenv("REDIS_INTEGRATION") != "1", reason="requires isolated Redis"
)
def test_real_http_and_redis_keep_signed_client_quotas_separate() -> None:
    redis = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/15"))
    app = middleware_app(RedisRateLimiter(redis, namespace="fip:test:clients"))
    app.state.settings.rate_limit_submission_requests = 1
    with TestClient(app, client=("172.20.0.3", 50000)) as client:
        first_a = client.post(
            "/api/v1/jobs/upload", headers=signed_headers("203.0.113.8")
        )
        second_a = client.post(
            "/api/v1/jobs/upload", headers=signed_headers("203.0.113.8")
        )
        first_b = client.post(
            "/api/v1/jobs/upload", headers=signed_headers("2001:db8::9")
        )
        forged = client.post(
            "/api/v1/jobs/upload",
            headers=signed_headers("198.51.100.4", signature="0" * 64),
        )
        keys = client.portal.call(redis.keys, "fip:test:clients:*")
        assert first_a.status_code == 200
        assert second_a.status_code == 429 and int(second_a.headers["Retry-After"]) > 0
        assert first_b.status_code == 200
        assert forged.status_code == 200
        client.portal.call(redis.aclose)
    data_redis = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379/15"))
    data_app = middleware_app(
        RedisRateLimiter(data_redis, namespace="fip:test:clients")
    )
    data_app.state.settings.rate_limit_submission_requests = 1
    with TestClient(data_app, client=("172.21.0.8", 50000)) as data_peer:
        unsigned = data_peer.post(
            "/api/v1/jobs/upload", headers={"X-Forwarded-For": "192.0.2.9"}
        )
        keys = data_peer.portal.call(data_redis.keys, "fip:test:clients:*")
        assert unsigned.status_code == 200
        assert len(keys) == 4
        assert all(
            not any(
                raw in key
                for raw in (
                    b"203.0.113.8",
                    b"2001:db8",
                    b"198.51.100.4",
                    b"192.0.2.9",
                )
            )
            for key in keys
        )
        data_peer.portal.call(data_redis.delete, *keys)
        data_peer.portal.call(data_redis.aclose)
