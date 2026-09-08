import base64
import hashlib
import hmac
import ipaddress
import re
from dataclasses import dataclass
from typing import Protocol

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

RATE_LIMIT_DOMAIN = b"frame-intelligence-platform:rate-limit:v1"
MAX_FORWARDED_HEADER_LENGTH = 512
MAX_FORWARDED_HOPS = 16
PROXY_SIGNATURE_HEADER = "x-frame-client-ip-signature"
PROXY_SIGNATURE_DOMAIN = b"frame-intelligence-platform:proxy-client-ip:v1"


class RedisCommands(Protocol):
    async def eval(self, script: str, numkeys: int, *args: object) -> object: ...


_RATE_LIMIT_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
local ttl = redis.call('PTTL', KEYS[1])
if ttl <= 0 then
  redis.call('PEXPIRE', KEYS[1], ARGV[1])
  ttl = redis.call('PTTL', KEYS[1])
end
return {current, ttl}
"""


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    retry_after: int


class RedisRateLimiter:
    def __init__(self, redis: RedisCommands, *, namespace: str = "fip:rate-limit"):
        self._redis = redis
        self._namespace = namespace

    async def check(
        self, *, group: str, client_id: str, limit: int, window_seconds: int
    ) -> RateLimitDecision:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("Invalid rate-limit configuration")
        key = f"{self._namespace}:v1:w{window_seconds}:{group}:{client_id}"
        result = await self._redis.eval(
            _RATE_LIMIT_SCRIPT, 1, key, window_seconds * 1000
        )
        if not isinstance(result, (list, tuple)) or len(result) != 2:
            raise RuntimeError("Invalid rate-limit response")
        try:
            count, ttl_ms = (int(item) for item in result)
        except (TypeError, ValueError) as error:
            raise RuntimeError("Invalid rate-limit response") from error
        if count <= 0 or ttl_ms <= 0:
            raise RuntimeError("Invalid rate-limit response")
        retry_after = (ttl_ms + 999) // 1000
        return RateLimitDecision(count <= limit, retry_after)


def derive_rate_limit_key(encoded_secret: str) -> bytes:
    source_key = base64.urlsafe_b64decode(encoded_secret)
    return hmac.digest(source_key, RATE_LIMIT_DOMAIN, hashlib.sha256)


def client_identifier(
    scope: Scope,
    *,
    trusted_proxy_cidrs: list[str],
    secret: str,
    internal_proxy_shared_secret: str = "",
) -> str:
    direct = str(scope.get("client", ("unknown", 0))[0])
    try:
        peer = ipaddress.ip_address(direct)
    except ValueError:
        address = "unknown"
    else:
        networks = [
            ipaddress.ip_network(value, strict=False) for value in trusted_proxy_cidrs
        ]
        address = str(peer)
        signed = _signed_proxy_client(scope, internal_proxy_shared_secret)
        if signed is not None:
            address = signed
        elif any(peer in network for network in networks):
            forwarded_values = Headers(scope=scope).getlist("x-forwarded-for")
            forwarded = forwarded_values[0] if len(forwarded_values) == 1 else None
            candidate = _forwarded_client(forwarded, networks)
            if candidate is not None:
                address = candidate
    derived_key = derive_rate_limit_key(secret)
    return hmac.new(derived_key, address.encode(), hashlib.sha256).hexdigest()


def _signed_proxy_client(scope: Scope, shared_secret: str) -> str | None:
    if len(shared_secret) < 32:
        return None
    headers = Headers(scope=scope)
    forwarded_values = headers.getlist("x-forwarded-for")
    signature_values = headers.getlist(PROXY_SIGNATURE_HEADER)
    if len(forwarded_values) != 1 or len(signature_values) != 1:
        return None
    value = forwarded_values[0]
    signature = signature_values[0]
    if (
        not value
        or len(value) > 45
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or not re.fullmatch(r"[0-9a-f]{64}", signature)
    ):
        return None
    try:
        canonical = str(ipaddress.ip_address(value))
    except ValueError:
        return None
    expected = hmac.new(
        shared_secret.encode(),
        PROXY_SIGNATURE_DOMAIN + b"\0" + canonical.encode(),
        hashlib.sha256,
    ).hexdigest()
    return canonical if hmac.compare_digest(signature, expected) else None


def _forwarded_client(
    value: str | None,
    trusted_networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    # Trusted proxies append their sender. Walking right-to-left discards trusted
    # hops and selects the nearest untrusted address, so left-side spoofing fails.
    if not value or len(value) > MAX_FORWARDED_HEADER_LENGTH:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return None
    parts = value.split(",")
    if not 1 <= len(parts) <= MAX_FORWARDED_HOPS or any(
        not part.strip() for part in parts
    ):
        return None
    try:
        addresses = [ipaddress.ip_address(part.strip()) for part in parts]
    except ValueError:
        return None
    for address in reversed(addresses):
        if not any(address in network for network in trusted_networks):
            return str(address)
    return None


_RESULT_PATHS = (
    ("GET", re.compile(r"^/api/v1/jobs/[0-9a-fA-F-]{36}/result$")),
    ("GET", re.compile(r"^/api/v1/jobs/[0-9a-fA-F-]{36}/result/manifest$")),
    (
        "POST",
        re.compile(r"^/api/v1/jobs/[0-9a-fA-F-]{36}/result/frames/[0-9]+/access$"),
    ),
    (
        "GET",
        re.compile(r"^/api/v1/jobs/[0-9a-fA-F-]{36}/result/frames/[0-9]+/download$"),
    ),
    ("POST", re.compile(r"^/api/v1/jobs/[0-9a-fA-F-]{36}/exports$")),
    (
        "GET",
        re.compile(
            r"^/api/v1/jobs/[0-9a-fA-F-]{36}/exports/[0-9a-fA-F-]{36}(?:/download)?$"
        ),
    ),
)

_DATASET_PREVIEW_PATH = re.compile(
    r"^/api/v1/jobs/[0-9a-fA-F-]{36}/result/images/[0-9]+/preview$"
)
_DATASET_DOWNLOAD_PATH = re.compile(
    r"^/api/v1/jobs/[0-9a-fA-F-]{36}/dataset-exports/(?:accepted|yolo)/download$"
)


def protected_group(method: str, path: str) -> tuple[str, str] | None:
    if method == "POST" and path in {
        "/api/v1/jobs/upload",
        "/api/v1/jobs/url",
        "/api/v1/jobs/image-dataset",
    }:
        return "submission", "rate_limit_submission_requests"
    if method == "GET" and _DATASET_PREVIEW_PATH.fullmatch(path):
        return "dataset-previews", "rate_limit_result_requests"
    if method == "GET" and _DATASET_DOWNLOAD_PATH.fullmatch(path):
        return "dataset-downloads", "rate_limit_result_requests"
    if any(
        method == allowed and pattern.fullmatch(path)
        for allowed, pattern in _RESULT_PATHS
    ):
        return "results", "rate_limit_result_requests"
    return None


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        match = protected_group(scope["method"], scope["path"])
        if match is None:
            await self.app(scope, receive, send)
            return
        group, limit_setting = match
        settings = scope["app"].state.settings
        limiter: RedisRateLimiter = scope["app"].state.rate_limiter
        identifier = client_identifier(
            scope,
            trusted_proxy_cidrs=settings.trusted_proxy_cidrs,
            secret=settings.job_source_encryption_key,
            internal_proxy_shared_secret=getattr(
                settings, "internal_proxy_shared_secret", ""
            ),
        )
        try:
            decision = await limiter.check(
                group=group,
                client_id=identifier,
                limit=getattr(settings, limit_setting),
                window_seconds=settings.rate_limit_window_seconds,
            )
        except Exception:
            await _error_response(
                503,
                "RATE_LIMIT_UNAVAILABLE",
                "Request protection is temporarily unavailable",
            )(scope, receive, send)
            return
        if not decision.allowed:
            await _error_response(
                429,
                "RATE_LIMITED",
                "Too many requests",
                {"Retry-After": str(decision.retry_after)},
            )(scope, receive, send)
            return
        if scope["method"] == "POST" and scope["path"] == "/api/v1/jobs/image-dataset":
            maximum = settings.image_dataset_max_total_bytes + min(
                16 * 1024 * 1024,
                settings.image_dataset_max_files * 2048 + 1024 * 1024,
            )
            raw_length = Headers(scope=scope).get("content-length")
            if raw_length is not None:
                try:
                    if int(raw_length) > maximum:
                        await _error_response(
                            413, "DATASET_TOO_LARGE", "Dataset upload is too large"
                        )(scope, receive, send)
                        return
                except ValueError:
                    pass
            consumed = 0

            async def bounded_receive():
                nonlocal consumed
                message = await receive()
                if message["type"] == "http.request":
                    consumed += len(message.get("body", b""))
                    if consumed > maximum:
                        raise DatasetRequestTooLarge
                return message

            try:
                await self.app(scope, bounded_receive, send)
            except DatasetRequestTooLarge:
                await _error_response(
                    413, "DATASET_TOO_LARGE", "Dataset upload is too large"
                )(scope, receive, send)
            return
        await self.app(scope, receive, send)


class DatasetRequestTooLarge(RuntimeError):
    pass


def _error_response(
    status_code: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"detail": {"code": code, "message": message}},
        headers=headers,
    )
