#!/usr/bin/env python3
"""Serve raw PPP over the native modem's loopback TCP connection.

Relay design adapted from this repository's RetroExternalModem pppd-wrapper.py.
See software/README.md and the repository license for attribution.
"""

import argparse
import base64
import configparser
import errno
import ipaddress
import json
import logging
import os
import pty
import re
import selectors
import signal
import stat
import socket
import subprocess
import termios
import threading
import time
import tty

from dialback_config import load

LOG = logging.getLogger("dialback-zero-ppp")
BUFFER_LIMIT = 64 * 1024
PROFILE_PORTS = {"internet": 5432, "hub": 6543}
WIREGUARD_CONFIG = "/etc/wireguard/wg-dbz.conf"
WIREGUARD_INTERFACE = "wg-dbz"
INTERFACE_ALIAS = "dialback-zero-managed"
ROUTE_TABLE = "51877"
ROUTE_PROTOCOL = "242"
HUB_RULE_PRIORITY = "10077"
PROBE_RULE_PRIORITY = "10078"


def pppd_command(config, profile):
    if profile not in PROFILE_PORTS:
        raise ValueError("unknown PPP profile")
    network = config["hub"] if profile == "hub" else config["ppp"]
    interface = "ppp-hub" if profile == "hub" else "ppp-inet"
    command = ["/usr/sbin/pppd", "nodetach", "local", "noauth", "nocrtscts",
               "nodefaultroute", "noipv6", "noipdefault", "noproxyarp",
               "ifname", interface, "mtu", "1280", "mru", "1280",
               "asyncmap", "0", "connect-delay", "5000", "lcp-echo-interval", "30",
               "lcp-echo-failure", "4", f"{network['local']}:{network['peer']}",
               "ms-dns", network["dns"], "logfd", "2"]
    return command


def parse_tunnel_config(text, config):
    """Accept only our single-peer, IPv4 host-only configuration; never hooks.

    Return normalized fields and a wg-only configuration. Neither the caller
    nor errors should log this result, because it contains the private key.
    """
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.Error as error:
        raise ValueError("invalid WireGuard configuration syntax") from None
    interface_fields = {"PrivateKey", "Address", "Table"}
    peer_fields = {"PublicKey", "Endpoint", "AllowedIPs", "PersistentKeepalive"}
    if (parser.defaults() or set(parser.sections()) != {"Interface", "Peer"}
            or set(parser["Interface"]) != interface_fields
            or set(parser["Peer"]) != peer_fields):
        raise ValueError("WireGuard configuration must contain only the supported single-peer settings")
    interface, peer = parser["Interface"], parser["Peer"]
    if interface["Table"] != "off":
        raise ValueError("WireGuard Table must be off")
    for section, key in ((interface, "PrivateKey"), (peer, "PublicKey")):
        try:
            decoded = base64.b64decode(section[key], validate=True)
        except (ValueError, TypeError):
            raise ValueError("invalid WireGuard key") from None
        if len(decoded) != 32 or not any(decoded):
            raise ValueError("invalid WireGuard key")
    try:
        address = ipaddress.IPv4Interface(interface["Address"])
        allowed = ipaddress.IPv4Network(peer["AllowedIPs"], strict=True)
    except ValueError:
        raise ValueError("WireGuard addresses must be single IPv4 hosts") from None
    server = ipaddress.IPv4Address(config["hub"]["server"])
    forbidden = {server, *(ipaddress.IPv4Address(config[section][key])
                          for section in ("ppp", "hub") for key in ("local", "peer"))}
    private_networks = tuple(ipaddress.IPv4Network(net) for net in
                             ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    if (address.network.prefixlen != 32 or address.ip in forbidden
            or not any(address.ip in net for net in private_networks)):
        raise ValueError("WireGuard client address must be a distinct private IPv4 /32")
    if allowed.prefixlen != 32 or allowed.network_address != server:
        raise ValueError("WireGuard AllowedIPs must be exactly the configured hub server /32")
    endpoint = peer["Endpoint"]
    host, separator, port_text = endpoint.rpartition(":")
    if not separator or not port_text.isascii() or not port_text.isdecimal() or not 1 <= int(port_text) <= 65535:
        raise ValueError("invalid WireGuard endpoint port")
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
        except ValueError:
            raise ValueError("invalid WireGuard endpoint host") from None
    elif len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
        raise ValueError("invalid WireGuard endpoint host")
    keepalive = peer["PersistentKeepalive"]
    if not keepalive.isascii() or not keepalive.isdecimal() or not 1 <= int(keepalive) <= 65535:
        raise ValueError("invalid WireGuard keepalive")
    wg_config = ("[Interface]\nPrivateKey = " + interface["PrivateKey"] +
                 "\n[Peer]\nPublicKey = " + peer["PublicKey"] +
                 "\nEndpoint = " + endpoint + "\nAllowedIPs = " + str(allowed) +
                 "\nPersistentKeepalive = " + str(int(keepalive)) + "\n")
    return {"address": str(address), "client": str(address.ip), "server": str(server),
            "public_key": peer["PublicKey"], "endpoint": endpoint,
            "allowed_ips": str(allowed), "wg_config": wg_config}


def read_tunnel_config(config, path=WIREGUARD_CONFIG):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, encoding="ascii") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
            raise ValueError("WireGuard configuration must be a root-owned private regular file (0600)")
        text = stream.read(8193)
        if len(text) > 8192:
            raise ValueError("WireGuard configuration is too large")
    return parse_tunnel_config(text, config)


