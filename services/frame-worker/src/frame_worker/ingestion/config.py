from dataclasses import dataclass


@dataclass(frozen=True)
class IngestionConfig:
    max_download_bytes: int = 2 * 1024 * 1024 * 1024
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 30.0
    total_timeout_seconds: float = 15 * 60.0
    max_redirects: int = 3
    chunk_size_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        if self.max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be greater than zero")
        if self.connect_timeout_seconds <= 0:
            raise ValueError("connect_timeout_seconds must be greater than zero")
        if self.read_timeout_seconds <= 0:
            raise ValueError("read_timeout_seconds must be greater than zero")
        if self.total_timeout_seconds <= 0:
            raise ValueError("total_timeout_seconds must be greater than zero")
        if self.max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if self.chunk_size_bytes <= 0:
            raise ValueError("chunk_size_bytes must be greater than zero")
