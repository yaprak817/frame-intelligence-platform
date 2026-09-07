# ruff: noqa: E501
import hashlib
import json
import logging
import re
import shutil
import stat
import time
import unicodedata
import warnings
import zipfile
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import boto3
import cv2
import numpy as np
from botocore.config import Config
from PIL import Image, ImageOps, UnidentifiedImageError

from frame_worker.artifacts.object_storage import PersistedArtifacts
from frame_worker.orchestration.config import WorkerSettings

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"RIFF", "image/webp"),
)
_IGNORED_NAMES = {".ds_store"}
_FORMAT_BY_TYPE = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
_ALPHA_BACKGROUND_RGB = (114, 114, 114)
logger = logging.getLogger(__name__)


class DatasetError(ValueError):
    """A permanent, safely reportable dataset validation failure."""


class DatasetCleanupError(RuntimeError):
    """Attempt-scoped artifact cleanup could not be completed."""


class _BoundedWriter:
    def __init__(self, target, maximum: int) -> None:
        if maximum <= 0:
            raise ValueError("maximum must be positive")
        self._target = target
        self._maximum = maximum

    def write(self, value: bytes) -> int:
        end = self.tell() + len(value)
        if end < 0 or max(self._size(), end) > self._maximum:
            raise DatasetError("Dataset export byte limit exceeded")
        return self._target.write(value)

    def _size(self) -> int:
        position = self._target.tell()
        self._target.seek(0, 2)
        size = self._target.tell()
        self._target.seek(position)
        return size

    def tell(self) -> int:
        return self._target.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._target.seek(offset, whence)

    def flush(self) -> None:
        self._target.flush()

    def truncate(self, size: int | None = None) -> int:
        target = self.tell() if size is None else size
        if target < 0 or target > self._maximum:
            raise DatasetError("Dataset export byte limit exceeded")
        return self._target.truncate(target)

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True


@dataclass(frozen=True)
class DatasetOutput:
    summary: dict[str, Any]
    persisted: PersistedArtifacts


def _client(settings: WorkerSettings):
    return boto3.client(
        "s3",
        endpoint_url=settings.object_storage_endpoint,
        aws_access_key_id=settings.object_storage_access_key,
        aws_secret_access_key=settings.object_storage_secret_key,
        region_name=settings.object_storage_region,
        config=Config(
            s3={"addressing_style": settings.object_storage_addressing_style}
        ),
    )


def _workspace_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _ensure_workspace(root: Path, settings: WorkerSettings) -> None:
    if _workspace_bytes(root) > settings.dataset_max_temp_bytes:
        raise DatasetError("Dataset temporary storage limit exceeded")
    if shutil.disk_usage(root).free < settings.dataset_min_temp_free_bytes:
        raise DatasetError("Dataset temporary storage is unavailable")


def _cleanup_stale_run(
    client,
    bucket: str,
    job_id: UUID,
    stale_run_token: UUID,
    current_run_token: UUID,
    maximum: int,
) -> None:
    if (
        not isinstance(job_id, UUID)
        or not isinstance(stale_run_token, UUID)
        or not isinstance(current_run_token, UUID)
        or stale_run_token == current_run_token
        or maximum <= 0
    ):
        raise DatasetCleanupError("Dataset stale cleanup identity is invalid")
    prefix = f"jobs/{job_id}/results/{stale_run_token}/"
    final_key = f"{prefix}manifest.json"
    token: str | None = None
    visited = 0
    keys: list[str] = []
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": min(1000, maximum)}
        if token:
            kwargs["ContinuationToken"] = token
        try:
            response = client.list_objects_v2(**kwargs)
        except Exception as error:
            raise DatasetCleanupError(
                "Dataset stale cleanup inventory is unavailable"
            ) from error
        for item in response.get("Contents", []):
            key = item.get("Key")
            visited += 1
            if visited > maximum:
                raise DatasetError("Stale dataset artifact limit exceeded")
            if not isinstance(key, str) or not key.startswith(prefix):
                raise DatasetCleanupError("Dataset stale cleanup listing is invalid")
            keys.append(key)
        if not response.get("IsTruncated"):
            break
        token = response.get("NextContinuationToken")
        if not isinstance(token, str):
            raise DatasetCleanupError("Dataset stale cleanup listing is invalid")
    # The manifest is written last and is the immutable publication boundary.  A
    # run that reached it is no longer a partial attempt, even if database state
    # was not advanced before a worker loss.  Reconciliation must preserve the
    # complete result rather than treating it as stale scratch data.
    if final_key in keys:
        raise DatasetCleanupError("Finalized dataset result requires reconciliation")
    _cleanup_uploaded(client, bucket, keys)


