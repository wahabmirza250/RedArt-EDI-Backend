"""Synthetic loopback SSH/SFTP integration; never contacts a real payer."""
import os
import socket
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paramiko
from django.test import override_settings
from apps.core.testing import AuthAPITestCase
from apps.claim.choices import ClaimStatus
from apps.edi.tests import EDIFixturesMixin
from apps.edi.models import SFTPCredentials, SFTPDirectory, EDI999Import, EDIAcknowledgement
from apps.edi.utils.handler import Generate837PHandler
from apps.edi.utils.sftp_client import _fingerprint_sha256, list_remote_files
from apps.edi.utils.upload import queue_edi_file_upload, run_edi_file_upload
from apps.edi.utils.import_999 import queue_edi_999_import_poll


class SyntheticAuth(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return paramiko.AUTH_SUCCESSFUL if (username, password) == ('synthetic', 'local-only-test') else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return 'password'

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == 'session' else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class SyntheticFilesystem(paramiko.SFTPServerInterface):
    def __init__(self, server, *args, root, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = Path(root).resolve()

    def local(self, remote):
        path = (self.root / remote.lstrip('/')).resolve()
        if not path.is_relative_to(self.root):
            raise PermissionError('Outside synthetic SFTP root')
        return path

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(self.local(path).stat())
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    lstat = stat

    def list_folder(self, path):
        try:
            result = []
            for item in self.local(path).iterdir():
                attr = paramiko.SFTPAttributes.from_stat(item.stat())
                attr.filename = item.name
                result.append(attr)
            return result
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def mkdir(self, path, attr):
        try:
            self.local(path).mkdir()
            return paramiko.SFTP_OK
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)

    def open(self, path, flags, attr):
        try:
            descriptor = os.open(self.local(path), flags | getattr(os, 'O_BINARY', 0), 0o600)
            mode = 'r+b' if flags & os.O_RDWR else ('wb' if flags & os.O_WRONLY else 'rb')
            stream = os.fdopen(descriptor, mode)
            handle = paramiko.SFTPHandle(flags)
            handle.readfile = stream
            handle.writefile = stream
            return handle
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class SyntheticSFTPServer:
    def __init__(self, root):
        self.root = root
        self.key = paramiko.RSAKey.generate(2048)
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1', 0))
        self.port = self.socket.getsockname()[1]
        self.socket.listen(5)
        self.socket.settimeout(.2)
        self.stopped = threading.Event()
        self.transports = []
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stopped.is_set():
            try:
                client, _ = self.socket.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            transport = paramiko.Transport(client)
            self.transports.append(transport)
            transport.add_server_key(self.key)
            transport.set_subsystem_handler('sftp', paramiko.SFTPServer, SyntheticFilesystem, root=self.root)
            try:
                transport.start_server(server=SyntheticAuth())
                while transport.is_active() and not self.stopped.wait(.02):
                    pass
            except (EOFError, OSError, paramiko.SSHException):
                pass
            finally:
                transport.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stopped.set()
        self.socket.close()
        for transport in self.transports:
            transport.close()
        self.thread.join(timeout=3)


@override_settings(DEBUG=False, EDI_SFTP_REQUIRE_HOST_FINGERPRINT=True,
                   CELERY_TASK_ALWAYS_EAGER=True, CELERY_TASK_EAGER_PROPAGATES=True)
class RealSFTPRoundtripTests(EDIFixturesMixin, AuthAPITestCase):
    def test_encrypted_upload_acknowledgment_replay_and_host_key_pin(self):
        with TemporaryDirectory(prefix='nemt-synthetic-sftp-') as directory:
            (Path(directory) / 'incoming').mkdir()
            with SyntheticSFTPServer(directory) as server:
                credential = SFTPCredentials.objects.create(
                    name='SYNTHETIC-LOOPBACK-ONLY', trading_partner=self.partner,
                    host='127.0.0.1', port=server.port, username='synthetic',
                    password='local-only-test', auth_type='PASSWORD', environment='TEST',
                    host_fingerprint=_fingerprint_sha256(server.key), timeout_seconds=5, is_active=True,
                )
                for purpose in ('OUTBOUND_837P', 'INBOUND_999'):
                    SFTPDirectory.objects.create(credentials=credential, name=purpose, purpose=purpose,
                        sending_path='/outgoing', receiving_path='/incoming', is_active=True)
                file, _, body = Generate837PHandler(self.batch.pk).generate()
                _, attempt, _, _ = queue_edi_file_upload(edi_file_id=file.pk, credentials_id=credential.pk)
                with patch('apps.edi.utils.upload.upload_bytes_to_s3', return_value=None):
                    outcome = run_edi_file_upload(edi_file_id=file.pk, attempt=attempt, credentials_id=credential.pk)
                self.assertEqual(outcome['status'], 'UPLOADED')
                self.assertEqual((Path(directory) / 'outgoing' / file.filename).read_bytes(), body.encode())
                credential.host_fingerprint = 'SHA256:deliberately-wrong-synthetic-pin'
                with self.assertRaisesRegex(ValueError, 'host key mismatch'):
                    list_remote_files(credentials=credential, remote_dir='/incoming')
                credential.host_fingerprint = _fingerprint_sha256(server.key)
                control = file.control_number
                acknowledgment = (
                    'ISA*00*          *00*          *ZZ*COMEDASSISTPROG*ZZ*89513013       '
                    f'*260817*1947*^*00501*{control.isa13}*0*T*:~'
                    f'GS*FA*COMEDASSISTPROG*89513013*20260817*1947*{control.gs06}*X*005010X231A1~'
                    f'ST*999*0001*005010X231A1~AK1*HC*{control.gs06}*005010X222A1~'
                    f'AK2*837*0001*005010X222A1~IK5*A~AK9*A*1*1*1~SE*6*0001~GE*1*{control.gs06}~IEA*1*{control.isa13}~'
                )
                (Path(directory) / 'incoming' / 'synthetic_999.x12').write_bytes(acknowledgment.encode())
                first = queue_edi_999_import_poll(credentials_id=credential.pk, batch_id=self.batch.pk)
                self.assertEqual(first['queued_count'], 1)
                row = EDI999Import.objects.get(pk=first['queued_import_ids'][0])
                self.assertEqual(row.status, 'IMPORTED')
                self.assertEqual(EDIAcknowledgement.objects.filter(edi_file=file).count(), 1)
                replay = queue_edi_999_import_poll(credentials_id=credential.pk, batch_id=self.batch.pk)
                self.assertEqual(replay['queued_count'], 0)
                self.assertEqual(EDIAcknowledgement.objects.filter(edi_file=file).count(), 1)
                self.claim.refresh_from_db()
                self.assertEqual(self.claim.status, ClaimStatus.EDI_ACCEPTED)
                self.assertNotEqual(self.claim.status, ClaimStatus.PAID)
