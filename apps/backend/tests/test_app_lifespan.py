from fastapi.testclient import TestClient

import app.main as main_module


def test_result_storage_is_shared_and_closed_once(monkeypatch) -> None:
    instances = []

    class RecordingStorage:
        def __init__(self, **_kwargs) -> None:
            self.close_calls = 0
            instances.append(self)

        async def close(self) -> None:
            self.close_calls += 1

    monkeypatch.setattr(main_module, "S3ResultObjectStorage", RecordingStorage)

    with TestClient(main_module.app) as client:
        first = client.app.state.result_object_storage
        assert client.get("/api/v1/health").status_code == 200
        second = client.app.state.result_object_storage
        assert first is second
        assert len(instances) == 1
        assert first.close_calls == 0

    assert instances[0].close_calls == 1