def cleanup_stale_run(
    settings: WorkerSettings,
    job_id: UUID,
    stale_run_token: UUID,
    current_run_token: UUID,
) -> None:
    _cleanup_stale_run(
        _client(settings),
        settings.object_storage_bucket,
        job_id,
        stale_run_token,
        current_run_token,
        settings.dataset_max_images * 2 + 3,
    )


def _safe_name(value: str, index: int) -> str:
    name = unicodedata.normalize("NFKC", value).replace("\\", "/").rsplit("/", 1)[-1]
    stem = Path(name).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip(".-_")[:80]
    return f"image_{index + 1:06d}_{stem or 'image'}.jpg"


def _download(
    client, bucket: str, item: dict[str, Any], target: Path, limit: int
) -> None:
    key = item.get("object_key")
    size = item.get("size_bytes")
    digest = item.get("sha256")
    if (
        item.get("schema_version") != 1
        or item.get("bucket") != bucket
        or not isinstance(key, str)
        or not key.startswith("jobs/")
        or ".." in key.split("/")
        or "\\" in key
        or type(size) is not int
        or size <= 0
        or size > limit
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise DatasetError("Dataset source reference is invalid")
    response = client.get_object(Bucket=bucket, Key=key)
    body = response["Body"]
    actual = 0
    sha = hashlib.sha256()
    try:
        with target.open("xb") as output:
            while chunk := body.read(1024 * 1024):
                actual += len(chunk)
                if actual > limit or actual > size:
                    raise DatasetError("Dataset source exceeds configured limits")
                sha.update(chunk)
                output.write(chunk)
    finally:
        body.close()
    if actual != size or sha.hexdigest() != digest:
        raise DatasetError("Dataset source integrity check failed")


def _zip_member_path(name: str, root: Path) -> Path:
    normalized = unicodedata.normalize("NFC", name).replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise DatasetError("ZIP entry path is unsafe")
    target = (root / Path(*path.parts)).resolve()
    if root not in target.parents:
        raise DatasetError("ZIP entry escaped the task workspace")
    return target


def extract_zip(
    archive: Path, root: Path, settings: WorkerSettings
) -> tuple[list[Path], int]:
    extracted: list[Path] = []
    ignored = 0
    names: set[str] = set()
    total = 0
    try:
        source = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as error:
        raise DatasetError("ZIP archive is invalid") from error
    with source:
        entries = source.infolist()
        if len(entries) > settings.dataset_zip_max_entries:
            raise DatasetError("ZIP entry limit exceeded")
        for info in entries:
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            if file_type and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise DatasetError("ZIP contains a non-regular entry")
            if info.flag_bits & 0x1:
                raise DatasetError("Encrypted ZIP entries are not supported")
            target = _zip_member_path(info.filename, root)
            leaf = target.name.casefold()
            if (
                "__macosx"
                in {part.casefold() for part in PurePosixPath(info.filename).parts}
                or leaf in _IGNORED_NAMES
            ):
                ignored += 1
                continue
            if info.is_dir():
                continue
            if target.suffix.lower() in {".zip", ".tar", ".gz", ".7z", ".rar"}:
                raise DatasetError("Nested archives are not supported")
            canonical = (
                unicodedata.normalize("NFC", info.filename)
                .replace("\\", "/")
                .casefold()
            )
            if canonical in names:
                raise DatasetError("ZIP contains duplicate normalized paths")
            names.add(canonical)
            if (
                info.compress_size > settings.dataset_zip_max_entry_compressed_bytes
                or info.file_size > settings.dataset_max_file_bytes
            ):
                raise DatasetError("ZIP entry size limit exceeded")
            if (
                info.file_size
                and info.file_size / max(1, info.compress_size)
                > settings.dataset_zip_max_ratio
            ):
                raise DatasetError("ZIP compression ratio limit exceeded")
            total += info.file_size
            if total > settings.dataset_zip_max_uncompressed_bytes:
                raise DatasetError("ZIP uncompressed byte limit exceeded")
            if target.suffix.lower() not in _IMAGE_SUFFIXES:
                raise DatasetError("ZIP contains an unsupported file")
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            actual = 0
            try:
                with source.open(info, "r") as incoming, target.open("xb") as output:
                    while chunk := incoming.read(1024 * 1024):
                        actual += len(chunk)
                        if (
                            actual > info.file_size
                            or actual > settings.dataset_max_file_bytes
                        ):
                            raise DatasetError("ZIP extracted byte limit exceeded")
                        output.write(chunk)
            except (OSError, EOFError, zipfile.BadZipFile) as error:
                raise DatasetError("ZIP decompression failed") from error
            if actual != info.file_size:
                raise DatasetError("ZIP metadata does not match extracted bytes")
            extracted.append(target)
    if not extracted:
        raise DatasetError("Dataset contains no supported images")
    if len(extracted) > settings.dataset_max_images:
        raise DatasetError("Image count limit exceeded")
    return extracted, ignored


def _magic(path: Path) -> str | None:
    with path.open("rb") as stream:
        header = stream.read(16)
    for prefix, content_type in _MAGIC:
        if header.startswith(prefix):
            if content_type == "image/webp" and header[8:12] != b"WEBP":
                return None
            return content_type
    return None


def _digest(path: Path) -> tuple[str, int]:
    sha = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            size += len(chunk)
            sha.update(chunk)
    return sha.hexdigest(), size


def _validate_container(path: Path, content_type: str) -> None:
    size = path.stat().st_size
    if content_type == "image/jpeg":
        _validate_jpeg(path)
        return
    if content_type == "image/webp":
        with path.open("rb") as stream:
            header = stream.read(12)
            if len(header) != 12 or int.from_bytes(header[4:8], "little") + 8 != size:
                raise DatasetError("WebP container size is invalid")
            while chunk_header := stream.read(8):
                if len(chunk_header) != 8:
                    raise DatasetError("WebP chunk header is truncated")
                chunk_type = chunk_header[:4]
                chunk_size = int.from_bytes(chunk_header[4:], "little")
                if chunk_type in {b"ANIM", b"ANMF"}:
                    raise DatasetError("Animated WebP is not supported")
                stream.seek(chunk_size + (chunk_size & 1), 1)
                if stream.tell() > size:
                    raise DatasetError("WebP chunk is truncated")
            if stream.tell() != size:
                raise DatasetError("WebP container has trailing data")
        return
    if content_type == "image/png":
        with path.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                raise DatasetError("PNG signature is invalid")
            ended = False
            while not ended:
                raw_length = stream.read(4)
                chunk_type = stream.read(4)
                if len(raw_length) != 4 or len(chunk_type) != 4:
                    raise DatasetError("PNG chunk header is truncated")
                length = int.from_bytes(raw_length, "big")
                if length > size or stream.tell() + length + 4 > size:
                    raise DatasetError("PNG chunk is truncated")
                payload = stream.read(length)
                expected_crc = stream.read(4)
                actual_crc = zlib.crc32(chunk_type + payload).to_bytes(4, "big")
                if expected_crc != actual_crc:
                    raise DatasetError("PNG chunk checksum is invalid")
                if chunk_type == b"acTL":
                    raise DatasetError("Animated PNG is not supported")
                ended = chunk_type == b"IEND"
            if stream.tell() != size:
                raise DatasetError("PNG container has trailing data")
        return
    raise DatasetError("Image format is unsupported")


def _validate_jpeg(path: Path) -> None:
    size = path.stat().st_size
    with path.open("rb") as stream:
        if size < 4 or stream.read(2) != b"\xff\xd8":
            raise DatasetError("JPEG container is invalid")
        in_scan = False
        frame_headers = 0
        while stream.tell() < size:
            marker_start = stream.tell()
            value = stream.read(1)
            if value != b"\xff":
                if not in_scan:
                    raise DatasetError("JPEG marker structure is invalid")
                continue
            marker_byte = stream.read(1)
            while marker_byte == b"\xff":
                marker_byte = stream.read(1)
            if len(marker_byte) != 1:
                raise DatasetError("JPEG marker is truncated")
            marker = marker_byte[0]
            if in_scan and marker == 0x00:
                continue
            if in_scan and 0xD0 <= marker <= 0xD7:
                continue
            if marker == 0xD9:
                if stream.tell() != size:
                    raise DatasetError("JPEG container has trailing data")
                if frame_headers != 1:
                    raise DatasetError("JPEG frame structure is invalid")
                return
            if in_scan:
                stream.seek(marker_start)
                in_scan = False
                continue
            if marker in {0x00, 0xD8} or 0xD0 <= marker <= 0xD7:
                raise DatasetError("JPEG marker structure is invalid")
            if marker == 0x01:
                continue
            raw_length = stream.read(2)
            if len(raw_length) != 2:
                raise DatasetError("JPEG segment length is truncated")
            segment_length = int.from_bytes(raw_length, "big")
            segment_end = stream.tell() + segment_length - 2
            if segment_length < 2 or segment_end > size:
                raise DatasetError("JPEG segment length is invalid")
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                frame_headers += 1
                if frame_headers > 1:
                    raise DatasetError("Multiple JPEG frames are not supported")
            stream.seek(segment_end)
            if marker == 0xDA:
                if frame_headers != 1:
                    raise DatasetError("JPEG scan precedes frame header")
                in_scan = True
        raise DatasetError("JPEG end marker is missing")


def _cleanup_uploaded(client, bucket: str, keys: list[str]) -> None:
    failures = 0
    for key in reversed(keys):
        deleted = False
        for retry in range(3):
            try:
                client.delete_object(Bucket=bucket, Key=key)
                deleted = True
                break
            except Exception:
                if retry < 2:
                    time.sleep(0.05 * (2**retry))
        if not deleted:
            failures += 1
    if failures:
        logger.error(
            "Dataset cleanup incomplete event=dataset_cleanup_failed failures=%s",
            failures,
        )
        raise DatasetCleanupError("Dataset attempt cleanup requires recovery")


def _decode_image(
    path: Path, content_type: str, settings: WorkerSettings
) -> np.ndarray:
    _validate_container(path, content_type)
    expected_format = _FORMAT_BY_TYPE[content_type]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as header:
                if (
                    header.format != expected_format
                    or getattr(header, "n_frames", 1) != 1
                ):
                    raise DatasetError("Image container variant is unsupported")
                width, height = header.size
                if (
                    width <= 0
                    or height <= 0
                    or width * height > settings.dataset_max_pixels
                ):
                    raise DatasetError("Image pixel limit exceeded")
                header.verify()
            with Image.open(path) as source:
                source.load()
                oriented = ImageOps.exif_transpose(source)
                if (
                    oriented.width <= 0
                    or oriented.height <= 0
                    or oriented.width * oriented.height > settings.dataset_max_pixels
                ):
                    raise DatasetError("Image pixel limit exceeded")
                if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                    rgba = oriented.convert("RGBA")
                    background = Image.new(
                        "RGBA", rgba.size, (*_ALPHA_BACKGROUND_RGB, 255)
                    )
                    rgb = Image.alpha_composite(background, rgba).convert("RGB")
                else:
                    rgb = oriented.convert("RGB")
                array = np.asarray(rgb, dtype=np.uint8)
    except (DatasetError, Image.DecompressionBombError):
        raise
    except (OSError, SyntaxError, ValueError, UnidentifiedImageError) as error:
        raise DatasetError("Image decode validation failed") from error
    if array.ndim != 3 or array.shape[2] != 3:
        raise DatasetError("Image decode result is invalid")
    return cv2.cvtColor(array, cv2.COLOR_RGB2BGR)


def _letterbox(image: np.ndarray) -> tuple[np.ndarray, float, dict[str, int]]:
    height, width = image.shape[:2]
    scale = min(1.0, 640 / width, 640 / height)
    out_width = max(1, round(width * scale))
    out_height = max(1, round(height * scale))
    resized = (
        image
        if scale == 1.0
        else cv2.resize(image, (out_width, out_height), interpolation=cv2.INTER_AREA)
    )
    top = (640 - out_height) // 2
    left = (640 - out_width) // 2
    canvas = np.full((640, 640, 3), 114, dtype=np.uint8)
    canvas[top : top + out_height, left : left + out_width] = resized
    return (
        canvas,
        scale,
        {
            "top": top,
            "right": 640 - out_width - left,
            "bottom": 640 - out_height - top,
            "left": left,
        },
    )


def _quality(
    image: np.ndarray, settings: WorkerSettings
) -> tuple[str, float, float, float, float, float, bool]:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    brightness = float(gray.mean())
    underexposed = float(np.count_nonzero(gray <= 10) / gray.size)
    overexposed = float(np.count_nonzero(gray >= 245) / gray.size)
    resolution_usable = bool(gray.size >= settings.dataset_min_pixels)
    score = min(1.0, sharpness / max(settings.dataset_normal_sharpness, 1.0))
    score *= min(1.0, brightness / max(settings.dataset_min_brightness, 1.0))
    if (
        not resolution_usable
        or sharpness < settings.dataset_unusable_sharpness
        or brightness < settings.dataset_unusable_brightness
        or underexposed >= 0.98
        or overexposed >= 0.98
    ):
        category = "unusable"
    elif (
        sharpness < settings.dataset_normal_sharpness
        or not settings.dataset_min_brightness
        <= brightness
        <= settings.dataset_max_brightness
        or underexposed > 0.5
        or overexposed > 0.5
    ):
        category = "challenging"
    else:
        category = "normal"
    return (
        category,
        round(sharpness, 4),
        round(brightness, 4),
        round(underexposed, 6),
        round(overexposed, 6),
        round(score, 6),
        resolution_usable,
    )


def _recommended(entries: list[dict[str, Any]]) -> list[int]:
    normal = [
        entry
        for entry in entries
        if entry["quality_category"] == "normal" and not entry["duplicate"]
    ]
    challenging = [
        entry
        for entry in entries
        if entry["quality_category"] == "challenging" and not entry["duplicate"]
    ]
    challenging.sort(
        key=lambda item: (-item["quality_score"], item["sha256"], item["index"])
    )
    cap = len(normal) // 4
    chosen = normal + challenging[:cap]
    return sorted(item["index"] for item in chosen)


def process_dataset(job, workspace: Path, settings: WorkerSettings) -> DatasetOutput:
    reference = job.source_reference
    if (
        not isinstance(reference, dict)
        or reference.get("schema_version") != 1
        or reference.get("kind") not in {"images", "zip"}
        or not isinstance(reference.get("items"), list)
    ):
        raise DatasetError("Dataset source is invalid")
    items = reference["items"]
    if not items or len(items) > settings.dataset_max_images:
        raise DatasetError("Dataset source count is invalid")
    root = workspace.resolve()
    _ensure_workspace(root, settings)
    inputs = root / "inputs"
    accepted_dir = root / "accepted"
    yolo_dir = root / "yolo"
    inputs.mkdir(mode=0o700)
    accepted_dir.mkdir(mode=0o700)
    yolo_dir.mkdir(mode=0o700)
    client = _client(settings)
    downloaded: list[Path] = []
    total = 0
    for index, item in enumerate(items):
        declared = item.get("size_bytes", 0)
        total += declared if type(declared) is int else 0
        if total > settings.dataset_max_total_bytes:
            raise DatasetError("Dataset total byte limit exceeded")
        suffix = (
            ".zip"
            if reference["kind"] == "zip"
            else Path(str(item.get("safe_name", ""))).suffix.lower()
        )
        target = inputs / f"source_{index:06d}{suffix}"
        _download(
            client,
            settings.object_storage_bucket,
            item,
            target,
            settings.dataset_zip_max_compressed_bytes
            if suffix == ".zip"
            else settings.dataset_max_file_bytes,
        )
        downloaded.append(target)
        _ensure_workspace(root, settings)
    ignored = 0
    if reference["kind"] == "zip":
        extract_root = root / "extracted"
        extract_root.mkdir(mode=0o700)
        sources, ignored = extract_zip(downloaded[0], extract_root, settings)
    else:
        sources = downloaded
    _ensure_workspace(root, settings)
    seen: dict[str, int] = {}
    entries: list[dict[str, Any]] = []
    for index, path in enumerate(sources):
        digest, size = _digest(path)
        content_type = _magic(path)
        expected_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        expected_type = expected_types.get(path.suffix.lower())
        if content_type is not None and content_type != expected_type:
            raise DatasetError("Image extension does not match its content")
        try:
            decoded = (
                _decode_image(path, content_type, settings)
                if content_type and content_type == expected_type
                else None
            )
        except DatasetError:
            decoded = None
        if decoded is None:
            entries.append(
                {
                    "index": index,
                    "filename": _safe_name(path.name, index),
                    "content_type": content_type or "application/octet-stream",
                    "size_bytes": size,
                    "sha256": digest,
                    "width": 0,
                    "height": 0,
                    "quality_category": "rejected",
                    "sharpness": 0.0,
                    "brightness": 0.0,
                    "underexposed_ratio": 0.0,
                    "overexposed_ratio": 0.0,
                    "resolution_usable": False,
                    "quality_score": 0.0,
                    "duplicate": digest in seen,
                    "object_key": None,
                    "yolo_object_key": None,
                    "yolo_size_bytes": 0,
                    "yolo_sha256": digest,
                    "output_width": 0,
                    "output_height": 0,
                    "resize_scale": 0.0,
                    "padding": {"top": 0, "right": 0, "bottom": 0, "left": 0},
                }
            )
            continue
        height, width = decoded.shape[:2]
        if width <= 0 or height <= 0 or width * height > settings.dataset_max_pixels:
            raise DatasetError("Image pixel limit exceeded")
        duplicate = digest in seen
        seen.setdefault(digest, index)
        (
            category,
            sharpness,
            brightness,
            underexposed,
            overexposed,
            quality_score,
            resolution_usable,
        ) = _quality(decoded, settings)
        name = _safe_name(path.name, index)
        sanitized = accepted_dir / name
        if not cv2.imwrite(str(sanitized), decoded, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise DatasetError("Image normalization failed")
        yolo, scale, padding = _letterbox(decoded)
        yolo_path = yolo_dir / name
        if not cv2.imwrite(str(yolo_path), yolo, [cv2.IMWRITE_JPEG_QUALITY, 95]):
            raise DatasetError("YOLO image creation failed")
        normalized_sha, normalized_size = _digest(sanitized)
        yolo_sha, yolo_size = _digest(yolo_path)
        _ensure_workspace(root, settings)
        entries.append(
            {
                "index": index,
                "filename": name,
                "content_type": "image/jpeg",
                "size_bytes": normalized_size,
                "sha256": normalized_sha,
                "width": width,
                "height": height,
                "quality_category": category,
                "sharpness": sharpness,
                "brightness": brightness,
                "underexposed_ratio": underexposed,
                "overexposed_ratio": overexposed,
                "resolution_usable": resolution_usable,
                "quality_score": quality_score,
                "duplicate": duplicate,
                "object_key": None,
                "yolo_object_key": None,
                "yolo_size_bytes": yolo_size,
                "yolo_sha256": yolo_sha,
                "output_width": 640,
                "output_height": 640,
                "resize_scale": round(scale, 8),
                "padding": padding,
            }
        )
    usable = [
        item
        for item in entries
        if item["quality_category"] not in {"unusable", "rejected"}
    ]
    if not usable:
        raise DatasetError("Dataset contains no decodable images")
    recommended = _recommended(entries)
    normal_count = sum(item["quality_category"] == "normal" for item in entries)
    challenging_count = sum(
        item["quality_category"] == "challenging" for item in entries
    )
    unusable_count = sum(item["quality_category"] == "unusable" for item in entries)
    rejected_count = sum(item["quality_category"] == "rejected" for item in entries)
    duplicate_count = sum(bool(item["duplicate"]) for item in entries)
    rec_challenging = sum(
        entries[index]["quality_category"] == "challenging" for index in recommended
    )
    actual_ratio = rec_challenging / len(recommended) if recommended else 0.0
    prefix = f"jobs/{job.id}/results/{job.run_token}"
    uploaded: list[str] = []
    summary = {
        "uploaded_files": len(sources),
        "accepted_files": len(usable),
        "normal": normal_count,
        "challenging": challenging_count,
        "unusable": unusable_count,
        "rejected": rejected_count,
        "duplicates": duplicate_count,
        "ignored_metadata_entries": ignored,
        "recommended_count": len(recommended),
        "recommended_normal": len(recommended) - rec_challenging,
        "recommended_challenging": rec_challenging,
        "target_challenging_ratio": 0.2,
        "actual_challenging_ratio": round(actual_ratio, 6),
        "ratio_note": None
        if recommended and actual_ratio <= 0.2
        else "Insufficient normal images for the target ratio",
    }
    manifest = {
        "schema_version": 1,
        "dataset_type": "image",
        "job_id": str(job.id),
        "run_token": str(job.run_token),
        "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "summary": summary,
        "recommended_indices": recommended,
        "images": entries,
        "exports": {},
    }
    try:
        for item in entries:
            if item["quality_category"] in {"unusable", "rejected"}:
                continue
            key = f"{prefix}/images/{item['filename']}"
            yolo_key = f"{prefix}/yolo/{item['filename']}"
            for local, object_key, digest in (
                (accepted_dir / item["filename"], key, item["sha256"]),
                (yolo_dir / item["filename"], yolo_key, item["yolo_sha256"]),
            ):
                uploaded.append(object_key)
                with local.open("rb") as body:
                    client.upload_fileobj(
                        body,
                        settings.object_storage_bucket,
                        object_key,
                        ExtraArgs={
                            "ContentType": "image/jpeg",
                            "Metadata": {"sha256": digest},
                        },
                    )
            item["object_key"] = key
            item["yolo_object_key"] = yolo_key
        for mode, selected in (
            (
                "accepted",
                [
                    item
                    for item in entries
                    if item["quality_category"] not in {"unusable", "rejected"}
                    and not item["duplicate"]
                ],
            ),
            ("yolo", [entries[index] for index in recommended]),
        ):
            archive_path = root / f"{mode}.zip"
            projected = (
                _workspace_bytes(root)
                + sum(
                    (
                        item["size_bytes"]
                        if mode == "accepted"
                        else item["yolo_size_bytes"]
                    )
                    for item in selected
                )
                + 1024 * 1024
            )
            if projected > settings.dataset_max_temp_bytes:
                raise DatasetError("Dataset temporary storage limit exceeded")
            try:
                with archive_path.open("xb") as raw_archive:
                    bounded_archive = _BoundedWriter(
                        raw_archive, settings.dataset_export_max_bytes
                    )
                    with zipfile.ZipFile(
                        bounded_archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6
                    ) as output:
                        for item in selected:
                            local = (
                                accepted_dir if mode == "accepted" else yolo_dir
                            ) / item["filename"]
                            output.write(local, f"images/{item['filename']}")
                        public = {
                            key: value
                            for key, value in manifest.items()
                            if key not in {"run_token", "exports"}
                        }
                        public["export_mode"] = mode
                        public["images"] = []
                        for item in selected:
                            public_item = {
                                key: value
                                for key, value in item.items()
                                if key
                                not in {
                                    "object_key",
                                    "yolo_object_key",
                                    "yolo_size_bytes",
                                    "yolo_sha256",
                                }
                            }
                            if mode == "yolo":
                                public_item.update(
                                    size_bytes=item["yolo_size_bytes"],
                                    sha256=item["yolo_sha256"],
                                    width=640,
                                    height=640,
                                    output_width=640,
                                    output_height=640,
                                    output_format="image/jpeg",
                                )
                            else:
                                public_item["output_format"] = "image/jpeg"
                            public["images"].append(public_item)
                        output.writestr(
                            "manifest.json",
                            json.dumps(
                                public,
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=True,
                            ),
                        )
            except Exception:
                archive_path.unlink(missing_ok=True)
                raise
            if archive_path.stat().st_size > settings.dataset_export_max_bytes:
                raise DatasetError("Dataset export byte limit exceeded")
            _ensure_workspace(root, settings)
            key = f"{prefix}/exports/{mode}.zip"
            archive_sha, archive_size = _digest(archive_path)
            uploaded.append(key)
            with archive_path.open("rb") as body:
                client.upload_fileobj(
                    body,
                    settings.object_storage_bucket,
                    key,
                    ExtraArgs={
                        "ContentType": "application/zip",
                        "Metadata": {"sha256": archive_sha},
                    },
                )
            manifest["exports"][mode] = {
                "object_key": key,
                "size_bytes": archive_size,
                "sha256": archive_sha,
            }
        manifest_key = f"{prefix}/manifest.json"
        uploaded.append(manifest_key)
        client.put_object(
            Bucket=settings.object_storage_bucket,
            Key=manifest_key,
            Body=json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode(),
            ContentType="application/json",
        )
    except Exception as original:
        try:
            _cleanup_uploaded(client, settings.object_storage_bucket, uploaded)
        except DatasetCleanupError as cleanup_error:
            raise cleanup_error from original
        raise
    return DatasetOutput(
        summary=summary,
        persisted=PersistedArtifacts(
            result_reference=f"s3://{settings.object_storage_bucket}/{manifest_key}",
            prefix=prefix,
            object_keys=tuple(uploaded),
        ),
    )
