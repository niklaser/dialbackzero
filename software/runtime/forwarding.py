#!/usr/bin/env python3
"""Install the Internet and private hub PPP firewall in one nft transaction."""

import argparse
import ipaddress
import subprocess
from dialback_config import load


def nft_rules(config):
    # Canonicalize values even when called independently of config.load().
    peer = str(ipaddress.IPv4Address(config["ppp"]["peer"]))
    hub_peer = str(ipaddress.IPv4Address(config["hub"]["peer"]))
    hub_local = str(ipaddress.IPv4Address(config["hub"]["local"]))
    server = str(ipaddress.IPv4Address(config["hub"]["server"]))
    port = int(config["web"]["port"])
    if not 1 <= port <= 65535:
        raise ValueError("invalid configuration web port")
    # 'add' is idempotent. Delete and recreate only our table in one transaction
    # so upgrades may change base-chain declarations (flush keeps those). A
    # failed transaction leaves the previous rules intact, with no open gap.
    return f"""add table inet dialback_zero
delete table inet dialback_zero
table inet dialback_zero {{
 chain input {{ type filter hook input priority -10; policy accept;
   iifname "wg-dbz" ct state {{ established, related }} accept
   iifname "wg-dbz" drop
   iifname "ppp-inet" meta nfproto != ipv4 drop
   iifname "ppp-inet" ip saddr != {peer} drop
   iifname "ppp-hub" meta nfproto != ipv4 drop
   iifname "ppp-hub" ip saddr != {hub_peer} drop
   iifname "ppp-hub" ip daddr {hub_local} tcp dport {port} accept
   iifname "ppp-hub" ip daddr {hub_local} icmp type echo-request accept
   iifname "ppp-hub" drop
 }}
 chain forward {{ type filter hook forward priority -10; policy accept;
   iifname "ppp-hub" meta nfproto != ipv4 drop
   iifname "ppp-hub" ip saddr != {hub_peer} drop
   oifname "ppp-hub" meta nfproto != ipv4 drop
   oifname "ppp-hub" ip daddr != {hub_peer} drop
   iifname "ppp-inet" meta nfproto != ipv4 drop
   iifname "ppp-inet" ip saddr != {peer} drop
   oifname "ppp-inet" meta nfproto != ipv4 drop
   oifname "ppp-inet" ip daddr != {peer} drop

   iifname "ppp-hub" oifname "wg-dbz" ip daddr {server} ct state {{ new, established, related }} accept
   iifname "ppp-hub" drop
   oifname "ppp-hub" iifname "wg-dbz" ip saddr {server} ct state {{ established, related }} accept
   oifname "ppp-hub" drop

   iifname "ppp-inet" ip daddr {server} drop
   iifname "ppp-inet" oifname "wg-dbz" drop
   oifname "ppp-inet" iifname "wg-dbz" drop
   iifname "ppp-inet" oifname "ppp*" drop
   iifname "ppp-inet" ct state {{ new, established, related }} accept
   oifname "ppp-inet" ct state {{ established, related }} accept
   oifname "ppp-inet" drop
   iifname "ppp*" drop
   oifname "ppp*" drop
 }}
 chain postrouting {{ type nat hook postrouting priority srcnat; policy accept;
   iifname "ppp-hub" ip saddr {hub_peer}/32 oifname "wg-dbz" ip daddr {server}/32 masquerade
   iifname "ppp-inet" ip saddr {peer}/32 oifname != "wg-dbz" oifname != "ppp*" masquerade
 }}
}}
"""


def run(command, **kwargs):
    kwargs.setdefault("check", True)
    return subprocess.run(command, timeout=10, **kwargs)


def apply(runner=run):
    # Install restrictions before turning on forwarding. On reload, nft makes
    # the update atomic even while another PPP session is active.
    runner(["nft", "-f", "-"], input=nft_rules(load()), text=True)
    runner(["sysctl", "-w", "net.ipv4.ip_forward=1"], stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("apply",))
    parser.parse_args()
    apply()


if __name__ == "__main__":
    main()
