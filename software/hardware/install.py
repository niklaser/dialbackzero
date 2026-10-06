#!/usr/bin/env python3
"""Install Dialback Zero carrier settings on a Pi Zero or a mounted Pi OS image.

No services are started/stopped and no shutdown/reboot is requested. Settings
take full effect at the next boot. Existing changed files are backed up.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

SOURCE = Path(__file__).resolve().parent
START = "# BEGIN Dialback Zero managed include"
END = "# END Dialback Zero managed include"
INCLUDE = f"{START}\n[all]\ninclude dialback-zero.txt\n{END}\n"
OVERLAYS = {"audremap", "gpio-shutdown", "gpio-poweroff", "pwm", "pwm-2chan"}
MASKS = ("hciuart.service", "serial-getty@serial0.service", "serial-getty@ttyAMA0.service")


def managed_config(text):
    if text.count(START) != text.count(END) or text.count(START) > 1:
        raise ValueError("Malformed/duplicate Dialback Zero config markers")
    clean = re.sub(re.escape(START) + r".*?" + re.escape(END) + r"\n?", "", text, flags=re.S)
    return clean.rstrip() + "\n\n" + INCLUDE


def inside(root, relative):
    path = root / relative
    # Offline images must not follow an absolute symlink into the host filesystem.
    if not path.parent.resolve().is_relative_to(root):
        raise ValueError(f"Path escapes the selected root: {path}")
    if path.is_symlink():
        raise ValueError(f"Refusing to replace a symlink configuration file: {path}")
    return path


def check_boot_includes(boot, main_text, ethernet=False):
    seen = set()

    def scan(text, name):
        text = re.sub(re.escape(START) + r".*?" + re.escape(END), "", text, flags=re.S)
        for number, raw in enumerate(text.splitlines(), 1):
            line = raw.split("#", 1)[0].strip()
            if line.startswith("dtoverlay="):
                overlay = line.split("=", 1)[1].split(",", 1)[0]
                if overlay in OVERLAYS or (ethernet and (
                    overlay in {"w5500", "w5100", "enc28j60", "w1-gpio", "w1-gpio-pullup"}
                    or overlay.startswith("spi0")
                )):
                    raise ValueError(f"Conflicting overlay in {name}:{number}; remove the old manual entry first: {line}")
            if line.startswith("gpio="):
                pin_spec = line.split("=", 2)[1]
                pins = set()
                for item in pin_spec.split(","):
                    ends = item.strip().split("-")
                    if all(end.isdigit() for end in ends) and len(ends) in (1, 2):
                        pins.update(range(int(ends[0]), int(ends[-1]) + 1))
                if pins & {3, 26}:
                    raise ValueError(f"Conflicting power GPIO setting in {name}:{number}: {line}")
                if ethernet and pins & {4, 8, 9, 10, 11}:
                    raise ValueError(f"Conflicting Ethernet GPIO setting in {name}:{number}: {line}")
            if line.startswith("include "):
                include = line.split(None, 1)[1].strip()
                if include == "dialback-zero.txt":
                    raise ValueError("Unmanaged include dialback-zero.txt already exists; remove it before installing")
                path = (boot / include).resolve()
                if not path.is_relative_to(boot.resolve()):
                    raise ValueError(f"Boot include escapes boot directory: {include}")
                if path.exists() and path not in seen:
                    seen.add(path)
                    scan(path.read_text(), str(path))

    scan(main_text, "config.txt")


def build_plan(root, hardware="rev-b-ltc2954"):
    if hardware not in {"rev-b-ltc2954", "rev-c-ethernet"}:
        raise ValueError(f"Unsupported hardware: {hardware}")
    ethernet = hardware == "rev-c-ethernet"
    boot = root / "boot/firmware"
    if not (boot / "config.txt").is_file():
        boot = root / "boot"
    config = inside(root, str((boot / "config.txt").relative_to(root)))
    cmdline = inside(root, str((boot / "cmdline.txt").relative_to(root)))
    if not config.is_file() or not cmdline.is_file():
        raise ValueError("Mount the Pi OS root and boot partitions; config.txt and cmdline.txt are required")
    required = ["audremap", "gpio-shutdown", "gpio-poweroff", "disable-bt"]
    if ethernet:
        required.append("w5500")
        if not any((root / p).is_file() for p in (
            "usr/lib/systemd/system/NetworkManager.service",
            "lib/systemd/system/NetworkManager.service",
        )):
            raise ValueError("Rev C Ethernet requires Raspberry Pi OS with NetworkManager installed")
    for overlay in required:
        if not (boot / "overlays" / f"{overlay}.dtbo").is_file():
            raise ValueError(f"Missing {overlay}.dtbo in the target boot partition")
    if not (root / "etc/systemd").is_dir():
        raise ValueError("Target must be a systemd-based Raspberry Pi OS image")
    text = config.read_text()
    check_boot_includes(boot, text, ethernet)
    args = cmdline.read_text().strip().splitlines()
    if len(args) != 1:
        raise ValueError("cmdline.txt must contain exactly one non-empty line")
    tokens = [t for t in args[0].split() if not re.match(r"console=(serial0|ttyAMA0)(,|$)", t)]
    boot_settings = (SOURCE / "config.txt.example").read_text()
    if ethernet:
        boot_settings += "\n" + (SOURCE / "ethernet-config.txt.example").read_text()
    plan = {
        config: managed_config(text),
        cmdline: " ".join(tokens) + "\n",
        inside(root, str((boot / "dialback-zero.txt").relative_to(root))): boot_settings,
        inside(root, "etc/asound.conf"): (SOURCE / "asound.conf").read_text(),
        inside(root, "etc/systemd/logind.conf.d/90-dialback-zero.conf"): (SOURCE / "logind.conf").read_text(),
        inside(root, "etc/udev/rules.d/70-dialback-zero-power.rules"): (SOURCE / "power-switch.rules").read_text(),
    }
    if ethernet:
        for target, source in {
            "usr/local/lib/dialback-zero/prepare-ethernet.py": "prepare-ethernet.py",
            "usr/local/lib/dialback-zero/ethernet.nmconnection.example": "ethernet.nmconnection.example",
            "etc/systemd/system/dialback-zero-ethernet.service": "ethernet.service",
            "etc/systemd/system/NetworkManager.service.d/90-dialback-zero-ethernet.conf": "ethernet-nm.conf",
        }.items():
            plan[inside(root, target)] = (SOURCE / source).read_text()
    mask_paths = []
    for unit in MASKS:
        path = root / "etc/systemd/system" / unit
        if not path.parent.resolve().is_relative_to(root):
            raise ValueError(f"Service mask escapes the selected root: {path}")
        mask_paths.append(path)
    return plan, mask_paths


def install(root, plan, masks, dry_run):
    changed = [(p, data) for p, data in plan.items() if not p.exists() or p.read_text() != data]
    changed_masks = [p for p in masks if not p.is_symlink() or os.readlink(p) != "/dev/null"]
    for path, _ in changed:
        print(f"WRITE {path}")
    for path in changed_masks:
        print(f"MASK  {path}")
    if dry_run or not (changed or changed_masks):
        print("Dry run; nothing changed." if dry_run else "Already installed; nothing changed.")
        return
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = inside(root, f"var/lib/dialback-zero/backups/{stamp}/manifest.json").parent
    backup.mkdir(parents=True)
    manifest = []
    for path in [p for p, _ in changed] + changed_masks:
        relative = path.relative_to(root)
        exists = path.exists() or path.is_symlink()
        manifest.append({"path": str(relative), "existed": exists})
        if exists:
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, saved, follow_symlinks=False)
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    for path, data in changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as tmp:
            tmp.write(data)
            temp_path = Path(tmp.name)
        try:
            temp_path.chmod(0o644)
            temp_path.replace(path)
        finally:
            temp_path.unlink(missing_ok=True)
    for path in changed_masks:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.unlink(missing_ok=True)
        path.symlink_to("/dev/null")
    print(f"Backups: {backup}")
    print("Installed. No services restarted and no reboot requested. Reboot the target Pi to activate.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/"), help="Mounted Pi OS root (default: this Pi)")
    parser.add_argument("--hardware", required=True, choices=["rev-b-ltc2954", "rev-c-ethernet"], help="Mono carrier with LTC2954-1 power control; Rev C adds W5500 Ethernet")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if root == Path("/"):
            model = Path("/proc/device-tree/model")
            if not model.exists() or "Raspberry Pi Zero" not in model.read_text():
                raise ValueError("Live installation is restricted to Raspberry Pi Zero models; use --root for a mounted image")
            if not args.dry_run and os.geteuid() != 0:
                raise ValueError("Run the live installation with sudo")
        plan, masks = build_plan(root, args.hardware)
        install(root, plan, masks, args.dry_run)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Installation refused: {exc}\n")


if __name__ == "__main__":
    main()
