#!/usr/bin/env python3
"""Export the installed application release as a deterministic ARMv6 update."""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import os
from pathlib import Path
import re
import struct
import subprocess
import tarfile
import tempfile


HELPER_PATH = Path(__file__).resolve().parents[1] / "software/release.py"
SPEC = importlib.util.spec_from_file_location("dialback_package_release", HELPER_PATH)
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


def validate_armv6_binary(path: Path) -> None:
    """Require the original Pi Zero's 32-bit ARM EABI5 hard-float ABI."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("modem binary must be a regular file")
    with path.open("rb") as source:
        header = source.read(52)
    if len(header) != 52 or header[:7] != b"\x7fELF\x01\x01\x01":
        raise ValueError("modem binary must be a little-endian 32-bit ELF")
    elf_type, machine, elf_version = struct.unpack_from("<HHI", header, 16)
    flags = struct.unpack_from("<I", header, 36)[0]
    if elf_type not in (2, 3) or machine != 40 or elf_version != 1:
        raise ValueError("modem binary must be an ARM executable")
    if flags & 0xFF000000 != 0x05000000 or not flags & 0x400 or flags & 0x200:
        raise ValueError("modem binary must use ARM EABI5 hard-float")
    result = subprocess.run(
        ["readelf", "-A", str(path)], check=True, capture_output=True, text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    attributes = result.stdout
    # ARM1176JZF-S (the original Zero) implements ARMv6KZ. Reject newer
    # instructions and microcontroller-only ARMv6-M, even in a 32-bit ELF.
    cpu = re.findall(r"^\s*Tag_CPU_arch:\s*(\S+)\s*$", attributes, re.MULTILINE)
    if not cpu or any(value not in {"v6", "v6K", "v6Z", "v6KZ"} for value in cpu):
        raise ValueError("modem binary must target ARMv6 supported by the original Pi Zero")
    if not re.search(r"^\s*Tag_FP_arch:\s*VFPv2\s*$", attributes, re.MULTILINE):
        raise ValueError("modem binary must target VFPv2")
    if not re.search(r"^\s*Tag_ABI_VFP_args:\s*VFP registers\s*$", attributes, re.MULTILINE):
        raise ValueError("modem binary must pass hard-float arguments in VFP registers")


def package_update(directory: Path, output: Path) -> Path:
    directory = directory.resolve(strict=True)
    manifest = release.validate_release(directory)
    if len(manifest["version"]) > 128 or not re.fullmatch(
        r"(?:v[0-9]+\.[0-9]+\.[0-9]+|dev(?:-[A-Za-z0-9][A-Za-z0-9._-]*)?)", manifest["version"]
    ):
        raise ValueError("update version must be vMAJOR.MINOR.PATCH or dev[-identifier]")
    validate_armv6_binary(directory / "bin/dialback-zero-modem")
    output.mkdir(parents=True, exist_ok=True)
    destination = output / f"dialback-zero-{manifest['version']}-armv6-update.tar.gz"
    with tempfile.NamedTemporaryFile(dir=output, prefix=".update-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            # Omitting the filename and timestamp prevents machine-specific gzip
            # headers. Tar metadata is fixed independently of source permissions.
            with gzip.GzipFile(fileobj=stream, mode="wb", filename="", mtime=0) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    for relative in ["release.json", *sorted(manifest["files"])]:
                        source = directory / relative
                        info = tarfile.TarInfo(relative)
                        info.size = source.stat().st_size
                        info.mode = 0o755 if (
                            relative.startswith("bin/")
                            or relative.startswith("runtime/") and (
                                relative.endswith(".py") or relative == "runtime/config-menu"
                            )
                        ) else 0o644
                        with source.open("rb") as payload:
                            archive.addfile(info, payload)
            stream.flush()
            os.fsync(stream.fileno())
            temporary.chmod(0o644)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        destination = package_update(args.release_dir, args.output)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Update packaging refused: {exc}\n")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