def run_network(command, **kwargs):
    kwargs.setdefault("check", True)
    kwargs.setdefault("timeout", 10)
    return subprocess.run(command, **kwargs)


def network_json(runner, arguments):
    return json.loads(runner(["ip", "-j", *arguments], capture_output=True, text=True).stdout)


def owned_interface(runner):
    links = network_json(runner, ["-details", "link", "show"])
    existing = next((link for link in links if link.get("ifname") == WIREGUARD_INTERFACE), None)
    if existing is not None and (existing.get("ifalias") != INTERFACE_ALIAS
                                or existing.get("linkinfo", {}).get("info_kind") != "wireguard"):
        raise ValueError("wg-dbz already exists and is not managed by Dialback Zero")
    return existing is not None


def check_policy_ownership(runner):
    # Protocol 242 marks only routes/rules installed by this runtime. Abort on
    # collisions instead of flushing somebody else's policy routing table.
    rules = network_json(runner, ["-4", "rule", "show"])
    owned_rules = []
    for rule in rules:
        if str(rule.get("priority")) in (HUB_RULE_PRIORITY, PROBE_RULE_PRIORITY):
            if str(rule.get("protocol")) != ROUTE_PROTOCOL or str(rule.get("table")) != ROUTE_TABLE:
                raise ValueError("Dialback Zero routing rule priority is already in use")
            owned_rules.append(rule)
    result = runner(["ip", "-j", "-4", "route", "show", "table", ROUTE_TABLE],
                    capture_output=True, text=True, check=False)
    if result.returncode and "FIB table does not exist" not in result.stderr:
        raise RuntimeError("could not inspect Dialback Zero route table")
    routes = json.loads(result.stdout or "[]")
    for route in routes:
        if str(route.get("protocol")) != ROUTE_PROTOCOL or not (
                route.get("dev") == WIREGUARD_INTERFACE or
                route.get("type") == "unreachable" and route.get("dst") == "default"):
            raise ValueError("Dialback Zero routing table is already in use")
    return owned_rules


