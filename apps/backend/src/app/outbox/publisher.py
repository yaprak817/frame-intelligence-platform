import asyncio
import logging
import signal

from app.core.config import settings
from app.db.session import SessionFactory, engine
from app.outbox.celery_client import CeleryJobMessagePublisher
from app.outbox.repository import OutboxRepository

logger = logging.getLogger(__name__)


async def run() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop.set)
        except NotImplementedError:  # Windows event loops
            signal.signal(signame, lambda *_args: loop.call_soon_threadsafe(stop.set))

    client = CeleryJobMessagePublisher(settings.celery_broker_url)
    repository = OutboxRepository(
        SessionFactory,
        backoff_base_seconds=settings.outbox_backoff_base_seconds,
        backoff_max_seconds=settings.outbox_backoff_max_seconds,
    )
    try:
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
