"""Passive FTP for the same private accounts and files as the web editor."""

from __future__ import annotations

import contextlib
import errno
import ipaddress
import logging
import os
from pathlib import Path
import stat
import tempfile

from pyftpdlib.authorizers import AuthenticationFailed, DummyAuthorizer
from pyftpdlib.filesystems import AbstractedFS
from pyftpdlib.handlers import DTPHandler, FTPHandler
from pyftpdlib.servers import FTPServer

from store import Store, StoreError, UserBusy


def filesystem_error(error: StoreError) -> OSError:
    code = errno.EBUSY if isinstance(error, UserBusy) else errno.EACCES
    return OSError(code, str(error))


class StoreAuthorizer(DummyAuthorizer):

    permissions = "elrdfmw"

    def __init__(self, store: Store):
        super().__init__()
        self.store = store

    def validate_authentication(self, username, password, handler):
        if username == "anonymous" or not self.store.authenticate(username, password):
            raise AuthenticationFailed("Authentication failed. Anonymous access is disabled.")

    def has_user(self, username):
        try:
            self.store.user_home(username)
            return username != "anonymous"
        except StoreError:
            return False

    def get_home_dir(self, username):
        try:
            return str(self.store.user_home(username))
        except StoreError as error:
            raise AuthenticationFailed("Authentication failed.") from error

    def has_perm(self, username, perm, path=None):
        return perm in self.permissions

    def get_perms(self, username):
        return self.permissions

    def get_msg_login(self, username):
        return "Logged in. Use passive mode (PASV or EPSV)."

    def get_msg_quit(self, username):
        return "Goodbye."


class AtomicUpload:

    def __init__(self, filesystem, destination: Path):
        self.fs = filesystem
        self.name = str(destination)
        self.destination = destination
        self.closed = False
        self.size = 0
        self.temporary = None
        self.handle = None
        self.lock = self.fs.mutation_lock()
        self.lock.__enter__()
        try:
            if destination.exists() and not destination.is_file():
                raise OSError(errno.EISDIR, "Destination is not a regular file.")
            self.fs.store.check_publish(self.fs.username, destination, 0)
            descriptor, name = tempfile.mkstemp(prefix=".upload-", dir=self.fs.root)
            self.temporary = Path(name)
            self.handle = os.fdopen(descriptor, "wb")
            self.fs.active_uploads.add(self.fs.username)
        except Exception:
            self.lock.__exit__(None, None, None)
            self.closed = True
            raise

    def write(self, chunk):
        # Check the projected published size before writing, including the old
        # destination's replacement credit. The lock spans the whole transfer.
        try:
            self.fs.store.check_publish(self.fs.username, self.destination, self.size + len(chunk))
        except StoreError as error:
            raise OSError(errno.ENOSPC, str(error)) from error
        written = self.handle.write(chunk)
        self.size += written
        return written

    def publish(self):
        try:
            self.handle.flush()
            os.fsync(self.handle.fileno())
            self.handle.close()
            self.fs.store.publish_temp(
                self.fs.username, self.fs.relative(self.destination), self.temporary
            )
        except StoreError as error:
            raise filesystem_error(error) from error
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            if self.handle:
                self.handle.close()
            if self.temporary:
                with contextlib.suppress(FileNotFoundError):
                    self.temporary.unlink()
        finally:
            self.fs.active_uploads.discard(self.fs.username)
            self.lock.__exit__(None, None, None)


class SiteFilesystem(AbstractedFS):
    """Hide private working files and reject symlinks at every path component."""

    def __init__(self, root, cmd_channel):
        super().__init__(root, cmd_channel)
        self.store = cmd_channel.authorizer.store
        self.username = self.store.validate_username(cmd_channel.username)
        self.active_uploads = cmd_channel.active_uploads

    def relative(self, path):
        try:
            return Path(path).relative_to(self.root).as_posix()
        except ValueError as error:
            raise OSError(errno.EACCES, "Path is outside this site.") from error

    def checked(self, path):
        relative = self.relative(path)
        if relative == ".":
            return Path(self.root)
        try:
            return self.store.allowed_path(self.username, relative)
        except StoreError as error:
            raise filesystem_error(error) from error

    def validpath(self, path):
        try:
            self.checked(path)
            return True
        except OSError:
            return False

    @contextlib.contextmanager
    def mutation_lock(self):
        if self.username in self.active_uploads:
            raise OSError(errno.EBUSY, "Site files are busy with another upload.")
        try:
            with self.store.user_lock(self.username, blocking=False):
                yield
        except StoreError as error:
            raise filesystem_error(error) from error

    def open(self, filename, mode):
        path = self.checked(filename)
        if mode == "wb":
            try:
                return AtomicUpload(self, path)
            except StoreError as error:
                raise filesystem_error(error) from error
        if mode != "rb":
            raise OSError(errno.EACCES, "Only complete uploads and downloads are supported.")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            raise OSError(errno.EACCES, "Only regular files may be downloaded.")
        return os.fdopen(descriptor, "rb")

    def listdir(self, path):
        directory = self.checked(path)
        return [name for name in os.listdir(directory)
                if not name.startswith(".") and self.validpath(directory / name)]

    def listdirinfo(self, path):
        return self.listdir(path)

    def chdir(self, path):
        return super().chdir(str(self.checked(path)))

    def mkdir(self, path):
        with self.mutation_lock():
            return self.store.mkdir(self.username, self.relative(self.checked(path)))

    def rmdir(self, path):
        with self.mutation_lock():
            path = self.checked(path)
            if path == Path(self.root):
                raise OSError(errno.EACCES, "Cannot remove the site root.")
            return os.rmdir(path)

    def remove(self, path):
        with self.mutation_lock():
            return os.remove(self.checked(path))

    def rename(self, source, destination):
        with self.mutation_lock():
            source, destination = self.checked(source), self.checked(destination)
            if source == Path(self.root) or destination == Path(self.root):
                raise OSError(errno.EACCES, "Cannot rename the site root.")
            return os.rename(source, destination)


