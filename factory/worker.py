"""Start with: celery -A factory.worker:celery_app worker --loglevel=INFO."""
from celery import Celery

from factory.config import settings

celery_app = Celery("content_factory", broker=settings.redis_url)
celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=settings.worker_concurrency,
    task_soft_time_limit=max(30, settings.job_timeout - 60),
    task_time_limit=settings.job_timeout,
    broker_transport_options={"visibility_timeout": settings.job_timeout + 300, "socket_connect_timeout": 3, "socket_timeout": 3},
    broker_connection_timeout=3,
    broker_connection_retry_on_startup=True,
    task_ignore_result=True,
    task_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)


@celery_app.task(name="factory.execute_job", acks_late=True)
def execute_job(job_id):
    from factory.services.jobs import process_job
    return process_job(job_id)
