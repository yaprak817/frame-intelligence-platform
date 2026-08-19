from pathlib import Path
from typing import Protocol

from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.models import AcquiredVideo, URLSourceRequest


class URLSourceAdapter(Protocol):
    name: str

    def supports(self, request: URLSourceRequest) -> bool: ...

    def acquire(
        self,
        request: URLSourceRequest,
        workspace: Path,
        config: IngestionConfig,
    ) -> AcquiredVideo: ...
