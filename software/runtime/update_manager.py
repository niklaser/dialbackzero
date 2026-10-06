#!/usr/bin/env python3
"""GitHub release updates; only the versioned application tree is switched."""

from contextlib import contextmanager
import fcntl
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


APP = Path("/opt/dialback-zero")
STATE = Path("/var/lib/dialback-zero/updates")
CONFIG = Path("/etc/dialback-zero/config.json")
PENDING_CONFIG = Path("/var/lib/dialback-zero/pending-config.json")
REPOSITORY = "niklaser/dialbackzero"
API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
SERVICE = "dialback-zero-update.service"
TARGET = "dialback-zero.target"
BUSY = {"checking", "downloading", "installing", "verifying"}
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 128 * 1024 * 1024
MAX_JSON = 1024 * 1024
MAX_FILES = 256
STABLE_VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
UNITS = (
    "dialback-zero-modem.service", "dialback-zero-config.service",
    "dialback-zero-leds.service", "dialback-zero-forwarding.service",
    "dialback-zero-ppp-internet.service", "dialback-zero-ppp-hub.service",
)


class UpdateError(Exception):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("Duplicate field in update metadata.")
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        raise UpdateError("Invalid update metadata.") from None


def _read(path, default=None):
    if not path.exists():
        return {} if default is None else default
    if path.is_symlink() or path.stat().st_size > MAX_JSON:
        raise UpdateError("Invalid local update state.")
    result = _json(path.read_bytes())
    if not isinstance(result, dict):
        raise UpdateError("Invalid local update state.")
    return result


def _sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write((json.dumps(data, sort_keys=True) + "\n").encode())
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
            _sync_dir(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def _locked(state, name="lock"):
    if state.is_symlink():
        raise UpdateError("Invalid local update directory.")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    with (state / name).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise UpdateError("An update operation is already running.") from None
        yield


def _status(state, phase, message, **values):
    data = _read(state / "status.json")
    data.update(phase=phase, message=message, **values)
    _write(state / "status.json", data)


def _current(app):
    link = app / "current"
    if not link.is_symlink():
        raise UpdateError("Install update support on this device first.")
    name = os.readlink(link)
    parts = PurePosixPath(name).parts
    if (len(parts) != 2 or parts[0] != "releases" or
            not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", parts[1])):
        raise UpdateError("Invalid installed application path.")
    path = app / name
    if path.is_symlink() or path.resolve().parent != (app / "releases").resolve():
        raise UpdateError("Invalid installed release directory.")
    metadata = _read(path / "release.json")
    if not metadata.get("version") or not metadata.get("platform"):
        raise UpdateError("The installed release has no update metadata.")
    return name, metadata


def get_status(app=None, state=None):
    app, state = app or APP, state or STATE
    data = _read(state / "status.json")
    try:
        _, installed = _current(app)
        version = installed["version"]
    except UpdateError:
        version = "Unknown (update support not installed)"
    return dict(data, installed_version=version,
                available_version=data.get("available_version"),
                phase=data.get("phase", "idle"),
                message=data.get("message", "Check for a new release."))


def _run(args, **kwargs):
    return subprocess.run(args, check=True, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=kwargs.pop("timeout", 90), **kwargs)


def _request_allowed(action):
    _current(APP)
    status = _read(STATE / "status.json")
    if status.get("phase") in BUSY or (STATE / "transaction.json").exists():
        raise UpdateError("An update operation is already running.")
    if action == "install":
        if not status.get("available_version") or not (STATE / "candidate.json").is_file():
            raise UpdateError("Check for updates first.")
        if PENDING_CONFIG.exists():
            raise UpdateError("Restart the device to apply staged settings before updating.")


def _queue(action):
    # Serialize dispatch separately: the old service's stop hook needs the state
    # lock and must finish before a fresh request becomes visible to it.
    with _locked(STATE, "dispatch.lock"):
        with _locked(STATE):
            _request_allowed(action)
        try:
            _run(["systemctl", "stop", SERVICE], timeout=5)
        except (OSError, subprocess.SubprocessError):
            raise UpdateError("The previous update task is still stopping. Try again shortly.") from None
        with _locked(STATE):
            _request_allowed(action)
            _write(STATE / "request.json", {"action": action})
            _status(STATE, "checking" if action == "check" else "downloading",
                    "Checking for updates." if action == "check" else "Downloading update.")
        try:
            _run(["systemctl", "start", "--no-block", SERVICE], timeout=5)
        except (OSError, subprocess.SubprocessError):
            with _locked(STATE):
                (STATE / "request.json").unlink(missing_ok=True)
                _status(STATE, "failed", "Could not start the update service.")
            raise UpdateError("Could not start the update service.") from None


def request_check():
    _queue("check")


def request_install():
    _queue("install")


def _https_url(url):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.port not in (None, 443)
            or parsed.username or parsed.password
            or parsed.hostname not in {"api.github.com", "github.com",
                                       "release-assets.githubusercontent.com",
                                       "objects.githubusercontent.com"}):
        raise UpdateError("Unexpected release download address.")
    return parsed


class _Redirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, destination=None, limit=MAX_JSON):
    _https_url(url)
    request = Request(url, headers={"User-Agent": "Dialback-Zero-Updater/1",
                                   "Accept": "application/vnd.github+json" if url == API else "application/octet-stream"})
    buffer = bytearray()
    with build_opener(_Redirects()).open(request, timeout=30) as response:
        _https_url(response.url)
        size = 0
        handle = destination.open("wb") if destination else None
        try:
            while True:
                block = response.read(64 * 1024)
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise UpdateError("Release download exceeds the size limit.")
                if handle:
                    handle.write(block)
                else:
                    buffer.extend(block)
            if handle:
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            if handle:
                handle.close()
    return bytes(buffer)


