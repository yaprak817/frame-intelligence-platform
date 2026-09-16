FROM python:3.13.7-slim-bookworm AS model
ARG YOLO_MODEL_URL=https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt
ARG YOLO_MODEL_SHA256=0ebbc80d4a7680d14987a577cd21342b65ecfd94632bd9a8da63ae6417644ee1
RUN apt-get update && apt-get install --no-install-recommends -y ca-certificates curl \
    && mkdir -p /opt/models \
    && curl --fail --location --proto '=https' --tlsv1.2 "$YOLO_MODEL_URL" -o /opt/models/yolo11n.pt \
    && echo "$YOLO_MODEL_SHA256  /opt/models/yolo11n.pt" | sha256sum -c - \
    && rm -rf /var/lib/apt/lists/*

FROM python:3.13.7-slim-bookworm AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    YOLO_MODEL_PATH=/opt/models/yolo11n.pt \
    YOLO_OFFLINE=true PATH="/worker/.venv/bin:$PATH" PYTHONPATH=/worker/src \
    PROCESSING_TEMP_ROOT=/tmp/frame-intelligence
RUN apt-get update && apt-get install --no-install-recommends -y libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv==0.12.5 \
    && addgroup --system worker && adduser --system --ingroup worker worker \
    && mkdir -p /tmp/frame-intelligence && chown worker:worker /tmp/frame-intelligence
ENV YOLO_CONFIG_DIR=/tmp/frame-intelligence
WORKDIR /worker
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --extra ml --no-install-project
COPY src ./src
COPY --from=model /opt/models/yolo11n.pt /opt/models/yolo11n.pt
RUN uv sync --locked --no-dev --extra ml
USER worker
CMD ["celery", "-A", "frame_worker.orchestration.celery_app:celery_app", "worker", "--loglevel=INFO", "--queues=annotation-ml", "--concurrency=1"]
