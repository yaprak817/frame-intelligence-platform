# Frame Intelligence Platform

Production-oriented platform for intelligent video frame selection, quality assessment, enhancement, and dataset generation.

## Project Goal

The goal of the Frame Intelligence Platform is to automatically extract high-quality, diverse, and annotation-ready frames from videos.

Instead of extracting every frame at fixed intervals, the platform evaluates visual quality, motion, exposure, similarity, and other signals to select more useful frames for machine learning datasets.

## Current Capabilities

The initial prototype includes:

- Adaptive candidate frame sampling foundation
- Sharpness analysis
- Exposure analysis
- Motion analysis
- Temporal duplicate detection
- Frame quality scoring
- Basic image enhancement

## Planned Architecture

Frontend:

- Next.js
- React
- TypeScript

Backend:

- Python
- FastAPI

Frame Processing:

- OpenCV
- PyTorch
- AI-based image quality assessment
- AI image enhancement

Infrastructure:

- PostgreSQL
- Redis
- MinIO / S3-compatible storage
- Docker
- GitHub Actions

## Repository Structure

```text
apps/
  backend/
  frontend/

services/
  frame-worker/

ml/

infrastructure/

tests/

docs/
```

## Asynchronous Processing

Video submissions are persisted with a transactional outbox. A dedicated
publisher sends minimal `job_id` messages to Redis, and the frame-worker claims
jobs from PostgreSQL before materializing URL or object-storage sources. Job
state, retries, execution leases, and result summaries remain authoritative in
PostgreSQL; Celery has no result backend.

Selected frames are uploaded from the job-scoped temporary workspace to private
S3-compatible storage. Each execution uses a run-scoped prefix and writes a
versioned `manifest.json` last as its commit marker. PostgreSQL stores the
manifest's stable internal `s3://` reference atomically with the result summary;
local frame paths and presigned URLs are never persisted.

## Result Artifact Access

Completed job artifacts remain private in object storage. `GET /api/v1/jobs/{job_id}`
returns only backend-owned result links. The result metadata and downloadable public
manifest endpoints validate the stored manifest before removing bucket names, object
keys, run tokens, and storage references. A frame can be accessed only by posting to
`/api/v1/jobs/{job_id}/result/frames/{frame_index}/access`; the backend verifies the
stored object's size and media type with `HEAD` before issuing a short-lived URL.

`OBJECT_STORAGE_ENDPOINT` is the internal service endpoint used for object reads.
`OBJECT_STORAGE_EXTERNAL_ENDPOINT` is used directly by the S3 signer and must be
reachable by API clients; signed URL hostnames are never rewritten. Configure
`RESULT_ARTIFACT_URL_TTL_SECONDS` between 30 and 900 seconds (default: 300).

The API currently operates in a single-tenant, trusted-client deployment model. There
is no authentication, user ownership, or token boundary in this phase. Result routes
use a dedicated authorization dependency seam so ownership enforcement can be added
without changing artifact selection or storage validation.