def _version(value):
    match = STABLE_VERSION.fullmatch(value) if isinstance(value, str) else None
    return tuple(map(int, match.groups())) if match else None


def discover(installed, fetch=download):
    release = _json(fetch(API))
    version = release.get("tag_name")
    if release.get("draft") or release.get("prerelease") or not _version(version):
        raise UpdateError("No stable update release is available.")
    current_version = _version(installed["version"])
    if current_version is not None and _version(version) <= current_version:
        return None
    filename = f"dialback-zero-{version}-armv6-update.tar.gz"
    assets = release.get("assets", [])
    matches = [a for a in assets if a.get("name") == filename]
    sums = [a for a in assets if a.get("name") == "SHA256SUMS"]
    if len(matches) != 1 or len(sums) != 1:
        raise UpdateError("The latest release does not contain an update package yet.")
    asset, checksums = matches[0], sums[0]
    prefix = f"https://github.com/{REPOSITORY}/releases/download/{version}/"
    if (asset.get("browser_download_url") != prefix + filename or
            checksums.get("browser_download_url") != prefix + "SHA256SUMS"):
        raise UpdateError("Unexpected release asset address.")
    size = asset.get("size")
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE:
        raise UpdateError("Invalid update package size.")
    lines = fetch(prefix + "SHA256SUMS").decode("ascii").splitlines()
    digests = []
    for line in lines:
        fields = line.split()
        if len(fields) == 2 and fields[1].lstrip("*") == filename:
            digests.append(fields[0])
    if len(digests) != 1 or not re.fullmatch(r"[0-9a-f]{64}", digests[0]):
        raise UpdateError("The release checksum is missing or ambiguous.")
    digest = asset.get("digest")
    if digest is not None and digest != "sha256:" + digests[0]:
        raise UpdateError("GitHub and release checksums do not agree.")
    return {"version": version, "url": prefix + filename, "size": size,
            "sha256": digests[0], "checked_at": time.time()}


def _extract(archive, destination, platform):
    from release import validate_manifest, validate_release
    with tarfile.open(archive, "r:gz") as bundle:
        members = []
        names = set()
        total = 0
        for member in bundle:
            parts = PurePosixPath(member.name).parts
            if (len(members) >= MAX_FILES or not member.isfile() or member.name in names
                    or not parts or member.name.startswith("/") or ".." in parts
                    or str(PurePosixPath(member.name)) != member.name):
                raise UpdateError("Unsafe entry in update package.")
            total += member.size
            if member.size < 0 or total > MAX_EXPANDED:
                raise UpdateError("Expanded update package exceeds the size limit.")
            members.append(member)
            names.add(member.name)
        if "release.json" not in names:
            raise UpdateError("Update package has no manifest.")
        manifest_member = next(m for m in members if m.name == "release.json")
        if manifest_member.size > MAX_JSON:
            raise UpdateError("Update manifest is too large.")
        metadata = _json(bundle.extractfile(manifest_member).read())
        try:
            validate_manifest(metadata, expected_platform=platform)
        except ValueError as error:
            raise UpdateError(str(error)) from None
        if names != {"release.json", *metadata["files"]}:
            raise UpdateError("Update contents do not match the manifest.")
        for member in members:
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with bundle.extractfile(member) as source, path.open("xb") as output:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
            executable = member.name.startswith("bin/") or member.name == "runtime/config-menu" or member.name.endswith(".py")
            path.chmod(0o755 if executable else 0o644)
    validate_release(destination, expected_platform=platform)
    for directory in sorted((p for p in destination.rglob("*") if p.is_dir()), reverse=True):
        _sync_dir(directory)
    _sync_dir(destination)
    return metadata


