from collections.abc import Sequence
from pathlib import Path

from frame_worker.ingestion.adapters.base import AdapterMatch, URLSourceAdapter
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
        ranked = sorted(
            (
                (self._match(adapter, request), index, adapter)
                for index, adapter in enumerate(self.adapters)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        last_unsupported: UnsupportedVideoSourceError | None = None
        for match, _index, adapter in ranked:
            if match is AdapterMatch.NO_MATCH:
                continue
            try:
                return adapter.acquire(request, workspace, config)
            except UnsupportedVideoSourceError as error:
                last_unsupported = error
        if last_unsupported is not None:
            raise last_unsupported
        raise UnsupportedVideoSourceError(
            f"No configured source adapter supports host {safe_url_label(request.url)}"
        )

    @staticmethod
    def _match(
        adapter: URLSourceAdapter,
        request: URLSourceRequest,
    ) -> AdapterMatch:
        match_method = getattr(adapter, "match", None)
        if match_method is not None:
            return match_method(request)
        supports_method = getattr(adapter, "supports", None)
        if supports_method is not None and supports_method(request):
            return AdapterMatch.POSSIBLE
        return AdapterMatch.NO_MATCH
