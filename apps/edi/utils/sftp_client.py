"""Paramiko SFTP helpers — always close the connection; verify host key when set."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import stat
from contextlib import contextmanager
from pathlib import PurePosixPath

import paramiko
from django.conf import settings

from apps.core.crypto_secrets import decrypt_secret
from apps.edi.choices import SFTPAuthType
from apps.edi.utils.pyx12_validation import (
    compact_preflight_error,
    validate_colorado_837p_preflight,
)

logger = logging.getLogger(__name__)

_HCPF_837P_FILENAME_RE = re.compile(
    r"^tp(?P<tpid>\d+)-837P-\d{17}-1of1\.x12$",
    re.IGNORECASE,
)


class EDI837PPreflightError(ValueError):
    """Raised before any network connection when outbound HCPF 837P is invalid."""


def max_sftp_download_bytes() -> int:
    return int(getattr(settings, "EDI_MAX_SFTP_DOWNLOAD_BYTES", 5_000_000))


@contextmanager
def open_sftp(credentials):
    """Yield an SFTPClient; always closes transport/client."""
    if not credentials:
        raise ValueError("SFTP credentials are required.")

    transport = None
    sftp = None
    try:
        transport = paramiko.Transport((credentials.host, int(credentials.port or 22)))
        transport.banner_timeout = credentials.timeout_seconds or 30
        transport.auth_timeout = credentials.timeout_seconds or 30
        transport.start_client(timeout=credentials.timeout_seconds or 30)
        _verify_host_key(transport, credentials)
        _connect_transport(transport, credentials)
        sftp = paramiko.SFTPClient.from_transport(transport)
        yield sftp
    finally:
        if sftp is not None:
            try:
                sftp.close()
            except Exception:
                logger.exception("Failed closing SFTP client")
        if transport is not None:
            try:
                transport.close()
            except Exception:
                logger.exception("Failed closing SFTP transport")


def _fingerprint_sha256(key) -> str:
    digest = hashlib.sha256(key.asbytes()).digest()
    import base64

    b64 = base64.b64encode(digest).decode("ascii").rstrip("=")
    return f"SHA256:{b64}"


def _verify_host_key(transport, credentials):
    expected = (getattr(credentials, "host_fingerprint", None) or "").strip()
    require = bool(getattr(settings, "EDI_SFTP_REQUIRE_HOST_FINGERPRINT", True))
    if not expected:
        if require and not getattr(settings, "DEBUG", False):
            raise ValueError(
                "SFTP host_fingerprint is required before connecting "
                "(set on credentials or EDI_SFTP_REQUIRE_HOST_FINGERPRINT=false)."
            )
        logger.warning(
            "SFTP host key verification skipped (no fingerprint) host=%s",
            credentials.host,
        )
        return

    key = transport.get_remote_server_key()
    actual = _fingerprint_sha256(key)
    # Allow either full SHA256:xxx or bare base64 / hex MD5 legacy forms.
    expected_norm = expected.strip()
    actual_bare = actual.split(":", 1)[-1]
    expected_bare = expected_norm.split(":", 1)[-1].replace(":", "")
    if expected_norm == actual or expected_bare == actual_bare.replace(":", ""):
        return
    # Also accept OpenSSH MD5 colon hex if stored that way.
    md5 = hashlib.md5(key.asbytes()).hexdigest()
    md5_colon = ":".join(md5[i : i + 2] for i in range(0, len(md5), 2))
    if expected_norm.lower() in (md5, md5_colon, f"md5:{md5}"):
        return
    raise ValueError(
        f"SFTP host key mismatch for {credentials.host}: "
        f"expected {expected_norm}, got {actual}"
    )


def _connect_transport(transport, credentials):
    auth = (credentials.auth_type or SFTPAuthType.PASSWORD).upper()
    password = decrypt_secret(credentials.password)
    pem = decrypt_secret(credentials.private_key_pem)
    passphrase = decrypt_secret(credentials.private_key_passphrase)

    if auth == SFTPAuthType.PASSWORD:
        if not password:
            raise ValueError("SFTP password is required for PASSWORD auth.")
        transport.auth_password(credentials.username, password)
    elif auth == SFTPAuthType.PRIVATE_KEY:
        key = _load_private_key(pem, passphrase)
        transport.auth_publickey(credentials.username, key)
    elif auth == SFTPAuthType.PASSWORD_AND_KEY:
        key = _load_private_key(pem, passphrase)
        try:
            transport.auth_publickey(credentials.username, key)
        except paramiko.AuthenticationException:
            transport.auth_password(credentials.username, password)
    else:
        raise ValueError(f"Unsupported SFTP auth_type: {auth}")


def _validate_hcpf_837p_before_network(*, filename: str, data: bytes) -> None:
    """Fail closed for RedArt's HCPF 837P filename convention before SFTP opens."""
    if "-837P-" not in (filename or "").upper():
        return

    match = _HCPF_837P_FILENAME_RE.match(filename or "")
    if match is None:
        raise EDI837PPreflightError(
            "HCPF 837P preflight blocked: filename does not match the required "
            "tp{TPID}-837P-{17-digit-stamp}-1of1.x12 pattern; file was not sent."
        )

    try:
        raw_x12 = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EDI837PPreflightError(
            "HCPF 837P preflight blocked: X12 is not valid UTF-8/ASCII text; file was not sent."
        ) from exc

    result = validate_colorado_837p_preflight(
        raw_x12,
        expected_tpid=match.group("tpid"),
        # The generator already derives ISA15 from the batch environment.  At
        # this lowest transport layer we independently require only legal T/P;
        # the Colorado validator performs that check when expected_usage is blank.
        expected_usage="",
    )
    if not result.get("valid"):
        detail = compact_preflight_error(result)
        raise EDI837PPreflightError(
            f"HCPF 837P preflight blocked: {detail}; file was not sent."
        )


