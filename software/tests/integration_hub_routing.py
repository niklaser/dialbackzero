#!/usr/bin/env python3
"""Exercise real nftables and WireGuard entirely inside disposable namespaces.

Run: python3 software/tests/integration_hub_routing.py
Requires Linux user/network namespaces, iproute2, nftables, WireGuard and ping.
No sudo, host interfaces, host routes or live Pi are used. Sandboxes that deny
netlink need to permit this explicitly isolated command.
"""

import copy
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import dialback_config
import forwarding
import ppp_wrapper as ppp


def run(args, **kwargs):
    kwargs.setdefault("check", True)
    kwargs.setdefault("timeout", 15)
    return subprocess.run(args, **kwargs)


def output(args, **kwargs):
    return run(args, capture_output=True, text=True, **kwargs).stdout.strip()


def in_ns(pid, args, **kwargs):
    return run(["nsenter", "-t", str(pid), "-n", *args], **kwargs)


def ns_command(pid, args):
    return ["nsenter", "-t", str(pid), "-n", *args]


HTTP_SERVER = r'''
import http.server
class Handler(http.server.BaseHTTPRequestHandler):
 def do_GET(self):
  body=self.client_address[0].encode('ascii')
  self.send_response(200); self.send_header('Content-Length', str(len(body)))
  self.end_headers(); self.wfile.write(body)
 def log_message(self,*args): pass
http.server.HTTPServer(('0.0.0.0', int(__import__('sys').argv[1])),Handler).serve_forever()
'''
HTTP_CLIENT = r'''
import http.client, sys
try:
 c=http.client.HTTPConnection(sys.argv[1], int(sys.argv[2]), timeout=0.8,
  source_address=(sys.argv[3],0) if len(sys.argv)>3 else None)
 c.request('GET','/'); r=c.getresponse()
 assert r.status==200
 print(r.read().decode()); c.close()
except OSError:
 sys.exit(3)
'''

UDP_SERVER = r'''
import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.bind(('0.0.0.0',8181))
while True:
 data, address=s.recvfrom(1024); s.sendto(address[0].encode(),address)
'''
UDP_CLIENT = r'''
import socket,sys
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.settimeout(.8)
for command in sys.stdin:
 s.sendto(b'probe',('10.77.0.1',8181))
 try: print(s.recv(1024).decode(),flush=True)
 except OSError: print('BLOCKED',flush=True)
'''


def http(pid, address, port=8080, source=None, succeeds=True):
    args = [sys.executable, "-c", HTTP_CLIENT, address, str(port)]
    if source:
        args.append(source)
    result = in_ns(pid, args, capture_output=True, text=True, check=False)
    if succeeds and result.returncode:
        raise AssertionError(f"HTTP {address} failed: {result.stderr}")
    if not succeeds and result.returncode != 3:
        raise AssertionError(f"HTTP {address} should be blocked, got {result.returncode}: {result.stdout} {result.stderr}")
    return result.stdout.strip()


