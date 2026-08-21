FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.12.5 \
    && addgroup --system worker \
    && adduser --system --ingroup worker worker \
    && mkdir -p /tmp/frame-intelligence \
    && chown worker:worker /tmp/frame-intelligence

WORKDIR /worker
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

USER worker
ENV PATH="/worker/.venv/bin:$PATH" \
    PYTHONPATH="/worker/src" \
    PROCESSING_TEMP_ROOT="/tmp/frame-intelligence"

CMD ["celery", "-A", "frame_worker.orchestration.celery_app:celery_app", "worker", "--loglevel=INFO", "--queues=video-processing"]
