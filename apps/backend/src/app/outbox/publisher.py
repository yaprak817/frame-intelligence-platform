import asyncio
import logging
import os
import signal
from pathlib import Path

from sqlalchemy import text

from app.core.config import settings
from app.db.session import SessionFactory, engine
from app.outbox.celery_client import CeleryJobMessagePublisher
from app.outbox.repository import OutboxRepository

logger = logging.getLogger(__name__)


async def _signal_ready(client: CeleryJobMessagePublisher) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))
    await asyncio.to_thread(client.verify_connection)
    ready_value = os.environ.get("OUTBOX_READY_FILE")
    if ready_value:
        ready = Path(ready_value)
        temporary = ready.with_suffix(".tmp")
        temporary.write_text("ready", encoding="ascii")
        os.replace(temporary, ready)


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(signame, lambda *_args: loop.call_soon_threadsafe(stop.set))

    client = CeleryJobMessagePublisher(
        settings.celery_broker_url,
        settings.celery_task_queue,
        settings.celery_redis_keyprefix,
    )
    repository = OutboxRepository(
        SessionFactory,
        backoff_base_seconds=settings.outbox_backoff_base_seconds,
        backoff_max_seconds=settings.outbox_backoff_max_seconds,
    )
    try:
        await _signal_ready(client)
        while not stop.is_set():
            processed = await repository.publish_ready(
                client, settings.outbox_batch_size
            )
            if processed == 0:
                try:
                    await asyncio.wait_for(
                        stop.wait(), timeout=settings.outbox_poll_interval_seconds
                    )
                except TimeoutError:
                    pass
    finally:
        client.close()
        await engine.dispose()
        logger.info("Outbox publisher stopped")


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