def configure_policy(tunnel, rules, runner):
    # The terminal route prevents policy lookup falling through to main when
    # WireGuard disappears. nft additionally enforces the actual egress device.
    runner(["ip", "-4", "route", "replace", "unreachable", "default", "table", ROUTE_TABLE,
            "protocol", ROUTE_PROTOCOL])
    runner(["ip", "-4", "route", "replace", tunnel["server"] + "/32", "dev", WIREGUARD_INTERFACE,
            "table", ROUTE_TABLE, "protocol", ROUTE_PROTOCOL])
    for rule in rules:
        runner(["ip", "-4", "rule", "del", "priority", str(rule["priority"]),
                "table", ROUTE_TABLE, "protocol", ROUTE_PROTOCOL])
    runner(["ip", "-4", "rule", "add", "priority", HUB_RULE_PRIORITY,
            "iif", "ppp-hub", "table", ROUTE_TABLE, "protocol", ROUTE_PROTOCOL])
    runner(["ip", "-4", "rule", "add", "priority", PROBE_RULE_PRIORITY,
            "from", tunnel["client"] + "/32", "table", ROUTE_TABLE, "protocol", ROUTE_PROTOCOL])


def open_tunnel(config=None, runner=run_network, path=WIREGUARD_CONFIG):
    """Configure our dedicated interface, never invoke wg-quick or user hooks."""
    tunnel = read_tunnel_config(config or load(), path)
    exists = owned_interface(runner)
    rules = check_policy_ownership(runner)
    if exists:
        # A previous process may have crashed. Recreate only our marked
        # interface, so stale peers/addresses are not trusted or inherited.
        runner(["ip", "link", "delete", "dev", WIREGUARD_INTERFACE])
    created = False
    try:
        runner(["ip", "link", "add", WIREGUARD_INTERFACE, "type", "wireguard"])
        created = True
        runner(["ip", "link", "set", "dev", WIREGUARD_INTERFACE, "alias", INTERFACE_ALIAS])
        runner(["wg", "setconf", WIREGUARD_INTERFACE, "/dev/stdin"],
                input=tunnel["wg_config"], text=True, stdout=subprocess.DEVNULL)
        runner(["ip", "-4", "address", "add", tunnel["address"], "dev", WIREGUARD_INTERFACE])
        runner(["ip", "link", "set", "dev", WIREGUARD_INTERFACE, "mtu", "1420", "up"])
        # Reverse traffic uses a policy table rather than the main table. Loose
        # reverse-path validation also overrides hosts with all.rp_filter=1.
        runner(["sysctl", "-w", f"net.ipv4.conf.{WIREGUARD_INTERFACE}.rp_filter=2"],
                stdout=subprocess.DEVNULL)
        configure_policy(tunnel, rules, runner)
    except Exception:
        if created:
            runner(["ip", "link", "delete", "dev", WIREGUARD_INTERFACE], check=False)
        raise
    return True


def close_tunnel(runner=run_network):
    try:
        if owned_interface(runner):
            runner(["ip", "link", "delete", "dev", WIREGUARD_INTERFACE])
        # Keep the unreachable route and policy rules as a fail-closed guard.
    except (OSError, subprocess.SubprocessError, ValueError):
        LOG.exception("could not stop Dialback Zero WireGuard interface")


def stop_child(child):
    if child is None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def spawn_pppd(slave_fd, profile, command=None, config=None):
    command = command or pppd_command(config or load(), profile)
    return subprocess.Popen(command, stdin=slave_fd, stdout=slave_fd, stderr=None,
                            close_fds=True, start_new_session=True)


