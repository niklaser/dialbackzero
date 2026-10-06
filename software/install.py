#!/usr/bin/env python3
"""Offline installer for a mounted Dialback Zero Raspberry Pi OS image.
"""

import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile

SOURCE = Path(__file__).resolve().parent
RUNTIME = SOURCE / "runtime"
SYSTEMD = SOURCE / "systemd"
ASSETS = SOURCE / "assets"
SYSTEMD_FILES = tuple(sorted(path for path in SYSTEMD.iterdir()
                             if path.suffix in {".service", ".target", ".timer", ".socket", ".path"}))
ENABLE_UNITS = ("dialback-zero.target",)
PUBLIC_LINKS = {
    "usr/local/lib/dialback-zero": "../../../opt/dialback-zero/current/runtime",
    "usr/local/bin/dialback-zero-modem": "../../../opt/dialback-zero/current/bin/dialback-zero-modem",
    "usr/share/dialback-zero/sounds": "../../../opt/dialback-zero/current/sounds",
}

release_spec = importlib.util.spec_from_file_location("dialback_release", SOURCE / "release.py")
release = importlib.util.module_from_spec(release_spec)
release_spec.loader.exec_module(release)

config_spec = importlib.util.spec_from_file_location("dialback_installer_config", RUNTIME / "dialback_config.py")
config_module = importlib.util.module_from_spec(config_spec)
config_spec.loader.exec_module(config_module)
DEFAULT_CONFIG = config_module.DEFAULT_CONFIG


def target(root, relative, allow_symlink=False):
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"unsafe target path: {relative}")
    destination = root / relative
    if not destination.parent.resolve().is_relative_to(root):
        raise ValueError(f"target escapes selected root: {destination}")
    if destination.is_symlink() and not allow_symlink:
        raise ValueError(f"refusing to replace target symlink: {destination}")
    return destination


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
    plan, masks = module.build_plan(root, hardware, include_runtime=False)
    plan[target(root, "usr/local/lib/dialback-zero-recovery.py")] = (SOURCE / "update_recovery.py").read_text()
    for source in SYSTEMD_FILES:
        plan[target(root, Path("etc/systemd/system") / source.name)] = source.read_text()
    return module, plan, masks


def runtime_targets(root):
    directories = ["opt/dialback-zero", "opt/dialback-zero/releases",
                   "etc/dialback-zero", "var/lib/dialback-zero", "var/lib/dialback-zero/backups",
                   "etc/systemd/system/multi-user.target.wants"]
    files = ["etc/dialback-zero/config.json", "usr/local/lib/dialback-zero-recovery.py"]
    files += [str(Path("etc/systemd/system") / source.name) for source in SYSTEMD_FILES]
    for relative in directories:
        destination = target(root, relative)
        if destination.exists() and not destination.is_dir():
            raise ValueError(f"target directory is obstructed: {destination}")
    for relative in files:
        destination = target(root, relative)
        if destination.exists() and not destination.is_file():
            raise ValueError(f"target file is obstructed: {destination}")
    for relative, wanted in PUBLIC_LINKS.items():
        destination = target(root, relative, allow_symlink=True)
        if destination.is_symlink():
            if os.readlink(destination) != wanted:
                raise ValueError(f"refusing to replace unknown application symlink: {destination}")
            if not destination.resolve().is_relative_to(root / "opt/dialback-zero/releases"):
                raise ValueError(f"application symlink escapes release tree: {destination}")
        elif destination.exists():
            expected = destination.is_file() if relative.endswith("-modem") else destination.is_dir()
            if not expected:
                raise ValueError(f"application path is obstructed: {destination}")
    current = target(root, "opt/dialback-zero/current", allow_symlink=True)
    if current.exists() or current.is_symlink():
        if not current.is_symlink():
            raise ValueError("current release must be a relative symlink")
        value = os.readlink(current)
        if not value.startswith("releases/") or not release.RELEASE_ID.fullmatch(value.removeprefix("releases/")):
            raise ValueError("current release symlink has an unsafe destination")
        selected = target(root, Path("opt/dialback-zero") / value)
        manifest = release.validate_release(selected)
        if selected.name != release.release_id(manifest):
            raise ValueError("current release directory does not match its manifest")
    for unit in ENABLE_UNITS:
        link = target(root, Path("etc/systemd/system/multi-user.target.wants") / unit, allow_symlink=True)
        wanted = "../" + unit
        if (link.exists() or link.is_symlink()) and (not link.is_symlink() or os.readlink(link) != wanted):
            raise ValueError(f"refusing to replace existing enablement path: {link}")


