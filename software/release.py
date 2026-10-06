#!/usr/bin/env python3
"""Shared, dependency-free application release format and integrity checks.

The platform fingerprint deliberately excludes application code. It describes
the fixed image files and installed dependencies that an application expects.
"""

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import stat

SOURCE = Path(__file__).resolve().parent
FORMAT = 1
ARCHITECTURE = "armhf"
BASELINE = "armv6"
HARDWARE_PLATFORM_FILES = (
    "asound.conf", "config.txt.example", "ethernet-config.txt.example",
    "ethernet-nm.conf", "ethernet.service", "logind.conf", "power-switch.rules",
)
REQUIRED_FILES = {
    "bin/dialback-zero-modem", "runtime/launcher.py", "runtime/dialback_config.py",
    "runtime/config-menu", "runtime/release.py", "runtime/update_manager.py", "runtime/prepare-ethernet.py",
    "runtime/ethernet.nmconnection.example",
    "runtime/config_web.py", "runtime/led_daemon.py", "runtime/forwarding.py",
    "runtime/network.py", "runtime/activate_config.py", "runtime/ppp_wrapper.py",
    "runtime/hub_status.py", "runtime/hub_setup.py",
}
MANIFEST_FIELDS = {"format", "version", "source_commit", "architecture", "baseline", "platform", "files"}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RELEASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}-[0-9a-f]{16}\Z")
VERSION = re.compile(r"(?:v[0-9]+\.[0-9]+\.[0-9]+|dev(?:-[A-Za-z0-9][A-Za-z0-9._-]*)?)\Z")


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_bytes(manifest):
    return (json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")


def platform_fingerprint(source_root=SOURCE, dependencies=None):
    """Hash fixed image assets and normalized package dependencies by name."""
    source_root = Path(source_root)
    dependencies = Path(dependencies) if dependencies is not None else (
        source_root.parent / "image/pi-gen-stage/00-dependencies/00-packages-nr")
    inputs = {}
    recovery = source_root / "update_recovery.py"
    if recovery.is_symlink() or not recovery.is_file():
        raise ValueError("fixed platform recovery helper is missing")
    inputs["update_recovery.py"] = file_hash(recovery)
    systemd = source_root / "systemd"
    for path in sorted(systemd.iterdir()):
        if path.is_file() and path.suffix in {".service", ".target", ".timer", ".socket", ".path"}:
            if path.is_symlink():
                raise ValueError(f"platform source must not be a symlink: {path}")
            inputs["systemd/" + path.name] = file_hash(path)
    if not inputs:
        raise ValueError("platform requires systemd unit files")
    for name in HARDWARE_PLATFORM_FILES:
        path = source_root / "hardware" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"missing regular platform source: {path}")
        inputs["hardware/" + name] = file_hash(path)
    packages = sorted({line.split("#", 1)[0].strip() for line in dependencies.read_text().splitlines()
                       if line.split("#", 1)[0].strip()})
    inputs["dependencies"] = hashlib.sha256(("\n".join(packages) + "\n").encode()).hexdigest()
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def payload_path_allowed(name):
    if not isinstance(name, str) or "\\" in name or "\x00" in name:
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or name != path.as_posix() or len(path.parts) != 2:
        return False
    directory, filename = path.parts
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", filename):
        return False
    return ((directory == "bin" and filename == "dialback-zero-modem")
            or (directory == "runtime" and (filename.endswith(".py") or filename in {
                "config-menu", "ethernet.nmconnection.example"}))
            or (directory == "sounds" and filename.endswith(".wav")))


