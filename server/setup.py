#!/usr/bin/env python3
"""Create an explicit, one-time pair of WireGuard deployment profiles."""

import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
WORK_DIR = Path(os.environ.get(
    "DIALBACK_WORK_DIR", ROOT.parent / (ROOT.name + "-work"),
)).expanduser().resolve()


PRIVATE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
))


def parse_endpoint(value):
    try:
        host, port_text = value.split(":")
        address = ipaddress.IPv4Address(host)
        if not port_text.isdecimal():
            raise ValueError
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError
        if not any(address in network for network in PRIVATE_NETWORKS):
            raise ValueError
        if address in ipaddress.ip_network("10.77.0.0/24"):
            raise ValueError
        return str(address), port
    except (ValueError, ipaddress.AddressValueError):
        raise argparse.ArgumentTypeError(
            "Use the server's private LAN IPv4 address and UDP port, e.g. "
            "192.168.1.50:51820; the LAN must not overlap 10.77.0.0/24."
        ) from None


def keypair():
    private = subprocess.run(
        ["wg", "genkey"], check=True, text=True, capture_output=True,
    ).stdout.strip()
    public = subprocess.run(
        ["wg", "pubkey"], input=private + "\n", check=True,
        text=True, capture_output=True,
    ).stdout.strip()
    return private, public


def create_bundle(endpoint, output):
    host, port = parse_endpoint(endpoint)
    if shutil.which("wg") is None:
        raise RuntimeError("Install wireguard-tools on the computer running setup.py.")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise FileExistsError("Output already exists; existing keys are never overwritten.")
    server_private, server_public = keypair()
    pi_private, pi_public = keypair()
    old_mask = os.umask(0o077)
    try:
        output.mkdir(parents=True, mode=0o700)
        env = (
            f"DBZ_BIND_IP={host}\n"
            f"DBZ_WG_PORT={port}\n"
            f"WG_PRIVATE_KEY={server_private}\n"
            f"WG_PEER_PUBLIC_KEY={pi_public}\n"
        )
        (output / "server.env").write_text(env, encoding="ascii")
        profile = {
            "version": 1,
            "endpoint": f"{host}:{port}",
            "server_public_key": server_public,
            "private_key": pi_private,
            "address": "10.77.0.2/32",
            "server": "10.77.0.1",
            "dns": "10.77.0.1",
        }
        (output / "pi-hub.json").write_text(
            json.dumps(profile, indent=2) + "\n", encoding="ascii",
        )
    finally:
        os.umask(old_mask)
    return server_public, pi_public


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True, help="Server LAN_IP:UDP_PORT")
    parser.add_argument(
        "--output", type=Path,
        default=WORK_DIR / "private" / "retro-hub",
        help="New private output directory; existing directories are never replaced",
    )
    args = parser.parse_args()
    try:
        server_public, pi_public = create_bundle(args.endpoint, args.output)
    except (OSError, RuntimeError, subprocess.CalledProcessError,
            argparse.ArgumentTypeError) as exc:
        parser.exit(1, f"Setup failed: {exc}\n")
    print(f"Compose environment file: {(args.output / 'server.env').resolve()}")
    print(f"Pi installer profile: {(args.output / 'pi-hub.json').resolve()}")
    print(f"Server public key: {server_public}")
    print(f"Pi public key: {pi_public}")
    print("Both files contain private keys. Store them securely; do not commit them.")


if __name__ == "__main__":
    main()
