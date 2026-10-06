#!/usr/bin/env python3
"""Public tunnel diagnostics; never read WireGuard private keys."""

import ipaddress
import json
import secrets
import socket
import struct
import subprocess
import time

INTERFACE = "wg-dbz"


def status(config, runner=subprocess.run, now=None):
    if not config["modem"]["numbers"]["777"]["enabled"]:
        return "Disabled"
    try:
        result = runner(["wg", "show", INTERFACE, "latest-handshakes"],
                        text=True, capture_output=True, timeout=2, check=False)
        if result.returncode:
            return "Tunnel is not running"
        lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
        if len(lines) != 1 or len(lines[0]) != 2:
            return "Tunnel peer is not configured"
        timestamp = int(lines[0][1])
        if timestamp == 0:
            return "Waiting for the first WireGuard handshake"
        age = max(0, int((time.time() if now is None else now) - timestamp))
        return f"Last WireGuard handshake: {age} seconds ago"
    except (OSError, ValueError, subprocess.SubprocessError):
        return "Tunnel status unavailable"


def tunnel_address(runner=subprocess.run):
    result = runner(["ip", "-j", "-4", "address", "show", "dev", INTERFACE],
                    capture_output=True, text=True, check=True, timeout=2)
    addresses = [entry["local"] for link in json.loads(result.stdout)
                 for entry in link.get("addr_info", []) if entry.get("family") == "inet"]
    if len(addresses) != 1:
        raise ValueError("expected a single tunnel IPv4 address")
    return str(ipaddress.IPv4Address(addresses[0]))


def dns_query(transaction):
    name = b"\x05retro\x03net\x00"
    return struct.pack("!6H", transaction, 0, 1, 0, 0, 0) + name + struct.pack("!2H", 1, 1)


def probe(config, runner=subprocess.run, socket_factory=socket.socket):
    """Bound each probe to the tunnel source, whose policy route has no fallback."""
    if not config["modem"]["numbers"]["777"]["enabled"]:
        return [("Tunnel", False, "Enable and configure hub 777 first.")]
    try:
        address = tunnel_address(runner)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        return [("Tunnel", False, "The tunnel address is unavailable.")]
    result = []
    with socket_factory(socket.AF_INET, socket.SOCK_STREAM) as connection:
        try:
            connection.settimeout(3)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, INTERFACE.encode() + b"\0")
            connection.bind((address, 0))
            connection.connect((config["hub"]["server"], 80))
            connection.sendall(b"HEAD / HTTP/1.0\r\nHost: retro.net\r\n\r\n")
            response = b""
            while b"\r\n" not in response and len(response) < 1024:
                chunk = connection.recv(256)
                if not chunk:
                    break
                response += chunk
            success = response.startswith((b"HTTP/1.0 200 ", b"HTTP/1.1 200 "))
            result.append(("Web page", success, "HTTP 200 received." if success else "No HTTP 200 response."))
        except OSError:
            result.append(("Web page", False, "The server did not respond through WireGuard."))
    with socket_factory(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        try:
            connection.settimeout(3)
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, INTERFACE.encode() + b"\0")
            connection.bind((address, 0))
            connection.connect((config["hub"]["dns"], 53))
            transaction = secrets.randbelow(65536)
            query = dns_query(transaction)
            connection.send(query)
            response = connection.recv(4096)
            success = dns_answer_matches(response, query, config["hub"]["server"])
            result.append(("Private DNS", success, "retro.net resolved." if success else "Unexpected DNS response."))
        except OSError:
            result.append(("Private DNS", False, "Private DNS did not respond through WireGuard."))
    return result


def dns_answer_matches(response, query, expected):
    """Check transaction, exact question and the first A answer (including length)."""
    if len(response) < len(query) + 12:
        return False
    transaction, flags, questions, answers, _, _ = struct.unpack("!6H", response[:12])
    if (response[:2] != query[:2] or not flags & 0x8000 or flags & 0x020F
            or questions != 1 or answers < 1 or response[12:len(query)] != query[12:]):
        return False
    position = len(query)
    if response[position:position + 2] == b"\xc0\x0c":
        position += 2
    elif response[position:position + len(query) - 16] == query[12:-4]:
        position += len(query) - 16
    else:
        return False
    if len(response) < position + 14:
        return False
    kind, klass, ttl, length = struct.unpack("!2HIH", response[position:position + 10])
    return (kind == 1 and klass == 1 and length == 4
            and response[position + 10:position + 14] == socket.inet_aton(expected))
