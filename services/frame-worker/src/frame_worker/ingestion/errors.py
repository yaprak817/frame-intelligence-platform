class SourceIngestionError(RuntimeError):
    """Base error for source ingestion failures."""


class InvalidVideoSourceError(SourceIngestionError):
    """Raised when materialized content is not a valid video source."""


class UnsupportedVideoSourceError(SourceIngestionError):
    """Raised when no configured adapter supports a source."""


class VideoDownloadError(SourceIngestionError):
    """Raised when a direct video download fails."""


class VideoTooLargeError(SourceIngestionError):
    """Raised when a source exceeds the configured byte limit."""


class UnsafeVideoURLError(SourceIngestionError):
    """Raised when a URL violates the ingestion network policy."""
