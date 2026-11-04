"""Celery maintenance tasks for the EDI microservice."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="redartdigital.tasks.cleanup_celery_storage")
def cleanup_celery_storage() -> dict:
    """Leave result expiry to Celery, preserving queued and in-flight work.

    The legacy task name is retained for existing database schedules. Redis
    result backends expire individual results using result_expires. Never flush
    a database: deployments may share it with the broker or other services.
    """
    summary = {
        "action": "result_expiry_managed_by_celery",
        "deleted_keys": 0,
    }
    logger.info("Celery maintenance: %s", summary)
    return summary
