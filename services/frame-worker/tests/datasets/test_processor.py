import hashlib
import io
import json
import stat
import zipfile
from pathlib import Path
from uuid import uuid4

import cv2
import numpy as np
import pytest
from PIL import Image

from frame_worker.datasets.processor import (
    DatasetCleanupError,
    DatasetError,
    _BoundedWriter,
    _cleanup_stale_run,
    _cleanup_uploaded,
    _decode_image,
    _letterbox,
    _quality,
    _recommended,
    _validate_jpeg,
    extract_zip,
    process_dataset,
)
from frame_worker.orchestration.config import WorkerSettings
from frame_worker.orchestration.contracts import JobRecord


def settings(tmp_path: Path) -> WorkerSettings:
    return WorkerSettings(
        database_url="sqlite://",
        celery_broker_url="redis://localhost/0",
        job_source_encryption_key="VFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFRUVFQ=",
        object_storage_endpoint="http://localhost:9000",
        object_storage_access_key="test",
        object_storage_secret_key="test",
        object_storage_bucket="test",
        object_storage_region="us-east-1",
        object_storage_addressing_style="path",
        max_download_bytes=1024,
        lease_seconds=60,
        heartbeat_interval_seconds=10,
        visibility_timeout_seconds=120,
        worker_concurrency=1,
        processing_temp_root=tmp_path,
        dataset_max_file_bytes=1024 * 1024,
        dataset_max_total_bytes=4 * 1024 * 1024,
        dataset_zip_max_compressed_bytes=4 * 1024 * 1024,
        dataset_zip_max_uncompressed_bytes=4 * 1024 * 1024,
    )


def make_jpeg(path: Path, width: int = 320, height: int = 160) -> None:
    image = np.full((height, width, 3), 127, dtype=np.uint8)
    cv2.line(image, (0, 0), (width - 1, height - 1), (255, 255, 255), 3)
    assert cv2.imwrite(str(path), image)


@pytest.mark.parametrize("progressive", [False, True])
def test_decode_accepts_baseline_and_progressive_jpeg(
    tmp_path: Path, progressive: bool
) -> None:
    path = tmp_path / f"valid-{progressive}.jpg"
    Image.new("RGB", (32, 24), (10, 20, 30)).save(
        path, format="JPEG", progressive=progressive
    )
    assert _decode_image(path, "image/jpeg", settings(tmp_path)).shape == (24, 32, 3)


