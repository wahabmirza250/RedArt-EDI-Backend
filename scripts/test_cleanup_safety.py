import importlib.util
import logging
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

class CleanupSafety(unittest.TestCase):
    def load_task(self):
        celery = types.ModuleType('celery')
        names = []
        def shared_task(**kwargs):
            names.append(kwargs['name'])
            return lambda function: function
        celery.shared_task = shared_task
        # No Redis or Django access is available: maintenance must not touch
        # pending broker work even when results share the same database.
        with patch.dict(sys.modules, {'celery': celery, 'redis': None, 'django': None}):
            spec = importlib.util.spec_from_file_location('edi_cleanup', Path(__file__).resolve().parents[1] / 'redartdigital' / 'tasks.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module, names

    def test_legacy_schedule_name_preserved(self):
        _, names = self.load_task()
        self.assertEqual(names, ['redartdigital.tasks.cleanup_celery_storage'])

    def test_no_database_writes_or_connection(self):
        module, _ = self.load_task()
        self.assertEqual(module.cleanup_celery_storage()['deleted_keys'], 0)

    def test_repeatable_result_contains_no_connection_secrets(self):
        module, _ = self.load_task()
        with self.assertLogs(module.logger, level=logging.INFO) as captured:
            first = module.cleanup_celery_storage()
            second = module.cleanup_celery_storage()
        self.assertEqual(first, second)
        self.assertNotIn('redis://', str(first) + str(captured.output))

if __name__ == '__main__':
    unittest.main()