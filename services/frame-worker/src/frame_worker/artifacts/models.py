from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class FrameManifestEntry:
    index: int
    filename: str
    object_key: str
    content_type: str
    size_bytes: int
    sha256: str
    timestamp_ms: int
    width: int
    height: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
