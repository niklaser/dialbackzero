#!/usr/bin/env python3
"""Import a private hub bundle without starting services or changing live routes."""

import argparse
import base64
import binascii
from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path
import tempfile

from dialback_config import load, stage, validate

PROFILE_KEYS = {"version", "endpoint", "server_public_key", "private_key",
                "address", "server", "dns"}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(net) for net in
                         ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def private_ipv4(value):
    if not isinstance(value, str):
        raise ValueError("expected a private IPv4 address")
    address = ipaddress.ip_address(value)
    if address.version != 4 or not any(address in net for net in PRIVATE_NETWORKS):
        raise ValueError("expected a private IPv4 address")
    return str(address)


def key_valid(value):
    try:
        decoded = base64.b64decode(value, validate=True)
        return (isinstance(value, str) and len(decoded) == 32 and any(decoded)
                and base64.b64encode(decoded).decode("ascii") == value)
    except (ValueError, TypeError, binascii.Error):
        return False


def validate_profile(profile):
    if not isinstance(profile, dict) or set(profile) != PROFILE_KEYS:
        raise ValueError("invalid hub profile fields")
    if type(profile["version"]) is not int or profile["version"] != 1:
        raise ValueError("unsupported hub profile version")
    for key in ("server_public_key", "private_key"):
        if not key_valid(profile[key]):
            raise ValueError("invalid WireGuard key in hub profile")
    endpoint = profile["endpoint"]
    if not isinstance(endpoint, str) or endpoint.count(":") != 1:
        raise ValueError("endpoint must be a LAN IPv4 address and UDP port")
    host, port = endpoint.split(":")
    private_ipv4(host)
    if not port.isascii() or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("invalid endpoint UDP port")
    if not isinstance(profile["address"], str):
        raise ValueError("invalid tunnel address")
    address = ipaddress.ip_interface(profile["address"])
    if address.version != 4 or address.network.prefixlen != 32:
        raise ValueError("the Pi tunnel address must be a single IPv4 /32")
    client = private_ipv4(str(address.ip))
    server = private_ipv4(profile["server"])
    if profile["dns"] != server:
        raise ValueError("hub DNS must be the hub server")
    if len({host, client, server}) != 3:
        raise ValueError("LAN endpoint and tunnel addresses must be distinct")
    return deepcopy(profile)


def tunnel_config(profile):
    profile = validate_profile(profile)
    return ("[Interface]\n"
            f"PrivateKey = {profile['private_key']}\n"
            f"Address = {profile['address']}\n"
            "Table = off\n\n[Peer]\n"
            f"PublicKey = {profile['server_public_key']}\n"
            f"Endpoint = {profile['endpoint']}\n"
            f"AllowedIPs = {profile['server']}/32\n"
            "PersistentKeepalive = 25\n")


def safe_target(root, relative):
    target = root / relative
    for path in (target, *target.parents):
        if path == root:
            break
        if path.is_symlink():
            raise ValueError("refusing a symlink in the installation path")
    return target


def import_profile(profile, root=Path("/"), replace=False):
    profile = validate_profile(profile)
    root = Path(root).resolve(strict=True)
    active = safe_target(root, "etc/dialback-zero/config.json")
    state = safe_target(root, "var/lib/dialback-zero")
    pending = safe_target(root, "var/lib/dialback-zero/pending-config.json")
    destination = safe_target(root, "etc/wireguard/wg-dbz.conf")
    config = load(pending if pending.exists() else active)
    config["hub"].update(server=profile["server"], dns=profile["dns"])
    config["modem"]["numbers"]["777"] = {
        "enabled": True, "host": "127.0.0.1", "port": 6543}
    config = validate(config)
    occupied = {config[k][p] for k in ("ppp", "hub") for p in ("local", "peer")}
    if str(ipaddress.ip_interface(profile["address"]).ip) in occupied or profile["endpoint"].split(":")[0] in occupied:
        raise ValueError("tunnel or endpoint address overlaps a PPP address")
    rendered = tunnel_config(profile)
    old = destination.read_text() if destination.exists() else None
    if old is not None and old != rendered and not replace:
        raise ValueError("a different hub profile exists; use --replace to replace it")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if old != rendered:
        with tempfile.NamedTemporaryFile("w", dir=destination.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(rendered)
        try:
            temporary.chmod(0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        destination.chmod(0o600)
    stage(config, active, state)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path("/"), help="mounted Pi root, or / on the Pi")
    parser.add_argument("--replace", action="store_true", help="replace an existing hub key/configuration")
    args = parser.parse_args(argv)
    try:
        root = args.root.resolve(strict=True)
        if root == Path("/"):
            if os.geteuid() != 0:
                raise ValueError("run this import with sudo on the Pi")
            model = Path("/proc/device-tree/model")
            if not model.is_file() or "Raspberry Pi Zero" not in model.read_text(errors="replace"):
                raise ValueError("live import is restricted to Raspberry Pi Zero models")
        if args.profile.stat().st_size > 8192:
            raise ValueError("hub profile is too large")
        profile = json.loads(args.profile.read_text())
        import_profile(profile, root, args.replace)
    except (OSError, ValueError, TypeError) as exc:
        # Do not print parser exceptions: they can contain imported key material.
        parser.exit(1, "Hub import failed. Check the profile, destination and --replace option.\n")
    print("Hub profile installed; settings staged. No services were restarted.")
    print("After installing the runtime, restart dialback-zero.target or reboot to apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
