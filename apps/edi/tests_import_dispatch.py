from types import SimpleNamespace
from unittest.mock import Mock
from django.test import TestCase
from django.utils import timezone
from apps.edi.models import EDI999Import, EDI277Import, EDI835Import, SFTPCredentials, SFTPDirectory
from apps.edi.utils.import_dispatch import dispatch_import


class ImportDispatchTests(TestCase):
    def rows(self):
        credential = SFTPCredentials.objects.create(name='SYNTHETIC-DISPATCH', host='127.0.0.1', username='synthetic')
        directory = SFTPDirectory.objects.create(name='SYNTHETIC-IN', credentials=credential, receiving_path='/synthetic')
        return [model.objects.create(credentials=credential, directory=directory,
                 filename='synthetic.x12', remote_path='/synthetic.x12')
                for model in (EDI999Import, EDI277Import, EDI835Import)]

    def test_fast_worker_completion_survives_publisher_return(self):
        for row in self.rows():
            with self.subTest(model=type(row).__name__):
                finished = timezone.now()
                def complete(pk):
                    type(row).objects.filter(pk=pk).update(status='IMPORTED', message='Finished', finished_at=finished)
                    return SimpleNamespace(id='synthetic-task-id')
                dispatch_import(row, SimpleNamespace(delay=complete))
                row.refresh_from_db()
                self.assertEqual(row.status, 'IMPORTED')
                self.assertEqual(row.message, 'Finished')
                self.assertEqual(row.finished_at, finished)
                self.assertEqual(row.celery_task_id, 'synthetic-task-id')

    def test_broker_failure_is_visible_and_retryable(self):
        for row in self.rows():
            with self.subTest(model=type(row).__name__):
                task = SimpleNamespace(delay=Mock(side_effect=ConnectionError('synthetic broker failure')))
                with self.assertRaises(ConnectionError):
                    dispatch_import(row, task)
                row.refresh_from_db()
                self.assertEqual(row.status, 'FAILED')
                self.assertIn('retry', row.message)

    def test_lost_publish_response_does_not_undo_completed_import(self):
        for row in self.rows():
            with self.subTest(model=type(row).__name__):
                def complete_then_disconnect(pk):
                    type(row).objects.filter(pk=pk).update(status='IMPORTED', message='Finished')
                    raise ConnectionError('Synthetic lost response')
                with self.assertRaises(ConnectionError):
                    dispatch_import(row, SimpleNamespace(delay=complete_then_disconnect))
                row.refresh_from_db()
                self.assertEqual(row.status, 'IMPORTED')
                self.assertEqual(row.message, 'Finished')
