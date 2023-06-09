# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
import hashlib
import tempfile
import warnings
from logging import DEBUG, getLogger
from os import rename
from os.path import basename, exists, getsize, join

from ... import CondaError
from ...auxlib.ish import dals
from ...auxlib.logz import stringify
from ...base.context import context
from ...common.io import time_recorder
from ...exceptions import (
    BasicClobberError,
    ChecksumMismatchError,
    CondaDependencyError,
    CondaHTTPError,
    CondaSSLError,
    ProxyError,
    maybe_raise,
)
from ..disk.delete import rm_rf
from . import (
    ConnectionError,
    HTTPError,
    InsecureRequestWarning,
    InvalidSchema,
    RequestsProxyError,
    SSLError,
)
from .session import CondaSession

log = getLogger(__name__)


def disable_ssl_verify_warning():
    warnings.simplefilter("ignore", InsecureRequestWarning)


@time_recorder("download")
def download(
    url,
    target_full_path,
    md5=None,
    sha256=None,
    size=None,
    progress_update_callback=None,
):
    if exists(target_full_path):
        maybe_raise(BasicClobberError(target_full_path, url, context), context)
    if not context.ssl_verify:
        disable_ssl_verify_warning()

    # Use a .part file to be able to resume downloading in case of failures.
    target_part_path = target_full_path + ".part"
    target_part_size = 0
    if exists(target_part_path):
        target_part_size = getsize(target_part_path)

    try:
        timeout = context.remote_connect_timeout_secs, context.remote_read_timeout_secs
        session = CondaSession()
        headers = {
            "Range": "bytes=%s-" % target_part_size
        }  # Attempt to resume downloading.
        resp = session.get(
            url, stream=True, proxies=session.proxies, timeout=timeout, headers=headers
        )
        if log.isEnabledFor(DEBUG):
            log.debug(stringify(resp, content_max_len=256))
        resp.raise_for_status()

        content_length = int(resp.headers.get("Content-Length", 0))
        # Check whether 'Range' is supported by the server.
        resume_download = resp.headers.get(
            "Accept-Ranges", "none"
        ) == "bytes" or resp.headers.get("Content-Range", "none").startswith(
            "bytes %s" % target_part_size
        )

        total_target_size = None
        if content_length:
            total_target_size = content_length + target_part_size
            if size is not None and size == content_length:
                # The server is responding with full-sized file, no need to resume the download.
                resume_download = False

        file_open_flags = "ab"
        if not resume_download:
            # Pretend that the .part file is empty and needs to be overwritten.
            target_part_size = 0
            file_open_flags = "wb"

        try:
            with open(target_part_path, file_open_flags) as fh:
                for chunk in resp.iter_content(2**14):
                    try:
                        fh.write(chunk)
                    except OSError as e:
                        message = (
                            "Failed to write to %(target_path)s\n  errno: %(errno)d"
                        )
                        # TODO: make this CondaIOError
                        raise CondaError(
                            message, target_path=target_part_path, errno=e.errno
                        )

                    target_part_size += len(chunk)

                    if (
                        total_target_size is not None
                        and target_part_size <= total_target_size
                    ):
                        if progress_update_callback:
                            progress_update_callback(
                                target_part_size / total_target_size
                            )

            if total_target_size and total_target_size != target_part_size:
                # TODO: needs to be a more-specific error type
                message = dals(
                    """
                Downloaded bytes did not match Content-Length
                  url: %(url)s
                  target_path: %(target_path)s
                  total Content-Length: %(content_length)d
                  total downloaded bytes: %(downloaded_bytes)d
                """
                )
                original_target_part_size = total_target_size - content_length
                raise CondaError(
                    message,
                    url=url,
                    target_path=target_part_path,
                    content_length=content_length,
                    downloaded_bytes=target_part_size - original_target_part_size,
                )

        except OSError as e:
            if e.errno == 104:
                # Connection reset by peer
                log.debug("%s, trying again" % e)
            raise

        if size is not None:
            actual_size = total_target_size
            if actual_size != size:
                log.debug(
                    "size mismatch for download: %s (%s != %s)", url, actual_size, size
                )
                raise ChecksumMismatchError(
                    url, target_part_path, "size", size, actual_size
                )
        content_length = int(resp.headers.get("Content-Length", 0))

        # prefer sha256 over md5 when both are available
        checksum_builder = checksum_type = checksum = None
        if sha256:
            checksum_builder = hashlib.new("sha256")
            checksum_type = "sha256"
            checksum = sha256
        elif md5:
            checksum_builder = hashlib.new("md5") if md5 else None
            checksum_type = "md5"
            checksum = md5

        if checksum_builder:
            with open(target_part_path, "rb") as fh:
                chunk = fh.read(2**14)
                while len(chunk):
                    checksum_builder.update(chunk)
                    chunk = fh.read(2**14)

        size_builder = 0
        try:
            with open(target_full_path, "wb") as fh:
                streamed_bytes = 0
                for chunk in resp.iter_content(2**14):
                    # chunk could be the decompressed form of the real data
                    # but we want the exact number of bytes read till now
                    streamed_bytes = resp.raw.tell()
                    try:
                        fh.write(chunk)
                    except OSError as e:
                        message = (
                            "Failed to write to %(target_path)s\n  errno: %(errno)d"
                        )
                        # TODO: make this CondaIOError
                        raise CondaError(
                            message, target_path=target_full_path, errno=e.errno
                        )

                    checksum_builder and checksum_builder.update(chunk)
                    size_builder += len(chunk)

                    if content_length and 0 <= streamed_bytes <= content_length:
                        if progress_update_callback:
                            progress_update_callback(streamed_bytes / content_length)

            if content_length and streamed_bytes != content_length:
                # TODO: needs to be a more-specific error type
                message = dals(
                    """
                Downloaded bytes did not match Content-Length
                  url: %(url)s
                  target_path: %(target_path)s
                  Content-Length: %(content_length)d
                  downloaded bytes: %(downloaded_bytes)d
                """
                )
                raise CondaError(
                    message,
                    url=url,
                    target_path=target_full_path,
                    content_length=content_length,
                    downloaded_bytes=streamed_bytes,
                )

        except OSError as e:
            if e.errno == 104:
                # Connection reset by peer
                log.debug("%s, trying again" % e)
            raise

        if checksum:
            actual_checksum = checksum_builder.hexdigest()
            if actual_checksum != checksum:
                log.debug(
                    "%s mismatch for download: %s (%s != %s)",
                    checksum_type,
                    url,
                    actual_checksum,
                    checksum,
                )
                raise ChecksumMismatchError(
                    url, target_part_path, checksum_type, checksum, actual_checksum
                )

        # Finally, rename the .part file to complete the successful download.
        rename(target_part_path, target_full_path)
        if size is not None:
            actual_size = size_builder
            if actual_size != size:
                log.debug(
                    "size mismatch for download: %s (%s != %s)", url, actual_size, size
                )
                raise ChecksumMismatchError(
                    url, target_full_path, "size", size, actual_size
                )

    except RequestsProxyError:
        raise ProxyError()  # see #3962

    except InvalidSchema as e:
        if "SOCKS" in str(e):
            message = dals(
                """
                Requests has identified that your current working environment is configured
                to use a SOCKS proxy, but pysocks is not installed.  To proceed, remove your
                proxy configuration, run `conda install pysocks`, and then you can re-enable
                your proxy configuration.
                """
            )
            raise CondaDependencyError(message)
        else:
            raise

    except SSLError as e:
        # SSLError: either an invalid certificate or OpenSSL is unavailable
        try:
            import ssl  # noqa: F401
        except ImportError:
            raise CondaSSLError(
                dals(
                    f"""
                    OpenSSL appears to be unavailable on this machine. OpenSSL is required to
                    download and install packages.

                    Exception: {e}
                    """
                )
            )
        else:
            raise CondaSSLError(
                dals(
                    f"""
                    Encountered an SSL error. Most likely a certificate verification issue.

                    Exception: {e}
                    """
                )
            )

    except (ConnectionError, HTTPError) as e:
        help_message = dals(
            """
        An HTTP error occurred when trying to retrieve this URL.
        HTTP errors are often intermittent, and a simple retry will get you on your way.
        """
        )
        raise CondaHTTPError(
            help_message,
            url,
            getattr(e.response, "status_code", None),
            getattr(e.response, "reason", None),
            getattr(e.response, "elapsed", None),
            e.response,
            caused_by=e,
        )