@pytest.mark.parametrize(
    "payload",
    [b"<script>x</script>", b"PK\x03\x04zip", b"\x00", b" \r\n"],
)
def test_jpeg_rejects_every_trailing_payload(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "trailing.jpg"
    make_jpeg(path)
    path.write_bytes(path.read_bytes() + payload)
    with pytest.raises(DatasetError, match="trailing"):
        _decode_image(path, "image/jpeg", settings(tmp_path))


def test_jpeg_rejects_concatenated_image_truncation_and_bad_length(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.jpg"
    second = tmp_path / "second.jpg"
    make_jpeg(first)
    make_jpeg(second, 64, 32)
    first.write_bytes(first.read_bytes() + second.read_bytes())
    with pytest.raises(DatasetError, match="trailing"):
        _decode_image(first, "image/jpeg", settings(tmp_path))
    second.write_bytes(second.read_bytes()[:-2])
    with pytest.raises(DatasetError, match="end marker"):
        _decode_image(second, "image/jpeg", settings(tmp_path))
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"\xff\xd8\xff\xe0\x00\x20short")
    with pytest.raises(DatasetError, match="length"):
        _decode_image(broken, "image/jpeg", settings(tmp_path))


def test_jpeg_accepts_entropy_byte_stuffing(tmp_path: Path) -> None:
    path = tmp_path / "stuffed.jpg"
    rng = np.random.default_rng(42)
    image = rng.integers(0, 256, size=(256, 256, 3), dtype=np.uint8)
    assert cv2.imwrite(str(path), image, [cv2.IMWRITE_JPEG_QUALITY, 100])
    assert b"\xff\x00" in path.read_bytes()
    assert _decode_image(path, "image/jpeg", settings(tmp_path)).shape == image.shape


def test_jpeg_parser_streams_without_read_bytes_or_large_reads(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "streamed.jpg"
    Image.new("RGB", (64, 64), (10, 20, 30)).save(path, progressive=True)
    monkeypatch.setattr(
        Path, "read_bytes", lambda _self: pytest.fail("read_bytes used")
    )
    _validate_jpeg(path)


def test_bounded_writer_stops_single_and_cumulative_overflow(tmp_path: Path) -> None:
    path = tmp_path / "bounded.bin"
    with path.open("wb") as raw:
        writer = _BoundedWriter(raw, 4)
        assert writer.write(b"1234") == 4
        with pytest.raises(DatasetError, match="limit"):
            writer.write(b"5")


def test_bounded_writer_seek_overwrite_truncate_and_zip_directory(
    tmp_path: Path,
) -> None:
    path = tmp_path / "bounded.zip"
    with path.open("w+b") as raw:
        writer = _BoundedWriter(raw, 256)
        writer.write(b"1234")
        writer.seek(1)
        writer.write(b"X")
        assert writer.truncate(4) == 4
        writer.seek(0)
        assert raw.read() == b"1X34"
        with pytest.raises(DatasetError):
            writer.truncate(257)
    with path.open("w+b") as raw:
        writer = _BoundedWriter(raw, 40)
        with pytest.raises(DatasetError):
            with zipfile.ZipFile(writer, "w") as archive:
                archive.writestr("entry.txt", b"payload")
        assert path.stat().st_size <= 40
    with path.open("wb") as raw:
        writer = _BoundedWriter(raw, 4)
        writer.write(b"12")
        writer.write(b"34")
        with pytest.raises(DatasetError, match="limit"):
            writer.write(b"5")


def test_cleanup_retries_and_reports_exhaustion() -> None:
    class Client:
        def __init__(self, failures: int) -> None:
            self.failures = failures
            self.calls = 0

        def delete_object(self, **_kwargs):
            self.calls += 1
            if self.calls <= self.failures:
                raise OSError("private cleanup detail")

    recovered = Client(1)
    _cleanup_uploaded(recovered, "bucket", ["attempt/key"])
    assert recovered.calls == 2
    exhausted = Client(3)
    with pytest.raises(DatasetCleanupError, match="requires recovery"):
        _cleanup_uploaded(exhausted, "bucket", ["attempt/key"])
    assert exhausted.calls == 3


def test_stale_cleanup_is_exactly_fenced_and_preserves_inventory() -> None:
    job_id, stale, current, other = uuid4(), uuid4(), uuid4(), uuid4()
    stale_prefix = f"jobs/{job_id}/results/{stale}/"
    preserved = {
        f"jobs/{job_id}/results/{current}/active.tmp": b"active",
        f"jobs/{job_id}/results/{other}/manifest.json": b"final",
        f"jobs/{uuid4()}/results/{stale}/other-job": b"other",
    }

    class Client:
        def __init__(self):
            self.objects = {stale_prefix + "partial": b"partial", **preserved}

        def list_objects_v2(self, **kwargs):
            return {
                "Contents": [
                    {"Key": key}
                    for key in self.objects
                    if key.startswith(kwargs["Prefix"])
                ]
            }

        def delete_object(self, *, Bucket, Key):
            self.objects.pop(Key)

    client = Client()
    _cleanup_stale_run(client, "bucket", job_id, stale, current, 10)
    assert client.objects == preserved
    with pytest.raises(DatasetCleanupError, match="identity"):
        _cleanup_stale_run(client, "bucket", job_id, current, current, 10)


def test_stale_cleanup_never_deletes_a_finalized_result() -> None:
    job_id, stale, current = uuid4(), uuid4(), uuid4()
    prefix = f"jobs/{job_id}/results/{stale}/"

    class Client:
        def __init__(self):
            self.objects = {
                prefix + "images/image.jpg": b"image",
                prefix + "exports/accepted.zip": b"zip",
                prefix + "manifest.json": b"manifest",
            }
            self.deleted = []

        def list_objects_v2(self, **kwargs):
            return {
                "Contents": [
                    {"Key": key}
                    for key in self.objects
                    if key.startswith(kwargs["Prefix"])
                ]
            }

        def delete_object(self, *, Bucket, Key):
            self.deleted.append((Bucket, Key))
            self.objects.pop(Key)

    client = Client()
    before = dict(client.objects)
    with pytest.raises(DatasetCleanupError, match="reconciliation"):
        _cleanup_stale_run(client, "bucket", job_id, stale, current, 10)
    assert client.objects == before
    assert client.deleted == []


@pytest.mark.parametrize("extension", [".jpg", ".png", ".webp"])
def test_decode_rejects_trailing_polyglot_data(tmp_path: Path, extension: str) -> None:
    path = tmp_path / f"polyglot{extension}"
    Image.new("RGB", (8, 6), (10, 20, 30)).save(path)
    path.write_bytes(path.read_bytes() + b"<script>polyglot</script>")
    content_type = {
        ".jpg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
    }[extension]
    with pytest.raises(DatasetError, match="trailing|size"):
        _decode_image(path, content_type, settings(tmp_path))


@pytest.mark.parametrize("extension", [".png", ".webp"])
def test_decode_rejects_animated_images(tmp_path: Path, extension: str) -> None:
    path = tmp_path / f"animated{extension}"
    frames = [Image.new("RGB", (8, 6), color) for color in ((255, 0, 0), (0, 255, 0))]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=100, loop=0)
    content_type = ".png" == extension and "image/png" or "image/webp"
    with pytest.raises(DatasetError, match="Animated"):
        _decode_image(path, content_type, settings(tmp_path))


def test_decode_applies_exif_orientation_and_returns_bgr(tmp_path: Path) -> None:
    path = tmp_path / "oriented.jpg"
    source = Image.new("RGB", (6, 4), (240, 20, 10))
    exif = source.getexif()
    exif[274] = 6
    source.save(path, quality=100, subsampling=0, exif=exif)
    decoded = _decode_image(path, "image/jpeg", settings(tmp_path))
    assert decoded.shape[:2] == (6, 4)
    assert decoded[0, 0, 2] > decoded[0, 0, 0]


def test_decode_composites_alpha_on_fixed_background(tmp_path: Path) -> None:
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (4, 3), (255, 0, 0, 0)).save(path)
    decoded = _decode_image(path, "image/png", settings(tmp_path))
    assert np.all(decoded == np.array([114, 114, 114], dtype=np.uint8))


