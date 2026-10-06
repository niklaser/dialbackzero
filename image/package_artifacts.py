#!/usr/bin/env python3
"""Name, checksum, and describe the image and its matching application update."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import re
import shutil
import tarfile
from pathlib import Path


MAX_IMAGE_BYTES = 2 * 1024 * 1024 * 1024
MAX_UPDATE_BYTES = 64 * 1024 * 1024
MAX_PAYLOAD_BYTES = 128 * 1024 * 1024

HELPER_PATH = Path(__file__).resolve().parents[1] / "software/release.py"
SPEC = importlib.util.spec_from_file_location("dialback_artifact_release", HELPER_PATH)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def safe_version(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")
    if not value:
        raise ValueError("version contains no usable characters")
    return value


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def update_manifest(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_UPDATE_BYTES:
        raise ValueError("update must be a regular archive no larger than 64 MiB")
    with tarfile.open(path, "r:gz") as archive:
        members = {}
        total = 0
        for member in archive:
            if not member.isfile() or member.name in members:
                raise ValueError("update contains a non-regular or duplicate archive entry")
            if member.name != "release.json" and not release.payload_path_allowed(member.name):
                raise ValueError("update contains an unsupported payload path")
            total += member.size
            if total > MAX_PAYLOAD_BYTES or len(members) >= 256:
                raise ValueError("update payload exceeds size or file-count limits")
            members[member.name] = member
        info = members.get("release.json")
        if info is None or info.size > 1024 * 1024:
            raise ValueError("update is missing a bounded release manifest")
        stream = archive.extractfile(info)
        assert stream is not None
        manifest = release.validate_manifest(json.loads(stream.read(), object_pairs_hook=release._unique_keys))
        if set(members) != {"release.json", *manifest["files"]}:
            raise ValueError("update contents do not match the release manifest")
        for name, expected in manifest["files"].items():
            digest = hashlib.sha256()
            stream = archive.extractfile(members[name])
            assert stream is not None
            with stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected:
                raise ValueError(f"update payload checksum mismatch: {name}")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--pi-gen-commit", required=True)
    args = parser.parse_args(argv)

    candidates = sorted(args.deploy.glob("*.img.xz"))
    if len(candidates) != 1:
        names = ", ".join(path.name for path in candidates) or "none"
        raise SystemExit(f"expected one .img.xz in {args.deploy}, found: {names}")

    source = candidates[0]
    size = source.stat().st_size
    if size >= MAX_IMAGE_BYTES:
        raise SystemExit(f"compressed image is {size} bytes; artifact limit is below 2 GiB")

    version = safe_version(args.version)
    updates = sorted(args.deploy.glob("*-armv6-update.tar.gz"))
    if len(updates) != 1:
        raise SystemExit(f"expected one ARMv6 update archive in {args.deploy}, found {len(updates)}")
    try:
        manifest = update_manifest(updates[0])
    except (OSError, ValueError, tarfile.TarError) as exc:
        raise SystemExit(f"invalid update archive: {exc}") from exc
    if manifest["version"] != args.version or manifest["source_commit"] != args.source_commit:
        raise SystemExit("update release identity does not match the image build")
    update_name = f"dialback-zero-{version}-armv6-update.tar.gz"
    if updates[0].name != update_name:
        raise SystemExit("update filename does not match the image version")

    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / f"dialback-zero-{version}-raspios-trixie-armhf.img.xz"
    shutil.copyfile(source, destination)
    digest = sha256(destination)
    update_destination = args.output / update_name
    shutil.copyfile(updates[0], update_destination)
    update_digest = sha256(update_destination)

    (args.output / "SHA256SUMS").write_text(
        f"{digest}  {destination.name}\n{update_digest}  {update_destination.name}\n", encoding="utf-8"
    )
    metadata = {
        "artifact": destination.name,
        "artifact_bytes": destination.stat().st_size,
        "artifact_sha256": digest,
        "boot_tested": False,
        "build_host_architecture": platform.machine(),
        "image_architecture": "armhf",
        "image_baseline": "ARMv6 VFP hard-float",
        "os": "Raspberry Pi OS Lite",
        "os_release": "trixie",
        "pi_gen_commit": args.pi_gen_commit,
        "source_commit": args.source_commit,
        "platform": manifest["platform"],
        "supported_hardware": ["Raspberry Pi Zero", "Raspberry Pi Zero W", "Raspberry Pi Zero 2 W"],
        "update_artifact": update_destination.name,
        "update_artifact_bytes": update_destination.stat().st_size,
        "update_artifact_sha256": update_digest,
        "update_format": manifest["format"],
        "version": args.version,
    }
    (args.output / "build-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