def relay(conn, master_fd, child, stopping):
    """Relay unchanged bytes, preserving partial writes with bounded queues."""
    conn.setblocking(False)
    os.set_blocking(master_fd, False)
    to_pty, to_socket = bytearray(), bytearray()
    pty_eof = False
    child_exit_deadline = None
    with selectors.DefaultSelector() as selector:
        while not stopping.is_set():
            child_status = child.poll()
            if child_status is not None:
                to_pty.clear()
                if child_exit_deadline is None:
                    child_exit_deadline = time.monotonic() + 3
                if time.monotonic() >= child_exit_deadline:
                    return f"pppd exited ({child_status})"
            if pty_eof and not to_socket:
                return "PPP terminal closed"
            socket_events = 0
            if not pty_eof and child_status is None and len(to_pty) < BUFFER_LIMIT:
                socket_events |= selectors.EVENT_READ
            if to_socket:
                socket_events |= selectors.EVENT_WRITE
            pty_events = 0
            if not pty_eof and len(to_socket) < BUFFER_LIMIT:
                pty_events |= selectors.EVENT_READ
            if not pty_eof and to_pty:
                pty_events |= selectors.EVENT_WRITE
            for stream, events in ((conn, socket_events), (master_fd, pty_events)):
                try:
                    selector.get_key(stream)
                except KeyError:
                    if events:
                        selector.register(stream, events)
                else:
                    if events:
                        selector.modify(stream, events)
                    else:
                        selector.unregister(stream)
            ready = selector.select(timeout=.2)
            if not ready and child_status is not None and not to_socket:
                return f"pppd exited ({child_status})"
            for key, events in ready:
                if key.fileobj is conn:
                    if events & selectors.EVENT_READ:
                        try:
                            data = conn.recv(min(4096, BUFFER_LIMIT - len(to_pty)))
                        except BlockingIOError:
                            pass
                        else:
                            if not data:
                                return "caller disconnected"
                            to_pty.extend(data)
                    if events & selectors.EVENT_WRITE:
                        try:
                            sent = conn.send(to_socket)
                        except BlockingIOError:
                            pass
                        else:
                            if sent == 0:
                                return "caller disconnected"
                            del to_socket[:sent]
                else:
                    if events & selectors.EVENT_READ:
                        try:
                            data = os.read(master_fd, min(4096, BUFFER_LIMIT - len(to_socket)))
                        except BlockingIOError:
                            data = None
                        except OSError as error:
                            if error.errno != errno.EIO:
                                raise
                            data = b""
                        if data == b"":
                            pty_eof = True
                            to_pty.clear()
                        elif data:
                            to_socket.extend(data)
                    if events & selectors.EVENT_WRITE and to_pty and not pty_eof:
                        try:
                            sent = os.write(master_fd, to_pty)
                        except BlockingIOError:
                            pass
                        except OSError as error:
                            if error.errno != errno.EIO:
                                raise
                            pty_eof = True
                            to_pty.clear()
                        else:
                            del to_pty[:sent]
    return "service stopping"


def handle_session(conn, profile, stopping, command=None, config=None):
    master_fd = slave_fd = child = None
    try:
        if stopping.is_set():
            return
        master_fd, slave_fd = pty.openpty()
        tty.setraw(slave_fd, when=termios.TCSANOW)
        child = spawn_pppd(slave_fd, profile, command, config)
        os.close(slave_fd)
        slave_fd = None
        LOG.info("%s session: %s", profile, relay(conn, master_fd, child, stopping))
    except (OSError, subprocess.SubprocessError):
        LOG.exception("%s session failed", profile)
    finally:
        conn.close()
        stop_child(child)
        for descriptor in (master_fd, slave_fd):
            if descriptor is not None:
                os.close(descriptor)


def serve(listener, profile, stopping, command=None, config=None):
    listener.settimeout(.5)
    while not stopping.is_set():
        try:
            conn, address = listener.accept()
        except socket.timeout:
            continue
        LOG.info("incoming %s call from %s", profile, address)
        handle_session(conn, profile, stopping, command, config)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILE_PORTS, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load()
    if args.profile == "hub" and not config["modem"]["numbers"]["777"]["enabled"]:
        LOG.info("hub profile disabled")
        return 0
    if args.profile == "hub" and not os.path.isfile(WIREGUARD_CONFIG):
        LOG.error("hub enabled but wg-dbz configuration is absent")
        return 1
    tunnel_owned = False
    stopping = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda _signum, _frame: stopping.set())
    try:
        if args.profile == "hub":
            # Bring the tunnel up before listening, so failed setup is a modem BUSY.
            tunnel_owned = open_tunnel(config)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", PROFILE_PORTS[args.profile]))
            listener.listen(4)
            serve(listener, args.profile, stopping, config=config)
    except OSError:
        LOG.exception("could not run %s listener", args.profile)
        return 1
    except (subprocess.SubprocessError, ValueError, RuntimeError):
        LOG.exception("could not prepare %s transport", args.profile)
        return 1
    finally:
        if tunnel_owned:
            close_tunnel()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
