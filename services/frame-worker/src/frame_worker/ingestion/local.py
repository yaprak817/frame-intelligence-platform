from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.models import NormalizedLocalVideo
from frame_worker.ingestion.validation import VideoFileValidator


class LocalUploadedVideoSource:
    def __init__(
        self,
        path: Path,
        owns_file: bool = False,
        config: IngestionConfig | None = None,
        validator: VideoFileValidator | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self.owns_file = owns_file
        self.config = config or IngestionConfig()
        self.validator = validator

    @contextmanager
    def materialize(self) -> Iterator[NormalizedLocalVideo]:
        resolved = self.path.resolve()
        try:
            validator = self.validator or VideoFileValidator()
            validator.validate(resolved, self.config.max_download_bytes)
            yield NormalizedLocalVideo(
                path=resolved,
                source_kind="local-upload",
                original_name=resolved.name,
                temporary=self.owns_file,
            )
        finally:
            if self.owns_file:
                resolved.unlink(missing_ok=True)
