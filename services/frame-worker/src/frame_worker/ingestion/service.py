from pathlib import Path

from frame_worker.ingestion.models import VideoSource
from frame_worker.processing.pipeline import ProcessingSummary, VideoProcessor


class SourceProcessingService:
    def __init__(self, processor: VideoProcessor) -> None:
        self.processor = processor

    def process(
        self,
        source: VideoSource,
        output_directory: Path,
    ) -> ProcessingSummary:
        with source.materialize() as video:
            return self.processor.process(video.path, output_directory)
