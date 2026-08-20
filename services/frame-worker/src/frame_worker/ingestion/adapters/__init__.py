from frame_worker.ingestion.adapters.base import AdapterMatch, URLSourceAdapter
from frame_worker.ingestion.adapters.direct_http import DirectHTTPVideoAdapter
from frame_worker.ingestion.adapters.yt_dlp import YtDlpURLAdapter

__all__ = [
    "AdapterMatch",
    "DirectHTTPVideoAdapter",
    "URLSourceAdapter",
    "YtDlpURLAdapter",
]