class AtomicDTPHandler(DTPHandler):
    def close(self):
        upload = getattr(self, "file_obj", None)
        if not self._closed and isinstance(upload, AtomicUpload) and not upload.closed:
            if self.transfer_finished:
                try:
                    upload.publish()
                except OSError:
                    self.transfer_finished = False
                    self._resp = ("451 Upload could not be published.", logging.getLogger(__name__).warning)
            else:
                upload.close()
        super().close()


class BoundedPassiveDTP(FTPHandler.passive_dtp):
    """Do not use pyftpdlib's random-port fallback when the range is occupied."""

    def __init__(self, cmd_channel, extmode=False):
        try:
            super().__init__(cmd_channel, extmode)
        except OSError:
            self.close()
            cmd_channel.respond("425 No passive port is available. Please try again.")

    def bind(self, address):
        if address[1] not in self.cmd_channel.passive_ports:
            raise OSError(errno.EADDRINUSE, "Passive port range is busy.")
        return super().bind(address)

    def listen(self, backlog):
        if self.socket.getsockname()[1] not in self.cmd_channel.passive_ports:
            raise OSError(errno.EADDRINUSE, "No passive port was bound.")
        return super().listen(backlog)


class SiteFTPHandler(FTPHandler):
    abstracted_fs = SiteFilesystem
    dtp_handler = AtomicDTPHandler
    passive_dtp = BoundedPassiveDTP
    permit_foreign_addresses = False
    permit_privileged_ports = False
    timeout = 120
    banner = "Dialback Zero private site FTP. Passive mode required."

    def pre_process_command(self, line, cmd, arg):
        queued = self._in_dtp_queue and isinstance(self._in_dtp_queue[0], AtomicUpload)
        receiving = self.data_channel and isinstance(self.data_channel.file_obj, AtomicUpload)
        if (queued or receiving) and cmd not in ("ABOR", "QUIT", "NOOP"):
            # Do not let another command replace a temporary file handle or
            # discard a queued transfer while its account lock is held.
            self.respond("450 An upload is in progress. Finish it or send ABOR.")
            return
        return super().pre_process_command(line, cmd, arg)

    def ftp_ABOR(self, line):
        if self._in_dtp_queue and isinstance(self._in_dtp_queue[0], AtomicUpload):
            self._in_dtp_queue[0].close()
            self._in_dtp_queue = None
        return super().ftp_ABOR(line)

    def ftp_USER(self, line):
        return super().ftp_USER(line.lower())

    def ftp_PORT(self, line):
        self.respond("502 Active FTP is disabled. Use PASV or EPSV.")

    def ftp_EPRT(self, line):
        self.respond("502 Active FTP is disabled. Use PASV or EPSV.")

    def ftp_REST(self, line):
        self.respond("502 Resume is disabled. Upload or download the complete file.")

    def ftp_STOU(self, line):
        self.respond("502 Use STOR with a filename.")


def create_server(store: Store, *, bind="10.77.0.1", port=21,
                  passive_address="10.77.0.1", passive_ports=range(30000, 30010),
                  ioloop=None):
    ipaddress.IPv4Address(bind)
    ipaddress.IPv4Address(passive_address)
    ports = list(passive_ports)
    if not ports or any(not 1024 <= value <= 65535 for value in ports):
        raise ValueError("Passive ports must be an explicit unprivileged range.")

    class Handler(SiteFTPHandler):
        authorizer = StoreAuthorizer(store)
        active_uploads = set()

    Handler.masquerade_address = passive_address
    Handler.passive_ports = ports
    server = FTPServer((bind, port), Handler, ioloop=ioloop)
    server.max_cons = 32
    server.max_cons_per_ip = min(8, len(ports))
    return server


def main():
    store = Store(os.environ.get("DBZ_DATA_DIR", "/data"), os.environ.get("DBZ_DNS_DIR", "/dns"))
    server = create_server(
        store, bind=os.environ.get("DBZ_FTP_BIND", "10.77.0.1"),
        port=int(os.environ.get("DBZ_FTP_PORT", "21")),
        passive_address=os.environ.get("DBZ_FTP_PASSIVE_ADDRESS", "10.77.0.1"),
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
