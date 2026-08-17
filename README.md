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