from enum import IntEnum
from pathlib import Path
from typing import Protocol

from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.models import AcquiredVideo, URLSourceRequest


class AdapterMatch(IntEnum):
    NO_MATCH = 0
    POSSIBLE = 1
    DEFINITE = 2


class URLSourceAdapter(Protocol):
    name: str

    def match(self, request: URLSourceRequest) -> AdapterMatch: ...

    def acquire(
        self,
        request: URLSourceRequest,
        workspace: Path,
        config: IngestionConfig,
    ) -> AcquiredVideo: ...
