from collections.abc import Sequence
from pathlib import Path

from frame_worker.ingestion.adapters.base import URLSourceAdapter
from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.errors import UnsupportedVideoSourceError
from frame_worker.ingestion.models import AcquiredVideo, URLSourceRequest
from frame_worker.ingestion.security import safe_url_label


class SourceResolver:
    def __init__(self, adapters: Sequence[URLSourceAdapter]) -> None:
        self.adapters = tuple(adapters)

    def resolve(
        self,
        request: URLSourceRequest,
        workspace: Path,
        config: IngestionConfig,
    ) -> AcquiredVideo:
        for adapter in self.adapters:
            if adapter.supports(request):
                return adapter.acquire(request, workspace, config)
        raise UnsupportedVideoSourceError(
            f"No configured source adapter supports host {safe_url_label(request.url)}"
        )
