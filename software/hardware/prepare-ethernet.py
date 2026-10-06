#!/usr/bin/env python3
"""Prepare one W5500 NetworkManager profile before networking starts on a Pi.

No network commands run here. Existing IP/DNS/priority settings are preserved;
only the locally administered MAC follows the Pi's serial number on each boot.
"""

import configparser
import hashlib
import io
from pathlib import Path
import re
import tempfile

PROFILE = "etc/NetworkManager/system-connections/dialback-zero-w5500.nmconnection"
PROFILE_UUID = "553e90a5-f31d-52a1-a72d-8b514d57b470"
TEMPLATE = "usr/local/lib/dialback-zero/ethernet.nmconnection.example"


def mac_from_serial(serial):
    serial = serial.rstrip("\0\n").lower()
    if not re.fullmatch(r"[0-9a-f]{16}", serial) or int(serial, 16) == 0:
        raise ValueError("A valid, non-zero Pi serial number is required; no shared fallback MAC is allowed")
    octets = bytearray(hashlib.sha256(("Dialback Zero W5500 v1:" + serial).encode("ascii")).digest()[:6])
    octets[0] = (octets[0] & 0xfc) | 0x02  # locally administered, unicast
    return ":".join(f"{octet:02x}" for octet in octets)


def profile_text(existing, template, mac):
    text = existing if existing is not None else template.replace("@MAC@", mac)
    profile = configparser.ConfigParser(interpolation=None, delimiters=("=",), comment_prefixes=("#",))
    profile.read_string(text)
    if profile.get("connection", "uuid", fallback="") != PROFILE_UUID:
        raise ValueError("The Dialback Zero profile path contains an unrelated profile")
    if profile.get("match", "driver", fallback="").rstrip(";") != "w5100":
        raise ValueError("The Dialback Zero profile must match only the w5100 driver")
    # NetworkManager can use either the long section name or its keyfile alias.
    section = "802-3-ethernet" if profile.has_section("802-3-ethernet") else "ethernet"
    if not profile.has_section(section):
        profile.add_section(section)
    if profile.get(section, "cloned-mac-address", fallback="") == mac:
        return text
    profile.set(section, "cloned-mac-address", mac)
    output = io.StringIO()
    profile.write(output, space_around_delimiters=False)
    return output.getvalue()


def prepare(root):
    root = root.resolve(strict=True)
    model = (root / "proc/device-tree/model").read_text().rstrip("\0")
    if not model.startswith("Raspberry Pi Zero"):
        raise ValueError("Ethernet identity preparation is restricted to Raspberry Pi Zero models")
    mac = mac_from_serial((root / "proc/device-tree/serial-number").read_text())
    target = root / PROFILE
    if target.is_symlink() or not target.parent.resolve().is_relative_to(root):
        raise ValueError("Refusing a symlink profile or a path outside the selected root")
    existing = target.read_text() if target.exists() else None
    updated = profile_text(existing, (root / TEMPLATE).read_text(), mac)
    if existing != updated:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=target.parent, delete=False) as temporary:
            temporary.write(updated)
            temporary_path = Path(temporary.name)
        try:
            temporary_path.chmod(0o600)
            temporary_path.replace(target)
        finally:
            temporary_path.unlink(missing_ok=True)
    target.chmod(0o600)
    return mac


if __name__ == "__main__":
    try:
        print("Dialback Zero Ethernet MAC: " + prepare(Path("/")))
    except (OSError, ValueError, configparser.Error) as exc:
        raise SystemExit(f"Ethernet profile preparation failed: {exc}") from exc
