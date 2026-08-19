import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.errors import InvalidVideoSourceError
from frame_worker.ingestion.models import NormalizedLocalVideo, URLSourceRequest
from frame_worker.ingestion.resolver import SourceResolver
from frame_worker.ingestion.security import safe_url_label
from frame_worker.ingestion.validation import VideoFileValidator


class GenericURLVideoSource:
    def __init__(
        self,
        url: str,
        resolver: SourceResolver,
        config: IngestionConfig | None = None,
        validator: VideoFileValidator | None = None,
    ) -> None:
        self.url = url
        self.resolver = resolver
        self.config = config or IngestionConfig()
        self.validator = validator

    @contextmanager
    def materialize(self) -> Iterator[NormalizedLocalVideo]:
        with tempfile.TemporaryDirectory(prefix="frame-worker-source-") as directory:
            workspace = Path(directory).resolve()
            acquired = self.resolver.resolve(
                URLSourceRequest(self.url),
                workspace,
                self.config,
            )
            path = acquired.path.resolve()
            if not path.is_relative_to(workspace):
                raise InvalidVideoSourceError(
                    "Source adapter returned a path outside its workspace"
                )
            validator = self.validator or VideoFileValidator()
            validator.validate(path, self.config.max_download_bytes)
            yield NormalizedLocalVideo(
                path=path,
                source_kind="generic-url",
                original_reference=safe_url_label(self.url),
                original_name=acquired.original_name,
                media_type=acquired.media_type,
                temporary=True,
            )