def _switch(app, target):
    # Targets originate only from validated release IDs or our root-owned journal.
    parts = PurePosixPath(target).parts
    if len(parts) != 2 or parts[0] != "releases" or not re.fullmatch(r"[A-Za-z0-9._-]{1,160}", parts[1]):
        raise UpdateError("Invalid application switch target.")
    path = app / target
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != (app / "releases").resolve():
        raise UpdateError("Application switch target is missing.")
    temporary = app / ".current-update"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, app / "current")
    _sync_dir(app)


def _prune(app, keep):
    """Keep current and last good release; never remove unrecognized directories."""
    from release import RELEASE_ID, read_manifest, release_id
    for directory in (app / "releases").iterdir():
        if directory.name in keep or directory.is_symlink() or not directory.is_dir():
            continue
        if not RELEASE_ID.fullmatch(directory.name):
            continue
        try:
            if release_id(read_manifest(directory / "release.json")) == directory.name:
                shutil.rmtree(directory)
        except (OSError, ValueError):
            # A cleanup failure must not turn a successful update into a rollback.
            continue


def preflight(directory, config=CONFIG):
    binary = directory / "bin/dialback-zero-modem"
    with binary.open("rb") as stream:
        data = stream.read(52)
    if (len(data) < 52 or data[:6] != b"\x7fELF\x01\x01"
            or int.from_bytes(data[18:20], "little") != 40
            or not int.from_bytes(data[36:40], "little") & 0x400):
        raise UpdateError("The update executable is not 32-bit ARM hard-float.")
    attributes = subprocess.run(["readelf", "-A", str(binary)], check=True,
                                capture_output=True, text=True, timeout=15).stdout
    if not re.search(r"Tag_CPU_arch: v6\s*$", attributes, re.MULTILINE):
        raise UpdateError("The update executable does not target ARMv6.")
    for path in (directory / "runtime").glob("*.py"):
        compile(path.read_bytes(), str(path), "exec")
    # This executes only a checksummed release from the fixed official repository.
    # Loading through the new validator is read-only; no migration runs on settings.
    script = ("import sys; sys.path.insert(0, sys.argv[1]); "
              "import update_manager, config_web, launcher, led_daemon, ppp_wrapper, forwarding; "
              "assert all(callable(getattr(update_manager, n)) for n in "
              "('worker', 'recover', 'finish', 'get_status', 'request_check', 'request_install')); "
              "from pathlib import Path; from dialback_config import load; "
              "load(Path(sys.argv[2]))")
    _run([sys.executable, "-B", "-c", script, str(directory / "runtime"), str(config)])


def _restart():
    _run(["systemctl", "restart", TARGET])


def healthy(timeout=60):
    from dialback_config import load
    config = load()
    units = [unit for unit in UNITS if unit != "dialback-zero-ppp-hub.service"
             or config["modem"]["numbers"]["777"]["enabled"]]
    deadline = time.monotonic() + timeout
    steady = 0
    while time.monotonic() < deadline:
        try:
            checks = [subprocess.run(["systemctl", "show", unit,
                                      "--property=ActiveState", "--property=NRestarts"],
                                     check=True, capture_output=True, text=True, timeout=5).stdout
                      for unit in units]
            active = all("ActiveState=active\n" in result and "NRestarts=0\n" in result for result in checks)
            connection = http.client.HTTPConnection("127.0.0.1", config["web"]["port"], timeout=3)
            try:
                connection.request("GET", "/")
                response = connection.getresponse()
                page = response.read(MAX_JSON)
                active = active and response.status == 200 and b"Dialback Zero settings" in page
            finally:
                connection.close()
            steady = steady + 1 if active else 0
            if steady >= 3:
                return True
        except (OSError, ValueError, subprocess.SubprocessError, http.client.HTTPException):
            steady = 0
        time.sleep(2)
    return False


def _rollback(app, state, restart, check, boot=False):
    journal = _read(state / "transaction.json")
    if not journal:
        return
    _switch(app, journal["previous"])
    if not boot:
        restart()
        if not check():
            raise UpdateError("Previous version restored, but services need attention. Restart the device.")
    (state / "transaction.json").unlink()
    _sync_dir(state)
    _status(state, "rolled_back", "Update did not complete. The previous version was restored.", available_version=None)


