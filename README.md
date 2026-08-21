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

This orchestration stage records processing summaries but intentionally does
not provide durable frame artifacts. Extracted frames are produced in a
job-scoped temporary workspace and cleaned after processing. A follow-up change
will persist frame manifests and artifacts to S3-compatible object storage.