@pytest.mark.parametrize("name", ["../escape.jpg", "/absolute.jpg", "C:\\evil.jpg"])
def test_zip_rejects_traversal_and_absolute_paths(tmp_path: Path, name: str) -> None:
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(name, b"image")
    root = tmp_path / "extract"
    root.mkdir()
    with pytest.raises(DatasetError, match="unsafe"):
        extract_zip(archive, root, settings(tmp_path))


def test_zip_rejects_symlink_nested_archive_and_casefold_collision(
    tmp_path: Path,
) -> None:
    root = tmp_path / "extract"
    root.mkdir()
    archive = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("link.jpg")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(info, "target")
    with pytest.raises(DatasetError, match="non-regular"):
        extract_zip(archive, root, settings(tmp_path))

    archive = tmp_path / "nested.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("nested.zip", b"PK")
    with pytest.raises(DatasetError, match="Nested"):
        extract_zip(archive, root, settings(tmp_path))

    archive = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("A.jpg", b"one")
        output.writestr("a.JPG", b"two")
    with pytest.raises(DatasetError, match="duplicate"):
        extract_zip(archive, root, settings(tmp_path))


def test_zip_ignores_known_metadata_and_extracts_to_task_root(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    make_jpeg(source)
    archive = tmp_path / "valid.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.write(source, "safe/sub/image.jpg")
        output.writestr("__MACOSX/._image.jpg", b"metadata")
        output.writestr(".DS_Store", b"metadata")
    root = tmp_path / "extract"
    root.mkdir()
    images, ignored = extract_zip(archive, root, settings(tmp_path))
    assert ignored == 2
    assert len(images) == 1
    assert root.resolve() in images[0].resolve().parents


def test_letterbox_is_640_square_preserves_ratio_and_never_upscales() -> None:
    small = np.zeros((100, 200, 3), dtype=np.uint8)
    output, scale, padding = _letterbox(small)
    assert output.shape == (640, 640, 3)
    assert scale == 1.0
    assert padding == {"top": 270, "right": 220, "bottom": 270, "left": 220}
    large = np.zeros((1000, 2000, 3), dtype=np.uint8)
    output, scale, padding = _letterbox(large)
    assert output.shape == (640, 640, 3)
    assert scale == pytest.approx(0.32)
    assert padding["top"] == padding["bottom"] == 160


def test_quality_and_recommendation_are_deterministic(tmp_path: Path) -> None:
    config = settings(tmp_path)
    image = np.indices((200, 200)).sum(axis=0).astype(np.uint8)
    image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
    assert _quality(image, config) == _quality(image.copy(), config)
    entries = [
        {
            "index": index,
            "quality_category": "normal",
            "duplicate": False,
            "quality_score": 1.0,
            "sha256": f"{index:064x}",
        }
        for index in range(8)
    ] + [
        {
            "index": 8 + index,
            "quality_category": "challenging",
            "duplicate": False,
            "quality_score": 0.8 - index / 10,
            "sha256": f"{8 + index:064x}",
        }
        for index in range(4)
    ]
    assert _recommended(entries) == list(range(10))
    assert _recommended(entries) == _recommended(list(reversed(entries)))


def recommendation_entry(index, category="normal", duplicate=False, score=1.0):
    return {
        "index": index,
        "quality_category": category,
        "duplicate": duplicate,
        "quality_score": score,
        "sha256": f"{index:064x}",
    }


@pytest.mark.parametrize("count", [1, 2, 3, 4, 5])
def test_recommendation_small_normal_batches_do_not_duplicate(count: int) -> None:
    entries = [recommendation_entry(index) for index in range(count)]
    assert _recommended(entries) == list(range(count))


def test_recommendation_edge_case_matrix() -> None:
    challenging_only = [
        recommendation_entry(index, "challenging", score=0.5) for index in range(5)
    ]
    assert _recommended(challenging_only) == []
    mixed = [recommendation_entry(index) for index in range(4)] + [
        recommendation_entry(4, "challenging", score=0.4),
        recommendation_entry(5, "challenging", score=0.9),
        recommendation_entry(6, "challenging", duplicate=True, score=1.0),
    ]
    selected = _recommended(mixed)
    assert selected == [0, 1, 2, 3, 5]
    assert len(selected) == len(set(selected))
    assert (
        sum(mixed[index]["quality_category"] == "challenging" for index in selected)
        / len(selected)
        == 0.2
    )
    tied = [recommendation_entry(index) for index in range(8)] + [
        recommendation_entry(8, "challenging", score=0.5),
        recommendation_entry(9, "challenging", score=0.5),
        recommendation_entry(10, "challenging", score=0.5),
    ]
    assert _recommended(tied) == _recommended(list(reversed(tied))) == list(range(10))


@pytest.mark.parametrize("value", [0, 10, 35, 225, 245, 255])
def test_quality_metrics_are_finite_and_bounded(tmp_path: Path, value: int) -> None:
    image = np.full((32, 32, 3), value, dtype=np.uint8)
    result = _quality(image, settings(tmp_path))
    for metric in result[1:6]:
        assert np.isfinite(metric)
    assert 0 <= result[3] <= 1
    assert 0 <= result[4] <= 1
    assert 0 <= result[5] <= 1


class FakeS3:
    def __init__(self, sources: dict[str, bytes]) -> None:
        self.objects = dict(sources)
        self.metadata: dict[str, dict[str, str]] = {}

    def get_object(self, *, Bucket, Key):
        payload = self.objects[Key]
        return {"Body": io.BytesIO(payload), "ContentLength": len(payload)}

    def upload_fileobj(self, body, bucket, key, ExtraArgs):
        self.objects[key] = body.read()
        self.metadata[key] = ExtraArgs.get("Metadata", {})

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[Key] = Body

    def delete_object(self, *, Bucket, Key):
        self.objects.pop(Key, None)


def test_full_dataset_processing_supports_jpeg_png_webp_and_public_zip(
    tmp_path: Path, monkeypatch
) -> None:
    sources: dict[str, bytes] = {}
    items = []
    for index, extension in enumerate((".jpg", ".png", ".webp")):
        image = np.full((80 + index * 10, 120, 3), 80 + index * 50, np.uint8)
        cv2.line(image, (0, 0), (119, 70), (255, 255, 255), 2)
        ok, encoded = cv2.imencode(extension, image)
        assert ok
        payload = encoded.tobytes()
        key = f"jobs/job/source/images/{index:06d}{extension}"
        sources[key] = payload
        items.append(
            {
                "schema_version": 1,
                "bucket": "test",
                "object_key": key,
                "version_id": None,
                "etag": None,
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                "safe_name": f"sample-{index}{extension}",
            }
        )
    fake = FakeS3(sources)
    monkeypatch.setattr(
        "frame_worker.datasets.processor._client", lambda _settings: fake
    )
    job_id, run_token = uuid4(), uuid4()
    job = JobRecord(
        id=job_id,
        status="RUNNING",
        source_type="IMAGE_DATASET",
        source_secret=None,
        source_reference={"schema_version": 1, "kind": "images", "items": items},
        processing_config={"dataset_schema_version": 1},
        attempt_count=1,
        run_token=run_token,
        lease_expires_at=None,
        result_reference=None,
        result_summary=None,
        version=2,
    )
    work = tmp_path / "work"
    work.mkdir()
    result = process_dataset(job, work, settings(tmp_path))
    assert result.summary["uploaded_files"] == 3
    manifest_key = f"jobs/{job_id}/results/{run_token}/manifest.json"
    manifest = json.loads(fake.objects[manifest_key])
    assert len(manifest["images"]) == 3
    assert {image["width"] for image in manifest["images"]} == {120}
    for mode in ("accepted", "yolo"):
        key = manifest["exports"][mode]["object_key"]
        assert fake.metadata[key]["sha256"] == manifest["exports"][mode]["sha256"]
        with zipfile.ZipFile(io.BytesIO(fake.objects[key])) as archive:
            public = json.loads(archive.read("manifest.json"))
            assert "run_token" not in public
            assert "object_key" not in json.dumps(public)
            public_images = {item["filename"]: item for item in public["images"]}
            for name in [
                item for item in archive.namelist() if item != "manifest.json"
            ]:
                payload = archive.read(name)
                item = public_images[Path(name).name]
                assert item["size_bytes"] == len(payload)
                assert item["sha256"] == hashlib.sha256(payload).hexdigest()
                decoded = cv2.imdecode(
                    np.frombuffer(payload, np.uint8), cv2.IMREAD_COLOR
                )
                assert decoded.shape[1] == item["width"]
                assert decoded.shape[0] == item["height"]
                if mode == "yolo":
                    assert decoded.shape[:2] == (640, 640)
    accepted = manifest["images"][0]
    assert accepted["sha256"] != accepted["yolo_sha256"]