def validate_manifest(manifest, expected_platform=None):
    if not isinstance(manifest, dict) or set(manifest) != MANIFEST_FIELDS:
        raise ValueError("release manifest has unsupported or missing fields")
    if type(manifest["format"]) is not int or manifest["format"] != FORMAT:
        raise ValueError("unsupported release manifest format")
    for name in ("version", "source_commit"):
        value = manifest[name]
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or any(ord(c) < 32 for c in value):
            raise ValueError(f"invalid release {name}")
    if len(manifest["version"]) > 128 or not VERSION.fullmatch(manifest["version"]):
        raise ValueError("release version must be vX.Y.Z or dev with an optional safe suffix")
    if manifest["architecture"] != ARCHITECTURE or manifest["baseline"] != BASELINE:
        raise ValueError("release requires armhf architecture with armv6 baseline")
    platform = manifest["platform"]
    if not isinstance(platform, str) or not SHA256.fullmatch(platform):
        raise ValueError("invalid release platform fingerprint")
    if expected_platform is not None and platform != expected_platform:
        raise ValueError("release platform differs; install a compatible full image")
    files = manifest["files"]
    if not isinstance(files, dict) or not REQUIRED_FILES.issubset(files):
        raise ValueError("release manifest is missing required application files")
    for name, digest in files.items():
        if not payload_path_allowed(name):
            raise ValueError(f"unsafe or unsupported payload path: {name!r}")
        if not isinstance(digest, str) or not SHA256.fullmatch(digest):
            raise ValueError(f"invalid payload checksum: {name}")
    return manifest


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate release manifest key: {key}")
        result[key] = value
    return result


def read_manifest(path, expected_platform=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("release.json must be a regular file")
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("release manifest is too large")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_keys)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid release manifest JSON: {exc}") from exc
    return validate_manifest(manifest, expected_platform)


def validate_release(directory, expected_platform=None):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("release directory must be a physical directory")
    manifest = read_manifest(directory / "release.json", expected_platform)
    actual = set()
    for path in directory.rglob("*"):
        name = path.relative_to(directory).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"release contains a symlink: {name}")
        if stat.S_ISDIR(mode):
            if name not in {"bin", "runtime", "sounds"}:
                raise ValueError(f"release contains an unexpected directory: {name}")
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"release contains a non-regular file: {name}")
        if name != "release.json":
            actual.add(name)
    if actual != set(manifest["files"]):
        raise ValueError("release file list does not match manifest")
    for name, digest in manifest["files"].items():
        if file_hash(directory / name) != digest:
            raise ValueError(f"release checksum mismatch: {name}")
    return manifest


def release_id(manifest):
    validate_manifest(manifest)
    version = re.sub(r"[^A-Za-z0-9._-]+", "-", manifest["version"]).strip(".-_")[:64] or "release"
    digest = hashlib.sha256(manifest_bytes(manifest)).hexdigest()[:16]
    return version + "-" + digest


def build_payload(binary, destination, *, version="dev", source_commit="unknown", source_root=SOURCE, dependencies=None):
    """Create the complete immutable application tree in an empty directory."""
    binary, destination, source_root = Path(binary), Path(destination), Path(source_root)
    if binary.is_symlink() or not binary.is_file():
        raise ValueError("modem binary must be a regular file")
    if destination.is_symlink() or (destination.exists() and (not destination.is_dir() or any(destination.iterdir()))):
        raise ValueError("release destination must be an empty physical directory")
    sources = {"runtime/" + path.name: path for path in sorted((source_root / "runtime").glob("*.py"))}
    sources.update({
        "runtime/config-menu": source_root / "runtime/config_menu.py",
        "runtime/release.py": source_root / "release.py",
        "runtime/prepare-ethernet.py": source_root / "hardware/prepare-ethernet.py",
        "runtime/ethernet.nmconnection.example": source_root / "hardware/ethernet.nmconnection.example",
        "bin/dialback-zero-modem": binary,
    })
    sources.update({"sounds/" + path.name: path for path in sorted((source_root / "assets").glob("*.wav"))})
    manifest = {"format": FORMAT, "version": version, "source_commit": source_commit,
                "architecture": ARCHITECTURE, "baseline": BASELINE,
                "platform": platform_fingerprint(source_root, dependencies), "files": {}}
    for name, source in sources.items():
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"release source must be a regular file: {source}")
        manifest["files"][name] = file_hash(source)
    validate_manifest(manifest)
    destination.mkdir(parents=True, exist_ok=True)
    for name, source in sources.items():
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)
        path.chmod(0o644 if name.startswith("sounds/") or name.endswith(".example") else 0o755)
    (destination / "release.json").write_bytes(manifest_bytes(manifest))
    (destination / "release.json").chmod(0o644)
    return validate_release(destination)