def download_text(url):
    if not context.ssl_verify:
        disable_ssl_verify_warning()
    try:
        timeout = context.remote_connect_timeout_secs, context.remote_read_timeout_secs
        session = CondaSession()
        response = session.get(
            url, stream=True, proxies=session.proxies, timeout=timeout
        )
        if log.isEnabledFor(DEBUG):
            log.debug(stringify(response, content_max_len=256))
        response.raise_for_status()
    except RequestsProxyError:
        raise ProxyError()  # see #3962
    except InvalidSchema as e:
        if "SOCKS" in str(e):
            message = dals(
                """
                Requests has identified that your current working environment is configured
                to use a SOCKS proxy, but pysocks is not installed.  To proceed, remove your
                proxy configuration, run `conda install pysocks`, and then you can re-enable
                your proxy configuration.
                """
            )
            raise CondaDependencyError(message)
        else:
            raise
    except (ConnectionError, HTTPError, SSLError) as e:
        status_code = getattr(e.response, "status_code", None)
        if status_code == 404:
            help_message = dals(
                """
            An HTTP error occurred when trying to retrieve this URL.
            The URL does not exist.
            """
            )
        else:
            help_message = dals(
                """
            An HTTP error occurred when trying to retrieve this URL.
            HTTP errors are often intermittent, and a simple retry will get you on your way.
            """
            )
        raise CondaHTTPError(
            help_message,
            url,
            status_code,
            getattr(e.response, "reason", None),
            getattr(e.response, "elapsed", None),
            e.response,
            caused_by=e,
        )
    return response.text


class TmpDownload:
    """Context manager to handle downloads to a tempfile."""

    def __init__(self, url, verbose=True):
        self.url = url
        self.verbose = verbose

    def __enter__(self):
        if "://" not in self.url:
            # if we provide the file itself, no tmp dir is created
            self.tmp_dir = None
            return self.url
        else:
            self.tmp_dir = tempfile.mkdtemp()
            dst = join(self.tmp_dir, basename(self.url))
            download(self.url, dst)
            return dst

    def __exit__(self, exc_type, exc_value, traceback):
        if self.tmp_dir:
            rm_rf(self.tmp_dir)
