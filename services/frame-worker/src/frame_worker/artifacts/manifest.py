import json
from datetime import datetime
from typing import Any
from uuid import UUID

from frame_worker.artifacts.models import FrameManifestEntry


def manifest_bytes(
    *,
    job_id: UUID,
    run_token: UUID,
    created_at: datetime,
    summary: dict[str, int | float],
    frames: tuple[FrameManifestEntry, ...],
) -> bytes:
    if len(frames) != summary.get("frames_saved"):
        raise ValueError("Manifest frame count does not match processing summary")
    document: dict[str, Any] = {
        "schema_version": 1,
        "job_id": str(job_id),
        "run_token": str(run_token),
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "summary": summary,
        "frames": [frame.as_dict() for frame in frames],
    }
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
