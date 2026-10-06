#!/usr/bin/env python3
"""Fixed-platform recovery entry point, independent of the selected release."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

APP = Path("/opt/dialback-zero")
STATE = Path("/var/lib/dialback-zero/updates")
MAX_JSON = 65536
TARGET = re.compile(r"releases/[A-Za-z0-9][A-Za-z0-9._-]{0,63}-[0-9a-f]{16}\Z")
BUSY = {"checking", "downloading", "installing", "verifying"}


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate recovery metadata field")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("Invalid recovery JSON constant")


def read_json(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > MAX_JSON:
        raise ValueError("Invalid recovery metadata file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON + 1)
    if len(raw) > MAX_JSON:
        raise ValueError("Recovery metadata exceeds size limit")
    data = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)
    if not isinstance(data, dict):
        raise ValueError("Invalid recovery metadata object")
    return data


def write_status(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write((json.dumps(data, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def previous_updater(app, journal):
    if set(journal) != {"previous", "candidate"} or any(
            not isinstance(value, str) or not TARGET.fullmatch(value) for value in journal.values()):
        raise ValueError("Invalid recovery transaction")
    previous = app / journal["previous"]
    script = previous / "runtime/update_manager.py"
    for path in (app, app / "releases", previous, previous / "runtime", script):
        if path.is_symlink():
            raise ValueError("Recovery path must not be a symlink")
    if (not script.is_file() or not previous.is_dir()
            or previous.resolve().parent != (app / "releases").resolve()
            or not script.resolve().is_relative_to(previous.resolve())):
        raise ValueError("Previous recovery program is unavailable")
    return script


@contextmanager
def locked(state):
    if state.is_symlink() or not state.is_dir():
        raise ValueError("Invalid recovery state directory")
    descriptor = os.open(state / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def recover(action, app=APP, state=STATE, execute=subprocess.run):
    if action not in {"recover", "finish"}:
        raise ValueError("Expected recover or finish")
    if not state.exists() and not state.is_symlink():
        return
    script = None
    with locked(state):
        transaction = state / "transaction.json"
        if transaction.exists() or transaction.is_symlink():
            script = previous_updater(app, read_json(transaction))
        else:
            running, request, status = state / "running", state / "request.json", state / "status.json"
            data = read_json(status) if status.exists() or status.is_symlink() else {}
            was_running = running.exists() or running.is_symlink()
            queued = request.exists() or request.is_symlink()
            failed_service = action == "finish" and os.environ.get("SERVICE_RESULT", "success") != "success"
            interrupted = was_running or (data.get("phase") in BUSY and (action == "recover" or not queued or failed_service))
            if interrupted or (queued and (action == "recover" or failed_service)):
                data.update(phase="failed", available_version=None,
                            message="The update was interrupted. Check for updates again.")
                write_status(status, data)
            running.unlink(missing_ok=True)
            if action == "recover" or was_running or failed_service:
                request.unlink(missing_ok=True)
            descriptor = os.open(state, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    if script is not None:
        execute([sys.executable, "-B", str(script), action], check=True)


if __name__ == "__main__":
    try:
        if os.geteuid() != 0 or len(sys.argv) != 2:
            raise ValueError("Recovery requires root and recover or finish")
        recover(sys.argv[1])
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit("Dialback Zero recovery failed; the previous release needs attention.") from None
