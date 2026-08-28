import json
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.observability import (
    JsonFormatter,
    RequestLoggingMiddleware,
    configure_logging,
)


def test_safe_request_id_is_preserved(client) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "safe-id_123"})
    assert response.headers["X-Request-ID"] == "safe-id_123"


def test_invalid_request_id_is_replaced(client) -> None:
    response = client.get("/api/v1/health", headers={"X-Request-ID": "bad\nvalue"})
    assert response.headers["X-Request-ID"] != "bad\nvalue"
    assert "\n" not in response.headers["X-Request-ID"]


def test_json_formatter_emits_only_allowlisted_request_fields() -> None:
    record = logging.LogRecord(
        "app.request", logging.INFO, __file__, 1, "ignored secret-url", (), None
    )
    record.event = "http_request"
    record.request_id = "request-1"
    record.method = "GET"
    record.route = "/api/v1/jobs/{job_id}/result"
    record.status_code = 503
    record.duration_ms = 1.25
    record.query = "token=secret"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "http_request"
    assert payload["route"] == "/api/v1/jobs/{job_id}/result"
    assert "query" not in payload
    assert "secret" not in json.dumps(payload)


def test_unhandled_error_returns_safe_body_and_request_id() -> None:
    test_app = FastAPI()
    test_app.add_middleware(RequestLoggingMiddleware)

    @test_app.get("/failure")
    async def failure() -> None:
        raise RuntimeError("postgresql://user:secret@internal-db/private")

    with TestClient(test_app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/failure")
    assert response.status_code == 500
    assert response.headers["X-Request-ID"]
    assert response.json() == {
        "detail": {"code": "INTERNAL_ERROR", "message": "Internal server error"}
    }
    assert "internal-db" not in response.text


def test_production_logging_disables_uvicorn_access_and_redacts_messages(
    capsys,
) -> None:
    names = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    snapshot = {
        name: (
            list(logging.getLogger(name).handlers),
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
            logging.getLogger(name).level,
        )
        for name in names
    }
    try:
        configure_logging(True)
        logging.getLogger("uvicorn.access").info(
            '192.0.2.1 - "GET /private?token=secret HTTP/1.1"'
        )
        logging.getLogger("uvicorn.error").error(
            "failed redis://user:secret@internal-redis local=C:\\private\\file"
        )
        output = capsys.readouterr().err.strip().splitlines()
        assert len(output) == 1
        payload = json.loads(output[0])
        assert payload == {
            "timestamp": payload["timestamp"],
            "level": "error",
            "service": "frame-intelligence-api",
            "event": "application_log",
        }
        assert not any(
            value in output[0]
            for value in ("192.0.2.1", "token", "redis://", "internal-redis", "private")
        )
        assert logging.getLogger("uvicorn.access").disabled
    finally:
        for name, (handlers, propagate, disabled, level) in snapshot.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.disabled = disabled
            logger.setLevel(level)
