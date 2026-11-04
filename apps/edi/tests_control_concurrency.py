from concurrent.futures import ThreadPoolExecutor
from django.db import close_old_connections
from django.test import TransactionTestCase
from apps.edi.tests_enterprise import EnterpriseFixturesMixin
from apps.edi.utils.service import allocate_control_numbers


class ControlAllocationConcurrency(EnterpriseFixturesMixin, TransactionTestCase):
    def test_parallel_regenerations_preserve_distinct_controls(self):
        batch_id = self.batch.id
        def allocate(_):
            close_old_connections()
            try:
                row, created = allocate_control_numbers(batch_id=batch_id, force_new=True)
                return row.pk, row.isa13, row.gs06, created
            finally:
                close_old_connections()
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(allocate, range(24)))
        for column in range(3):
            self.assertEqual(len({row[column] for row in rows}), 24)
        self.assertTrue(all(row[3] for row in rows))
