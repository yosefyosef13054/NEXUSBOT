from __future__ import annotations

from celery import Celery
from celery.signals import worker_process_init

from app.config import settings
from app.core.logger import configure_logging

celery = Celery(
    "nexusbot",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.workers.tasks"],
)

celery.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    task_routes={
        "app.workers.tasks.ingest_document_task": {"queue": "ingest"},
    },
    broker_connection_retry_on_startup=True,
    timezone="UTC",
)


@worker_process_init.connect
def _on_worker_start(**_: object) -> None:
    configure_logging()
