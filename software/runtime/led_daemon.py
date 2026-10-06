#!/usr/bin/env python3

import json
import fcntl
import os
from pathlib import Path
import select
import signal
import socket
import struct
import time

PINS = {"MR": 23, "NET": 25, "SD": 22, "RD": 27, "CD": 24, "OH": 5, "AA": 6}


class Lights:
    def __init__(self):
        import gpiod
        from gpiod.line import Direction, Value
        self.Value = Value
        settings = gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.INACTIVE)
        self.request = gpiod.request_lines("/dev/gpiochip0", consumer="dialback-zero-leds",
                                           config={tuple(PINS.values()): settings})

    def set(self, name, active):
        self.request.set_value(PINS[name], self.Value.ACTIVE if active else self.Value.INACTIVE)

    def close(self):
        self.request.release()


def apply_event(lights, event, now=None):
    kind = event.get("event")
    value = event.get("value", 0)
    if kind == "ready":
        lights.set("MR", True)
    elif kind in ("connected", "disconnected"):
        lights.set("CD", kind == "connected" and bool(value))
    elif kind in ("offhook", "onhook"):
        lights.set("OH", kind == "offhook" and bool(value))
    elif kind == "autoanswer":
        lights.set("AA", bool(value))
    elif kind == "rx":
        lights.set("SD", bool(value))
        return "SD", (now or time.monotonic()) + .08
    elif kind == "tx":
        lights.set("RD", bool(value))
        return "RD", (now or time.monotonic()) + .08
    return None


def has_upstream_ipv4(names=None, ioctl=fcntl.ioctl):
    """Return true for an up, non-loopback, non-PPP interface with IPv4."""
    names = names if names is not None else [name for _index, name in socket.if_nameindex()]
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            if name == "lo" or name.startswith("ppp") or len(name.encode()) > 15:
                continue
            request = struct.pack("256s", name.encode())
            try:
                flags = struct.unpack("H", ioctl(probe.fileno(), 0x8913, request)[16:18])[0]
                ioctl(probe.fileno(), 0x8915, request)  # SIOCGIFADDR
            except OSError:
                continue
            if flags & 1:
                return True
    finally:
        probe.close()
    return False


def main():
    path = Path(os.environ.get("DIALBACK_EVENT_SOCKET", "/run/dialback-zero/events.sock"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(path))
    os.chmod(path, 0o600)
    netlink = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, socket.NETLINK_ROUTE)
    netlink.bind((os.getpid(), 0x1 | 0x10))  # link and IPv4 address changes
    netlink.setblocking(False)
    lights, deadlines = Lights(), {}
    ready_deadline = 0
    lights.set("NET", has_upstream_ipv4())
    stopping = False
    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping:
            now = time.monotonic()
            if ready_deadline and ready_deadline <= now:
                for name in ("MR", "SD", "RD", "CD", "OH", "AA"):
                    lights.set(name, False)
                deadlines.clear()
                ready_deadline = 0
            for name, deadline in list(deadlines.items()):
                if deadline <= now:
                    lights.set(name, False)
                    del deadlines[name]
            readable, _, _ = select.select([sock, netlink], [], [], .1)
            if netlink in readable:
                try:
                    while netlink.recv(65535):
                        pass
                except BlockingIOError:
                    pass
                lights.set("NET", has_upstream_ipv4())
            if sock in readable:
                try:
                    payload = sock.recv(2048)
                    event = json.loads(payload.decode("utf-8"))
                    if not isinstance(event, dict):
                        continue
                    deadline = apply_event(lights, event, now)
                    if event.get("event") == "ready" and event.get("value", 0):
                        ready_deadline = now + 5
                    if deadline:
                        deadlines[deadline[0]] = deadline[1]
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    pass
    finally:
        lights.close()
        netlink.close()
        sock.close()
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