def main():
    if "--inside" not in sys.argv:
        return run(["unshare", "--user", "--map-root-user", "--net", sys.executable,
                    str(Path(__file__).resolve()), "--inside", os.readlink("/proc/self/ns/net"),
                    os.readlink("/proc/self/ns/user")], check=False, timeout=90).returncode
    # unshare replaces itself with this child. Parent namespace identities were
    # captured BEFORE unshare; user-namespace root cannot read parent /proc/ns.
    if (len(sys.argv) != 4 or os.geteuid() != 0
            or os.readlink("/proc/self/ns/net") == sys.argv[2]
            or os.readlink("/proc/self/ns/user") == sys.argv[3]):
        raise RuntimeError("refusing to run outside isolated network AND user namespaces")
    print("Isolated test:", sys.argv[2], "->", os.readlink("/proc/self/ns/net"), flush=True)
    keepers, servers = [], []
    try:
        run(["ip", "link", "set", "lo", "up"])
        value = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        value["hub"] = {"local": "172.16.77.1", "peer": "172.16.77.2", "server": "10.77.0.1", "dns": "10.77.0.1"}
        # Start with the deployed legacy declaration, whose base-chain
        # priority differs. Flushing rules alone cannot migrate this table.
        run(["nft", "-f", "-"], input="""table inet dialback_zero {
 chain forward { type filter hook forward priority 0; policy accept; iifname "ppp*" accept; }
 chain postrouting { type nat hook postrouting priority srcnat; policy accept; }
}
table inet dbz_unrelated {
 chain sentinel { ip saddr 192.0.2.254 drop; }
}
""", text=True)
        unrelated = output(["nft", "list", "table", "inet", "dbz_unrelated"])
        # Both upgrade and subsequent replacement must succeed atomically,
        # preserving every unrelated table on the same host.
        for _ in range(2):
            run(["nft", "-f", "-"], input=forwarding.nft_rules(value), text=True)
            assert output(["nft", "list", "table", "inet", "dbz_unrelated"]) == unrelated
        migrated = json.loads(output(["nft", "-j", "list", "table", "inet", "dialback_zero"]))
        forward = next(item["chain"] for item in migrated["nftables"]
                       if item.get("chain", {}).get("name") == "forward")
        assert forward["prio"] == -10, "legacy base-chain declaration was not replaced"
        # A fresh boot, where our table does not yet exist, must also work.
        run(["nft", "delete", "table", "inet", "dialback_zero"])
        run(["nft", "-f", "-"], input=forwarding.nft_rules(value), text=True)
        assert output(["nft", "list", "table", "inet", "dbz_unrelated"]) == unrelated
        run(["sysctl", "-qw", "net.ipv4.ip_forward=1"])
        peers = []
        for index, (name, router_ip, client_ip) in enumerate((
                ("ppp-hub", "172.16.77.1", "172.16.77.2"),
                ("ppp-inet", "172.16.0.1", "172.16.0.2"),
                ("eth0", "192.0.2.2", "192.0.2.1"))):
            keeper = subprocess.Popen(["unshare", "--net", "sleep", "90"])
            keepers.append(keeper)
            for _ in range(100):
                if keeper.poll() is not None:
                    raise RuntimeError("could not create a peer namespace")
                if os.readlink(f"/proc/{keeper.pid}/ns/net") != os.readlink("/proc/self/ns/net"):
                    break
                time.sleep(.01)
            else:
                raise RuntimeError("peer namespace did not start")
            other = "peer" + str(index)
            run(["ip", "link", "add", name, "type", "veth", "peer", "name", other])
            run(["ip", "link", "set", other, "netns", str(keeper.pid)])
            run(["ip", "addr", "add", router_ip + "/24", "dev", name])
            run(["ip", "link", "set", name, "up"])
            in_ns(keeper.pid, ["ip", "link", "set", "lo", "up"])
            in_ns(keeper.pid, ["ip", "addr", "add", client_ip + "/24", "dev", other])
            in_ns(keeper.pid, ["ip", "link", "set", other, "up"])
            in_ns(keeper.pid, ["ip", "route", "add", "default", "via", router_ip])
            peers.append(keeper.pid)
        hub, internet, server = peers
        run(["ip", "route", "add", "default", "via", "192.0.2.1"])
        client_private, server_private = output(["wg", "genkey"]), output(["wg", "genkey"])
        client_public = output(["wg", "pubkey"], input=client_private + "\n")
        server_public = output(["wg", "pubkey"], input=server_private + "\n")
        in_ns(server, ["ip", "link", "add", "wg-server", "type", "wireguard"])
        in_ns(server, ["wg", "setconf", "wg-server", "/dev/stdin"], input=(
            f"[Interface]\nPrivateKey = {server_private}\nListenPort = 51820\n"
            f"[Peer]\nPublicKey = {client_public}\nAllowedIPs = 10.77.0.2/32\n"), text=True)
        in_ns(server, ["ip", "addr", "add", "10.77.0.1/32", "dev", "wg-server"])
        in_ns(server, ["ip", "link", "set", "wg-server", "up"])
        in_ns(server, ["ip", "route", "add", "10.77.0.2/32", "dev", "wg-server"])
        with tempfile.TemporaryDirectory(prefix="dialback-routing-test-") as tmp:
            conf = Path(tmp) / "wg-dbz.conf"
            conf.write_text(f"[Interface]\nPrivateKey = {client_private}\nAddress = 10.77.0.2/32\nTable = off\n"
                            f"[Peer]\nPublicKey = {server_public}\nEndpoint = 192.0.2.1:51820\n"
                            "AllowedIPs = 10.77.0.1/32\nPersistentKeepalive = 25\n")
            conf.chmod(0o600)
            ppp.open_tunnel(value, path=str(conf))
        # Confirm recovery from a previous owned runtime, including real JSON
        # protocol output and duplicate-free policy rule replacement.
        policies = ppp.check_policy_ownership(ppp.run_network)
        assert len(policies) == 2
        servers.append(subprocess.Popen(ns_command(server, [sys.executable, "-c", HTTP_SERVER, "8080"])))
        servers.append(subprocess.Popen(ns_command(hub, [sys.executable, "-c", HTTP_SERVER, "8080"])))
        servers.append(subprocess.Popen([sys.executable, "-c", HTTP_SERVER, "80"]))
        servers.append(subprocess.Popen(ns_command(server, [sys.executable, "-c", UDP_SERVER])))
        udp_client = subprocess.Popen(ns_command(hub, [sys.executable, "-u", "-c", UDP_CLIENT]),
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        servers.append(udp_client)
        def udp_probe():
            udp_client.stdin.write("probe\n")
            udp_client.stdin.flush()
            return udp_client.stdout.readline().strip()
        time.sleep(.15)
        assert http(hub, "10.77.0.1") == "10.77.0.2", "hub must NAT to WireGuard address"
        assert udp_probe() == "10.77.0.2", "establish a bidirectional conntrack flow"
        assert http(internet, "192.0.2.1") == "192.0.2.2", "Internet must NAT to uplink address"
        assert http(hub, "172.16.77.1", port=80) == "172.16.77.2"
        assert http(os.getpid(), "10.77.0.1", source="10.77.0.2") == "10.77.0.2", "source-bound health probe"
        http(hub, "192.0.2.1", succeeds=False)
        http(hub, "192.0.2.2", port=80, succeeds=False)
        http(internet, "10.77.0.1", succeeds=False)
        http(server, "172.16.77.2", succeeds=False)
        http(server, "10.77.0.2", port=80, succeeds=False)
        in_ns(hub, ["ip", "addr", "add", "172.16.0.2/32", "dev", "peer0"])
        http(hub, "10.77.0.1", source="172.16.0.2", succeeds=False)
        # IPv6 also cannot bypass the IPv4-only hub profile.
        run(["sysctl", "-qw", "net.ipv6.conf.all.forwarding=1"])
        run(["ip", "-6", "addr", "add", "fd77::1/64", "dev", "ppp-hub", "nodad"])
        run(["ip", "-6", "addr", "add", "fd78::2/64", "dev", "eth0", "nodad"])
        in_ns(hub, ["ip", "-6", "addr", "add", "fd77::2/64", "dev", "peer0", "nodad"])
        in_ns(server, ["ip", "-6", "addr", "add", "fd78::1/64", "dev", "peer2", "nodad"])
        in_ns(hub, ["ip", "-6", "route", "add", "default", "via", "fd77::1"])
        in_ns(server, ["ip", "-6", "route", "add", "default", "via", "fd78::2"])
        ipv6 = in_ns(hub, ["ping", "-6", "-c", "1", "-W", "1", "fd78::1"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        assert ipv6.returncode == 1, "hub IPv6 must not escape to the uplink"
        # The same server also listens via its LAN address. Removing the policy
        # rules would expose it via the default route without the egress guard.
        for priority in (ppp.HUB_RULE_PRIORITY, ppp.PROBE_RULE_PRIORITY):
            run(["ip", "rule", "del", "priority", priority, "table", ppp.ROUTE_TABLE,
                 "protocol", ppp.ROUTE_PROTOCOL])
        http(hub, "10.77.0.1", succeeds=False)
        assert udp_probe() == "BLOCKED", "even established flows must not fall back to LAN"
        ppp.close_tunnel()
        http(hub, "10.77.0.1", succeeds=False)
        assert udp_probe() == "BLOCKED", "established flows stay blocked after tunnel loss"
        assert http(internet, "192.0.2.1") == "192.0.2.2", "Internet survives tunnel teardown"
        print("PASS: real WireGuard/NAT; private HTTP and Pi config; Internet; source-bound probe; LAN, IPv6, wrong-source, inbound to PPP/Pi, policy-loss and tunnel-loss isolation including established flows; atomic legacy migration/reload preserving unrelated tables")
        return 0
    finally:
        for child in [*servers, *keepers]:
            child.terminate()
        for child in [*servers, *keepers]:
            try:
                child.wait(timeout=3)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
