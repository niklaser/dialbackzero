#!/bin/sh
set -eu
umask 077

iptables-restore <<'RULES'
*filter
:INPUT DROP [0:0]
:FORWARD DROP [0:0]
:OUTPUT DROP [0:0]
-A INPUT -i lo -j ACCEPT
-A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A INPUT -i eth0 -p udp --dport 51820 -j ACCEPT
-A INPUT -i wg0 -s 10.77.0.2/32 -d 10.77.0.1/32 -p tcp -m multiport --dports 21,53,80,30000:30009 -j ACCEPT
-A INPUT -i wg0 -s 10.77.0.2/32 -d 10.77.0.1/32 -p udp --dport 53 -j ACCEPT
-A OUTPUT -o lo -j ACCEPT
-A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
-A OUTPUT -o eth0 -p udp --sport 51820 -j ACCEPT
COMMIT
RULES

: "${WG_PRIVATE_KEY:?WG_PRIVATE_KEY must be provisioned explicitly}"
: "${WG_PEER_PUBLIC_KEY:?WG_PEER_PUBLIC_KEY must be provisioned explicitly}"
mkdir -p /run/wireguard
printf '%s\n' "$WG_PRIVATE_KEY" > /run/wireguard/private.key
wg pubkey < /run/wireguard/private.key > /dev/null
unset WG_PRIVATE_KEY

ip link delete wg0 2>/dev/null || true
ip link add wg0 type wireguard
wg set wg0 private-key /run/wireguard/private.key listen-port 51820 \
    peer "$WG_PEER_PUBLIC_KEY" allowed-ips 10.77.0.2/32
rm /run/wireguard/private.key
ip address add 10.77.0.1/24 dev wg0
ip link set wg0 mtu 1420 up
printf '%s\n' 'Retro hub ready: WireGuard UDP 51820, private services 10.77.0.1.'
exec tail -f /dev/null
