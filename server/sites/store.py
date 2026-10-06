from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile
import threading
from typing import BinaryIO, Iterator

from werkzeug.security import check_password_hash, generate_password_hash


MAX_ACCOUNT_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FILES = 256
MAX_DIRECTORIES = 64
MAX_PATH_DEPTH = 16

USERNAME_RE = re.compile(r"[A-Za-z0-9_-]{3,24}\Z", re.ASCII)
DOMAIN_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z", re.ASCII)


class StoreError(Exception):
    """Base class for errors suitable for a form-level message."""


class ValidationError(StoreError):
    pass


class DuplicateUser(StoreError):
    pass


class UnknownUser(StoreError):
    pass


class DomainUnavailable(StoreError):
    pass


class UnsafePath(StoreError):
    pass


class QuotaExceeded(StoreError):
    pass


class UserBusy(StoreError):
    pass


_local_locks_guard = threading.Lock()
_local_locks: dict[str, threading.RLock] = {}
_held_locks = threading.local()


def _local_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _local_locks_guard:
        return _local_locks.setdefault(key, threading.RLock())


class Store:
    """Thread- and process-safe storage used by both HTTP and FTP frontends."""

    def __init__(self, data_dir: str | os.PathLike[str], dns_dir: str | os.PathLike[str]):
        self.data_dir = Path(data_dir).resolve()
        self.dns_dir = Path(dns_dir).resolve()
        self.db_path = self.data_dir / "accounts.sqlite"
        self.sites_dir = self.data_dir / "sites"
        self.locks_dir = self.data_dir / ".locks"
        self.dns_path = self.dns_dir / "registered-hosts"

        for directory in (self.data_dir, self.sites_dir, self.locks_dir, self.dns_dir):
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink():
                raise UnsafePath(f"storage directory is a symlink: {directory}")
        os.chmod(self.data_dir, 0o700)
        os.chmod(self.locks_dir, 0o700)
        self._initialize_database()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextlib.contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self._db() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS sites (
                    user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                    domain TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        os.chmod(self.db_path, 0o600)

    @staticmethod
    def validate_username(username: str) -> str:
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise ValidationError("Username must be 3-24 ASCII letters, numbers, _ or -.")
        username = username.lower()
        if username == "anonymous":
            raise ValidationError("That username is reserved.")
        return username

    @staticmethod
    def validate_password(password: str) -> None:
        if not isinstance(password, str) or len(password) < 8:
            raise ValidationError("Password must contain at least 8 characters.")

    @staticmethod
    def validate_domain(domain: str) -> str:
        if not isinstance(domain, str):
            raise ValidationError("Enter a valid ASCII domain name.")
        domain = domain.strip().lower()
        # The automatically-created "www." alias must also fit DNS's limit.
        if len(domain) > 249 or not domain or not domain.isascii():
            raise ValidationError("Enter a valid ASCII domain name.")
        if domain.endswith(".") or domain.startswith("www."):
            raise ValidationError("Enter the domain without a trailing dot or www.")
        try:
            ipaddress.ip_address(domain.strip("[]"))
        except ValueError:
            pass
        else:
            raise ValidationError("IP addresses cannot be claimed as domains.")
        labels = domain.split(".")
        if len(labels) < 2 or any(not DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
            raise ValidationError("Enter a valid ASCII domain name.")
        if len(labels[-1]) < 2 or not labels[-1].isalpha():
            raise ValidationError("The final domain label must contain at least two letters.")
        reserved = ("retro.net", "home.arpa", "in-addr.arpa", "ip6.arpa", "internal")
        if any(
            domain == name or domain.endswith("." + name) or name.endswith("." + domain)
            for name in reserved
        ):
            raise DomainUnavailable("That domain is reserved.")
        return domain

    def register(self, username: str, password: str) -> str:
        username = self.validate_username(username)
        self.validate_password(password)
        password_hash = generate_password_hash(password)
        try:
            with self._db() as connection:
                connection.execute(
                    "INSERT INTO users(username, password_hash) VALUES (?, ?)",
                    (username, password_hash),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateUser("That username is already registered.") from error
        home = self.sites_dir / username
        home.mkdir(mode=0o755, exist_ok=True)
        if home.is_symlink():
            raise UnsafePath("User home cannot be a symlink.")
        os.chmod(home, 0o755)
        return username

    def authenticate(self, username: str, password: str) -> bool:
        try:
            username = self.validate_username(username)
        except ValidationError:
            return False
        with self._db() as connection:
            row = connection.execute(
                "SELECT password_hash FROM users WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone()
        return bool(row and isinstance(password, str) and check_password_hash(row[0], password))

    def _canonical_user(self, username: str) -> str:
        username = self.validate_username(username)
        with self._db() as connection:
            row = connection.execute(
                "SELECT username FROM users WHERE username = ? COLLATE NOCASE", (username,)
            ).fetchone()
        if row is None:
            raise UnknownUser("Unknown user.")
        return str(row[0])

    def user_home(self, username: str) -> Path:
        username = self._canonical_user(username)
        home = self.sites_dir / username
        home.mkdir(mode=0o755, exist_ok=True)
        if home.is_symlink():
            raise UnsafePath("User home cannot be a symlink.")
        return home

    def user_domain(self, username: str) -> str | None:
        username = self._canonical_user(username)
        with self._db() as connection:
            row = connection.execute(
                """SELECT sites.domain FROM sites JOIN users ON users.id = sites.user_id
                   WHERE users.username = ? COLLATE NOCASE""",
                (username,),
            ).fetchone()
        return str(row[0]) if row else None

    @staticmethod
    def _domains_overlap(first: str, second: str) -> bool:
        return first == second or first.endswith("." + second) or second.endswith("." + first)

    def claim_domain(self, username: str, domain: str) -> str:
        username = self._canonical_user(username)
        domain = self.validate_domain(domain)
        connection = self._connect()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                user = connection.execute(
                    "SELECT id FROM users WHERE username = ? COLLATE NOCASE", (username,)
                ).fetchone()
                if connection.execute(
                    "SELECT 1 FROM sites WHERE user_id = ?", (user[0],)
                ).fetchone():
                    raise DomainUnavailable("This account already has a domain.")
                existing = [str(row[0]) for row in connection.execute("SELECT domain FROM sites")]
                requested_names = (domain, "www." + domain)
                if any(
                    self._domains_overlap(requested, claimed)
                    or self._domains_overlap(requested, "www." + claimed)
                    for requested in requested_names
                    for claimed in existing
                ):
                    raise DomainUnavailable("That domain conflicts with an existing site.")
                connection.execute(
                    "INSERT INTO sites(user_id, domain) VALUES (?, ?)", (user[0], domain)
                )
                connection.commit()
        finally:
            connection.close()
        self.reconcile_dns()

        if not (self.user_home(username) / "index.html").exists():
            page = (
                '<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">\n'
                "<html><head><title>Welcome</title></head>\n"
                f"<body><h1>{domain}</h1><p>This site is ready.</p></body></html>\n"
            )
            self.write_file(username, "index.html", page.encode("ascii"))
        return domain

    def list_sites(self) -> list[dict[str, str]]:
        with self._db() as connection:
            rows = connection.execute(
                """SELECT users.username, sites.domain FROM sites
                   JOIN users ON users.id = sites.user_id ORDER BY sites.domain COLLATE NOCASE"""
            ).fetchall()
        return [{"username": str(row[0]), "domain": str(row[1])} for row in rows]

    def resolve_domain(self, domain: str) -> str | None:
        if not isinstance(domain, str):
            return None
        domain = domain.lower()
        lookup = domain[4:] if domain.startswith("www.") else domain
        with self._db() as connection:
            row = connection.execute(
                """SELECT users.username FROM sites JOIN users ON users.id = sites.user_id
                   WHERE sites.domain = ? COLLATE NOCASE""",
                (lookup,),
            ).fetchone()
        return str(row[0]) if row else None

    @contextlib.contextmanager
    def _dns_lock(self) -> Iterator[None]:
        lock_path = self.dns_dir / ".registered-hosts.lock"
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _write_dns_records(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT domain FROM sites ORDER BY domain COLLATE NOCASE")
        address = os.environ.get("DBZ_SITES_ADDRESS", "10.77.0.1")
        content = "".join(f"{address} {row[0]} www.{row[0]}\n" for row in rows)
        fd, temporary = tempfile.mkstemp(prefix=".registered-hosts-", dir=self.dns_dir)
        try:
            os.fchmod(fd, 0o644)
            with os.fdopen(fd, "w", encoding="ascii", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.dns_path)
            os.chmod(self.dns_path, 0o644)
            self._fsync_directory(self.dns_dir)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary)
            raise

    def reconcile_dns(self) -> None:
        """Publish DNS records from committed site claims.

        Web startup calls this to recover cleanly after a process or host stops
        between the SQLite commit and the atomic hosts-file replacement. FTP
        startup constructs Store without changing DNS state.
        """
        with self._dns_lock():
            with self._db() as connection:
                self._write_dns_records(connection)

    @contextlib.contextmanager
    def user_lock(self, username: str, blocking: bool = True) -> Iterator[None]:
        username = self.validate_username(username)
        path = self.locks_dir / f"{username}.lock"
        local = _local_lock(path)
        if not local.acquire(blocking=blocking):
            raise UserBusy("Site files are busy.")
        held = getattr(_held_locks, "items", None)
        if held is None:
            held = _held_locks.items = {}
        key = str(path)
        if key in held:
            held[key][1] += 1
            try:
                yield
            finally:
                held[key][1] -= 1
                local.release()
            return
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
            try:
                fcntl.flock(fd, operation)
            except BlockingIOError as error:
                raise UserBusy("Site files are busy.") from error
            held[key] = [fd, 1]
            yield
        finally:
            if key in held:
                held.pop(key, None)
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            local.release()

    def allowed_path(self, username: str, relative: str, *, must_exist: bool = False) -> Path:
        home = self.user_home(username)
        if not isinstance(relative, str) or not relative or "\x00" in relative or "\\" in relative:
            raise UnsafePath("Invalid file path.")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or any(part in ("", ".", "..") or part.startswith(".") for part in pure.parts):
            raise UnsafePath("Hidden paths and path traversal are not allowed.")
        if len(pure.parts) > MAX_PATH_DEPTH or len(relative.encode("utf-8")) > 1024:
            raise UnsafePath("File path is too long or too deeply nested.")
        if any(
            len(part.encode("utf-8")) > 255 or any(ord(character) < 32 for character in part)
            for part in pure.parts
        ):
            raise UnsafePath("File path contains an invalid name.")
        target = home.joinpath(*pure.parts)
        current = home
        if current.is_symlink():
            raise UnsafePath("Symlinks are not allowed.")
        for part in pure.parts:
            current = current / part
            if current.is_symlink():
                raise UnsafePath("Symlinks are not allowed.")
        if must_exist and not target.is_file():
            raise FileNotFoundError(relative)
        return target

    safe_user_path = allowed_path

    def _usage_details_unlocked(self, username: str) -> tuple[int, int, int]:
        home = self.user_home(username)
        count = 0
        size = 0
        directory_count = 0
        for root, directories, files in os.walk(home, followlinks=False):
            root_path = Path(root)
            if root_path.is_symlink():
                raise UnsafePath("Symlinks are not allowed.")
            safe_directories = []
            for name in directories:
                if name.startswith("."):
                    continue
                directory = root_path / name
                if directory.is_symlink() or not directory.is_dir():
                    raise UnsafePath("Symlinks and special files are not allowed.")
                directory_count += 1
                safe_directories.append(name)
            directories[:] = safe_directories
            for name in files:
                if name.startswith("."):
                    continue
                path = root_path / name
                if path.is_symlink() or not path.is_file():
                    raise UnsafePath("Symlinks and special files are not allowed.")
                count += 1
                size += path.stat().st_size
        return count, size, directory_count

    def _usage_unlocked(self, username: str) -> tuple[int, int]:
        count, size, _directories = self._usage_details_unlocked(username)
        return count, size

    def usage(self, username: str) -> tuple[int, int]:
        with self.user_lock(username):
            return self._usage_unlocked(username)

    def list_files(self, username: str) -> list[dict[str, int | str]]:
        home = self.user_home(username)
        with self.user_lock(username):
            result = []
            for root, directories, files in os.walk(home, followlinks=False):
                directories[:] = sorted(name for name in directories if not name.startswith("."))
                for name in sorted(files):
                    if name.startswith("."):
                        continue
                    path = Path(root) / name
                    relative = path.relative_to(home).as_posix()
                    safe = self.allowed_path(username, relative, must_exist=True)
                    result.append({"path": relative, "size": safe.stat().st_size})
            return result

    def read_file(self, username: str, relative: str) -> bytes:
        path = self.allowed_path(username, relative, must_exist=True)
        with self.user_lock(username):
            return path.read_bytes()

    def check_publish(self, username: str, destination: Path, temporary_size: int) -> None:
        if temporary_size > MAX_FILE_BYTES:
            raise QuotaExceeded("Files may be at most 2 MiB.")
        count, used, directories = self._usage_details_unlocked(username)
        old_size = destination.stat().st_size if destination.exists() else 0
        new_count = count if destination.exists() else count + 1
        if new_count > MAX_FILES:
            raise QuotaExceeded("An account may contain at most 256 files.")
        if used - old_size + temporary_size > MAX_ACCOUNT_BYTES:
            raise QuotaExceeded("This account has reached its 10 MiB quota.")
        home = self.user_home(username)
        parent = destination.parent
        missing_directories = 0
        while parent != home and not parent.exists():
            missing_directories += 1
            parent = parent.parent
        if parent.is_symlink() or not parent.is_dir():
            raise UnsafePath("Destination parent is not a regular directory.")
        if directories + missing_directories > MAX_DIRECTORIES:
            raise QuotaExceeded("An account may contain at most 64 directories.")

    def mkdir(self, username: str, relative: str) -> Path:
        """Create one safe FTP-visible directory within the directory quota."""
        with self.user_lock(username):
            target = self.allowed_path(username, relative)
            if target.exists():
                raise FileExistsError(relative)
            if not target.parent.is_dir() or target.parent.is_symlink():
                raise FileNotFoundError(target.parent.relative_to(self.user_home(username)).as_posix())
            _files, _bytes, directories = self._usage_details_unlocked(username)
            if directories >= MAX_DIRECTORIES:
                raise QuotaExceeded("An account may contain at most 64 directories.")
            target.mkdir(mode=0o755)
            self._fsync_directory(target.parent)
            return target

    def publish_temp(self, username: str, relative: str, temporary: str | os.PathLike[str]) -> Path:
        with self.user_lock(username):
            return self._publish_temp_unlocked(username, relative, Path(temporary))

    def _publish_temp_unlocked(self, username: str, relative: str, temporary: Path) -> Path:
        home = self.user_home(username)
        destination = self.allowed_path(username, relative)
        if temporary.parent != home or not temporary.name.startswith(".upload-"):
            raise UnsafePath("Temporary upload is outside the user home.")
        if temporary.is_symlink() or not temporary.is_file():
            raise UnsafePath("Temporary upload is not a regular file.")
        size = temporary.stat().st_size
        self.check_publish(username, destination, size)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        current = home
        for part in destination.relative_to(home).parts[:-1]:
            current = current / part
            if current.is_symlink():
                raise UnsafePath("Symlinks are not allowed.")
            os.chmod(current, 0o755)
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
        os.chmod(destination, 0o644)
        self._fsync_directory(destination.parent)
        return destination

    def write_file(self, username: str, relative: str, data: bytes) -> Path:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if len(data) > MAX_FILE_BYTES:
            raise QuotaExceeded("Files may be at most 2 MiB.")
        home = self.user_home(username)
        with self.user_lock(username):
            fd, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=home)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                return self._publish_temp_unlocked(username, relative, temporary)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()

    def write_stream(self, username: str, relative: str, source: BinaryIO) -> Path:
        home = self.user_home(username)
        with self.user_lock(username):
            fd, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=home)
            temporary = Path(temporary_name)
            total = 0
            try:
                with os.fdopen(fd, "wb") as handle:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_FILE_BYTES:
                            raise QuotaExceeded("Files may be at most 2 MiB.")
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                return self._publish_temp_unlocked(username, relative, temporary)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    temporary.unlink()

    def delete_file(self, username: str, relative: str) -> None:
        with self.user_lock(username):
            path = self.allowed_path(username, relative, must_exist=True)
            home = self.user_home(username)
            path.unlink()
            parent = path.parent
            while parent != home:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def open_public(self, domain: str, relative: str) -> tuple[Path, str, int]:
        username = self.resolve_domain(domain)
        if username is None:
            raise FileNotFoundError(domain)
        relative = relative or "index.html"
        if relative.endswith("/"):
            relative += "index.html"
        path = self.allowed_path(username, relative)
        if path.is_dir():
            relative = relative.rstrip("/") + "/index.html"
            path = self.allowed_path(username, relative)
        if not path.is_file():
            raise FileNotFoundError(relative)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path, mime, path.stat().st_size

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
