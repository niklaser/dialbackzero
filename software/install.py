#!/usr/bin/env python3
"""Offline installer for a mounted Dialback Zero Raspberry Pi OS image.
"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

SOURCE = Path(__file__).resolve().parent
RUNTIME = SOURCE / "runtime"
SYSTEMD = SOURCE / "systemd"
ASSETS = SOURCE / "assets"
SYSTEMD_FILES = tuple(sorted((*SYSTEMD.glob("*.service"), *SYSTEMD.glob("*.target"))))
ENABLE_UNITS = ("dialback-zero.target",)

config_spec = importlib.util.spec_from_file_location("dialback_installer_config", RUNTIME / "dialback_config.py")
config_module = importlib.util.module_from_spec(config_spec)
config_spec.loader.exec_module(config_module)
DEFAULT_CONFIG = config_module.DEFAULT_CONFIG


def target(root, relative, allow_symlink=False):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe target path: {relative}")
    destination = root / relative
    parent = destination.parent
    existing = parent
    while not existing.exists():
        existing = existing.parent
    if not existing.resolve().is_relative_to(root):
        raise ValueError(f"target escapes selected root: {destination}")
    if destination.is_symlink() and not allow_symlink:
        raise ValueError(f"refusing to replace target symlink: {destination}")
    return destination


def copy_file(source, destination, mode=None, preserve=False):
    if preserve and destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    with source.open("rb") as input_stream, tempfile.NamedTemporaryFile("wb", dir=destination.parent, delete=False) as output:
        shutil.copyfileobj(input_stream, output)
        temporary = Path(output.name)
    try:
        temporary.chmod(mode if mode is not None else stat.S_IMODE(source.stat().st_mode))
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def write_default_config(destination):
    if destination.exists():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(DEFAULT_CONFIG, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    return True


def hardware_plan(root, hardware):
    path = SOURCE / "hardware" / "install.py"
    spec = importlib.util.spec_from_file_location("dialback_hardware_installer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan, masks = module.build_plan(root, hardware)
    return module, plan, masks


def runtime_targets(root):
    directories = ["usr/local/lib/dialback-zero", "usr/share/dialback-zero/sounds",
                   "etc/dialback-zero", "var/lib/dialback-zero",
                   "etc/systemd/system/multi-user.target.wants"]
    files = ["usr/local/lib/dialback-zero/config-menu", "usr/local/bin/dialback-zero-modem",
             "etc/dialback-zero/config.json"]
    files += [str(Path("usr/local/lib/dialback-zero") / source.name) for source in RUNTIME.glob("*.py")]
    files += [str(Path("usr/share/dialback-zero/sounds") / source.name) for source in ASSETS.glob("*.wav")]
    files += [str(Path("etc/systemd/system") / source.name) for source in SYSTEMD_FILES]
    for relative in directories:
        destination = target(root, relative)
        if destination.exists() and not destination.is_dir():
            raise ValueError(f"target directory is obstructed: {destination}")
    for relative in files:
        destination = target(root, relative)
        if destination.exists() and not destination.is_file():
            raise ValueError(f"target file is obstructed: {destination}")
    for unit in ENABLE_UNITS:
        link = target(root, Path("etc/systemd/system/multi-user.target.wants") / unit, allow_symlink=True)
        wanted = "../" + unit
        if (link.exists() or link.is_symlink()) and (not link.is_symlink() or os.readlink(link) != wanted):
            raise ValueError(f"refusing to replace existing enablement path: {link}")


def install(root, binary, hardware, dry_run=False):
    if not binary.is_file() or binary.is_symlink():
        raise ValueError("--binary must name a regular native modem executable")
    hardware_module, hardware_files, masks = hardware_plan(root, hardware)
    runtime_targets(root)
    hardware_module.install(root, hardware_files, masks, dry_run)
    if dry_run:
        return
    library = target(root, "usr/local/lib/dialback-zero")
    for source in sorted(RUNTIME.glob("*.py")):
        copy_file(source, target(root, library.relative_to(root) / source.name), 0o755)
    menu = target(root, "usr/local/lib/dialback-zero/config-menu")
    copy_file(RUNTIME / "config_menu.py", menu, 0o755)
    copy_file(binary, target(root, "usr/local/bin/dialback-zero-modem"), 0o755)
    for source in sorted(ASSETS.glob("*.wav")):
        copy_file(source, target(root, Path("usr/share/dialback-zero/sounds") / source.name), 0o644)
    for source in SYSTEMD_FILES:
        copy_file(source, target(root, Path("etc/systemd/system") / source.name), 0o644)
    write_default_config(target(root, "etc/dialback-zero/config.json"))
    target(root, "var/lib/dialback-zero").mkdir(parents=True, exist_ok=True)
    target(root, "var/lib/dialback-zero").chmod(0o700)
    wants = target(root, "etc/systemd/system/multi-user.target.wants")
    wants.mkdir(parents=True, exist_ok=True)
    for unit in ENABLE_UNITS:
        link = target(root, Path("etc/systemd/system/multi-user.target.wants") / unit, allow_symlink=True)
        wanted = "../" + unit
        if link.is_symlink() and os.readlink(link) == wanted:
            continue
        if link.exists() or link.is_symlink():
            raise ValueError(f"refusing to replace existing enablement path: {link}")
        link.symlink_to(wanted)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="mounted Raspberry Pi OS root")
    parser.add_argument("--binary", type=Path, required=True, help="compiled tcpser binary")
    parser.add_argument("--hardware", choices=("rev-b-ltc2954", "rev-c-ethernet"), required=True)
    parser.add_argument("--allow-live-root", action="store_true", help="allow --root / on the target Pi")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        binary = args.binary.resolve(strict=True)
        if root == Path("/") and not args.allow_live_root:
            raise ValueError("--root / requires --allow-live-root; mounted image roots do not")
        if root == Path("/"):
            model = Path("/proc/device-tree/model")
            if not model.is_file() or "Raspberry Pi Zero" not in model.read_text(errors="replace"):
                raise ValueError("live-root installation is restricted to Raspberry Pi Zero models")
            if os.geteuid() != 0:
                raise ValueError("live-root installation requires root privileges")
        install(root, binary, args.hardware, args.dry_run)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Installation refused: {exc}\n")
    print("Dialback Zero installed offline. Services were enabled for next boot; none were started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