def upload_bytes_via_sftp(*, credentials, remote_dir, filename, data: bytes) -> str:
    if not remote_dir:
        raise ValueError("SFTP remote directory is required.")
    if not filename:
        raise ValueError("Filename is required.")

    # Absolute last safety gate: validate the exact bytes that would be sent
    # before open_sftp() creates a network connection.  Local pyx12 999 output
    # is diagnostic only; the real HCPF acknowledgement must still come back
    # through the inbound SFTP polling/import path.
    _validate_hcpf_837p_before_network(filename=filename, data=data)

    remote_path = str(PurePosixPath(remote_dir.rstrip("/")) / filename)
    with open_sftp(credentials) as sftp:
        _ensure_remote_dir(sftp, remote_dir)
        with io.BytesIO(data) as buf:
            sftp.putfo(buf, remote_path)

    logger.info(
        "SFTP upload ok host=%s path=%s bytes=%s",
        credentials.host,
        remote_path,
        len(data),
    )
    return remote_path


def list_remote_files(*, credentials, remote_dir, max_files=None) -> list[dict]:
    if not remote_dir:
        raise ValueError("SFTP remote directory is required.")

    if max_files is None:
        max_files = int(getattr(settings, "EDI_SFTP_LIST_MAX_FILES", 5000))

    remote_dir = remote_dir.rstrip("/") or "/"
    results = []
    with open_sftp(credentials) as sftp:
        try:
            entries = sftp.listdir_attr(remote_dir)
        except FileNotFoundError as exc:
            raise ValueError(f"Remote directory not found: {remote_dir}") from exc

        for attr in entries:
            if len(results) >= max_files:
                logger.warning(
                    "SFTP list truncated at %s files for %s",
                    max_files,
                    remote_dir,
                )
                break
            mode = getattr(attr, "st_mode", None)
            if mode is not None and stat.S_ISDIR(mode):
                continue
            name = attr.filename
            if not name or name in (".", ".."):
                continue
            path = str(PurePosixPath(remote_dir) / name)
            results.append(
                {
                    "filename": name,
                    "remote_path": path,
                    "size": int(getattr(attr, "st_size", 0) or 0),
                    "mtime": getattr(attr, "st_mtime", None),
                }
            )
    return results


def download_bytes_via_sftp(*, credentials, remote_path) -> bytes:
    if not remote_path:
        raise ValueError("remote_path is required.")

    limit = max_sftp_download_bytes()
    with open_sftp(credentials) as sftp:
        try:
            attrs = sftp.stat(remote_path)
            size = int(getattr(attrs, "st_size", 0) or 0)
            if size > limit:
                raise ValueError(
                    f"Remote file too large ({size} bytes); max is {limit}."
                )
        except OSError as exc:
            raise ValueError(f"Unable to stat remote path: {remote_path}") from exc

        with io.BytesIO() as buf:
            sftp.getfo(remote_path, buf)
            data = buf.getvalue()

    if len(data) > limit:
        raise ValueError(f"Downloaded file exceeds max size ({limit} bytes).")

    logger.info(
        "SFTP download ok host=%s path=%s bytes=%s",
        credentials.host,
        remote_path,
        len(data),
    )
    return data


def _load_private_key(pem, passphrase):
    if not pem:
        raise ValueError("private_key_pem is required.")
    if isinstance(pem, str):
        stream = io.StringIO(pem)
    elif isinstance(pem, (bytes, bytearray)):
        stream = io.StringIO(pem.decode("utf-8"))
    else:
        stream = pem
    password = passphrase.encode("utf-8") if passphrase else None
    errors = []
    for loader in (
        paramiko.RSAKey.from_private_key,
        paramiko.ECDSAKey.from_private_key,
        paramiko.Ed25519Key.from_private_key,
    ):
        try:
            stream.seek(0)
            return loader(stream, password=password)
        except Exception as exc:
            errors.append(f"{loader.__name__}: {exc}")
            continue
    raise ValueError(
        "Unable to load private key PEM (" + "; ".join(errors[:3]) + ")."
    )


def _ensure_remote_dir(sftp, remote_dir):
    path = PurePosixPath(remote_dir)
    parts = []
    for part in path.parts:
        if part == "/":
            parts.append("")
            continue
        parts.append(part)
        current = "/".join(parts) if parts[0] == "" else "/".join(parts)
        if current in ("", "/"):
            continue
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)