def atomic_symlink(destination, value):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".dialback-link-", dir=destination.parent) as temporary:
        link = Path(temporary) / "link"
        link.symlink_to(value)
        os.replace(link, destination)


def migrate_public_paths(root):
    """Retain an older physical installation before publishing public aliases."""
    existing = [root / relative for relative in PUBLIC_LINKS
                if (root / relative).exists() and not (root / relative).is_symlink()]
    if not existing:
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-application"
    backup = target(root, "var/lib/dialback-zero/backups/" + stamp)
    backup.mkdir(parents=True)
    manifest = [{"path": str(path.relative_to(root)), "existed": True} for path in existing]
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    # Copy all backups first. A copy failure leaves every original usable.
    for path in existing:
        saved = backup / path.relative_to(root)
        saved.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            shutil.copytree(path, saved, symlinks=True)
        else:
            shutil.copy2(path, saved, follow_symlinks=False)
    for path in existing:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def install(root, binary, hardware, dry_run=False, *, version="dev", source_commit="unknown"):
    root, binary = Path(root).resolve(strict=True), Path(binary)
    if not binary.is_file() or binary.is_symlink():
        raise ValueError("--binary must name a regular native modem executable")
    runtime_targets(root)
    hardware_module, hardware_files, masks = hardware_plan(root, hardware)
    for path in hardware_files:
        if path.exists() and not path.is_file():
            raise ValueError(f"platform file is obstructed: {path}")
    for path in masks:
        if path.exists() and not path.is_symlink() and not path.is_file():
            raise ValueError(f"service mask is obstructed: {path}")
    # Prepare and verify every application byte before changing image settings.
    with tempfile.TemporaryDirectory(prefix="dialback-payload-") as temporary:
        payload = Path(temporary) / "release"
        manifest = release.build_payload(binary, payload, version=version, source_commit=source_commit)
        identifier = release.release_id(manifest)
        destination = target(root, "opt/dialback-zero/releases/" + identifier)
        if destination.exists() and release.validate_release(destination) != manifest:
            raise ValueError("existing release differs from prepared payload")
        hardware_module.install(root, hardware_files, masks, dry_run)
        if dry_run:
            print(f"RELEASE {identifier}")
            return
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".stage-", dir=destination.parent) as staged:
                staged_payload = Path(staged) / "release"
                shutil.copytree(payload, staged_payload)
                release.validate_release(staged_payload)
                os.replace(staged_payload, destination)
    migrate_public_paths(root)
    atomic_symlink(target(root, "opt/dialback-zero/current", allow_symlink=True), "releases/" + identifier)
    for relative, wanted in PUBLIC_LINKS.items():
        link = target(root, relative, allow_symlink=True)
        if not link.is_symlink():
            atomic_symlink(link, wanted)
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
    parser.add_argument("--version", default="dev", help="release tag (vX.Y.Z) or dev identifier")
    parser.add_argument("--source-commit", default="unknown", help="source commit for release metadata")
    parser.add_argument("--allow-live-root", action="store_true", help="allow --root / on the target Pi")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        binary = args.binary.absolute()
        if root == Path("/") and not args.allow_live_root:
            raise ValueError("--root / requires --allow-live-root; mounted image roots do not")
        if root == Path("/"):
            model = Path("/proc/device-tree/model")
            if not model.is_file() or "Raspberry Pi Zero" not in model.read_text(errors="replace"):
                raise ValueError("live-root installation is restricted to Raspberry Pi Zero models")
            if os.geteuid() != 0:
                raise ValueError("live-root installation requires root privileges")
        install(root, binary, args.hardware, args.dry_run, version=args.version, source_commit=args.source_commit)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Installation refused: {exc}\n")
    print("Dialback Zero installed offline. Services were enabled for next boot; none were started.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
