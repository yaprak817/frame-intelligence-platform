"""Provider-independent video source ingestion."""

from frame_worker.ingestion.config import IngestionConfig
from frame_worker.ingestion.local import LocalUploadedVideoSource
from frame_worker.ingestion.models import NormalizedLocalVideo, VideoSource
from frame_worker.ingestion.resolver import SourceResolver
from frame_worker.ingestion.service import SourceProcessingService
from frame_worker.ingestion.url import GenericURLVideoSource

__all__ = [
    "GenericURLVideoSource",
    "IngestionConfig",
    "LocalUploadedVideoSource",
    "NormalizedLocalVideo",
    "SourceProcessingService",
    "SourceResolver",
    "VideoSource",
]