def install_candidate(app, state, candidate, fetch=download, restart=_restart, check=healthy, verify=preflight):
    from release import release_id, file_hash, validate_release
    previous, installed = _current(app)
    version = candidate.get("version")
    if not _version(version) or (_version(installed["version"]) is not None and _version(version) <= _version(installed["version"])):
        raise UpdateError("This update is not newer than the installed version.")
    expected_url = f"https://github.com/{REPOSITORY}/releases/download/{version}/dialback-zero-{version}-armv6-update.tar.gz"
    if candidate.get("url") != expected_url:
        raise UpdateError("Invalid cached release address.")
    if shutil.disk_usage(app).free < MAX_EXPANDED + MAX_ARCHIVE + 16 * 1024 * 1024:
        raise UpdateError("Not enough free space for an update and rollback.")
    releases = app / "releases"
    with tempfile.TemporaryDirectory(prefix=".update-", dir=releases) as temporary:
        scratch = Path(temporary)
        archive = scratch / "update.tar.gz"
        fetch(candidate["url"], destination=archive, limit=MAX_ARCHIVE)
        if archive.stat().st_size != candidate["size"]:
            raise UpdateError("Downloaded update size does not match the release.")
        if file_hash(archive) != candidate["sha256"]:
            raise UpdateError("Downloaded update checksum does not match the release.")
        new = scratch / "release"
        new.mkdir()
        metadata = _extract(archive, new, installed["platform"])
        if metadata["version"] != version:
            raise UpdateError("Update version does not match the release.")
        verify(new)
        if PENDING_CONFIG.exists():
            raise UpdateError("Restart the device to apply staged settings before updating.")
        destination = releases / release_id(metadata)
        if destination.exists():
            # Reuse a complete inactive tree from an interrupted attempt only
            # when every byte still matches this verified download.
            if destination == (app / previous):
                raise UpdateError("Update is already installed.")
            if validate_release(destination, installed["platform"]) != metadata:
                raise UpdateError("The previously staged release has changed.")
        else:
            os.replace(new, destination)
        _sync_dir(releases)
    _write(state / "transaction.json", {"previous": previous, "candidate": "releases/" + destination.name})
    _status(state, "installing", "Installing update. Reconnect after the modem restarts.")
    try:
        _switch(app, "releases/" + destination.name)
        restart()
        _status(state, "verifying", "Checking the new version.")
        if not check():
            raise UpdateError("The new version did not start correctly.")
        _status(state, "succeeded", "Update installed successfully.", available_version=None)
        (state / "transaction.json").unlink()
        _sync_dir(state)
    except Exception:
        if (state / "transaction.json").exists():
            _rollback(app, state, restart, check)
            raise UpdateError("Update did not complete. The previous version was restored.") from None
        raise UpdateError("Update installed, but its status could not be saved. Restart the device.") from None
    try:
        _prune(app, {PurePosixPath(previous).name, destination.name})
    except OSError:
        pass


def recover(app=None, state=None):
    app, state = app or APP, state or STATE
    with _locked(state):
        if (state / "transaction.json").exists():
            _rollback(app, state, _restart, healthy, boot=True)
        elif (state / "running").exists() or (state / "request.json").exists() or _read(state / "status.json").get("phase") in BUSY:
            _status(state, "failed", "The update was interrupted. Check for updates again.", available_version=None)
        (state / "running").unlink(missing_ok=True)
        (state / "request.json").unlink(missing_ok=True)
        _sync_dir(state)


def finish(app=None, state=None):
    """ExecStopPost handles timeouts or killed workers without waiting for reboot."""
    app, state = app or APP, state or STATE
    with _locked(state):
        if (state / "transaction.json").exists():
            _rollback(app, state, _restart, healthy)
        elif (state / "running").exists():
            _status(state, "failed", "The update was interrupted. Check for updates again.", available_version=None)
        (state / "running").unlink(missing_ok=True)


def worker(app=None, state=None, fetch=download):
    app, state = app or APP, state or STATE
    with _locked(state):
        request = _read(state / "request.json")
        if not request:
            return
        _write(state / "running", {"started": time.time()})
        (state / "request.json").unlink()
        try:
            _, installed = _current(app)
            if request.get("action") == "check":
                candidate = discover(installed, fetch)
                if candidate:
                    _write(state / "candidate.json", candidate)
                    _status(state, "available", "A new version is available.", available_version=candidate["version"])
                else:
                    (state / "candidate.json").unlink(missing_ok=True)
                    _status(state, "idle", "You have the latest stable version.", available_version=None)
            elif request.get("action") == "install":
                if PENDING_CONFIG.exists():
                    raise UpdateError("Restart the device to apply staged settings before updating.")
                install_candidate(app, state, _read(state / "candidate.json"), fetch=fetch)
            else:
                raise UpdateError("Invalid update request.")
        except Exception as error:
            # Network errors may include temporary signed redirect URLs. Never log them.
            message = str(error) if isinstance(error, UpdateError) else "Update failed. Check the connection and try again."
            if _read(state / "status.json").get("phase") != "rolled_back":
                _status(state, "failed", message, available_version=None)
        finally:
            (state / "running").unlink(missing_ok=True)
            _sync_dir(state)


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("The update service requires root.")
    if sys.argv[1:] == ["recover"]:
        recover()
    elif sys.argv[1:] == ["worker"]:
        worker()
    elif sys.argv[1:] == ["finish"]:
        finish()
    else:
        raise SystemExit("Usage: update_manager.py worker|recover|finish")
