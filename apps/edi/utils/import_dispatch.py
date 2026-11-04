"""Publish import tasks without overwriting a fast worker's final state."""
from django.utils import timezone


def dispatch_import(row, task, *args):
    row.status = 'QUEUED'
    row.message = 'Importing started (Celery task queued).'
    row.save(update_fields=['status', 'message', 'updated_at'])
    try:
        result = task.delay(row.pk, *args)
    except Exception:
        # Keep failed publication visible and eligible for the next poll.
        # A response lost after successful execution must not undo completion.
        type(row).objects.filter(pk=row.pk, status='QUEUED').update(
            status='FAILED', message='Unable to queue import; retry import polling.',
            updated_at=timezone.now(),
        )
        raise
    row.celery_task_id = result.id
    row.save(update_fields=['celery_task_id', 'updated_at'])
    return result
