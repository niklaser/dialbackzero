#!/usr/bin/env python3
"""Name, checksum, and describe one completed pi-gen image."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import shutil
from pathlib import Path


MAX_IMAGE_BYTES = 2 * 1024 * 1024 * 1024


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deploy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--pi-gen-commit", required=True)
    args = parser.parse_args()

    candidates = sorted(args.deploy.glob("*.img.xz"))
    if len(candidates) != 1:
        names = ", ".join(path.name for path in candidates) or "none"
        raise SystemExit(f"expected one .img.xz in {args.deploy}, found: {names}")

    source = candidates[0]
    size = source.stat().st_size
    if size >= MAX_IMAGE_BYTES:
        raise SystemExit(f"compressed image is {size} bytes; artifact limit is below 2 GiB")

    version = safe_version(args.version)
    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / f"dialback-zero-{version}-raspios-trixie-armhf.img.xz"
    shutil.copyfile(source, destination)
    digest = sha256(destination)

    (args.output / "SHA256SUMS").write_text(
        f"{digest}  {destination.name}\n", encoding="utf-8"
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
        "supported_hardware": ["Raspberry Pi Zero", "Raspberry Pi Zero W", "Raspberry Pi Zero 2 W"],
        "version": args.version,
    }
    (args.output / "build-metadata.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
