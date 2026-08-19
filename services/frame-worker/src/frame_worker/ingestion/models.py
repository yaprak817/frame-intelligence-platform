from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class NormalizedLocalVideo:
    path: Path
    source_kind: str
    original_reference: str | None = None
    original_name: str | None = None
    media_type: str | None = None
    temporary: bool = False


class VideoSource(Protocol):
    def materialize(self) -> AbstractContextManager[NormalizedLocalVideo]: ...


@dataclass(frozen=True)
class URLSourceRequest:
    url: str


@dataclass(frozen=True)
class AcquiredVideo:
    path: Path
    media_type: str | None = None
    original_name: str | None = None
