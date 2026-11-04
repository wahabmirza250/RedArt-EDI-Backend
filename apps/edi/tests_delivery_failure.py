from unittest.mock import patch
from django.urls import reverse
from apps.core.testing import AuthAPITestCase
from apps.edi.tests import EDIFixturesMixin
from apps.edi.models import SFTPCredentials, SFTPDirectory, EDIFileTransferLog
from apps.edi.choices import EDIFileStatus, TransferLogStatus
from apps.edi.utils.handler import Generate837PHandler


class DeliveryFailureTests(EDIFixturesMixin, AuthAPITestCase):
    def test_sftp_failure_never_returns_success_even_when_archive_succeeds(self):
        file, _, _ = Generate837PHandler(self.batch.id).generate()
        credential = SFTPCredentials.objects.create(
            name='SYNTHETIC-FAILURE', trading_partner=self.partner, environment='TEST',
            host='127.0.0.1', username='synthetic', password='not-a-real-password', is_active=True,
        )
        SFTPDirectory.objects.create(
            name='synthetic-outbound', credentials=credential, purpose='OUTBOUND_837P',
            sending_path='/synthetic/send', receiving_path='/synthetic/recv', is_active=True,
        )
        with patch('apps.edi.utils.upload.upload_bytes_via_sftp', side_effect=OSError('Synthetic connection failure')), \
             patch('apps.edi.utils.upload.upload_bytes_to_s3', return_value='s3://synthetic/archive.x12'):
            response = self.client.post(reverse('edi-file-queue-upload', kwargs={'pk': file.pk}), {}, format='json')
        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.data['success'])
        file.refresh_from_db()
        self.assertEqual(file.status, EDIFileStatus.FAILED)
        self.assertIsNone(file.uploaded_at)
        self.assertTrue(EDIFileTransferLog.objects.filter(
            edi_file=file, channel='SFTP', status=TransferLogStatus.FAILED).exists())
        self.assertTrue(EDIFileTransferLog.objects.filter(
            edi_file=file, channel='S3', status=TransferLogStatus.SUCCESS).exists())
