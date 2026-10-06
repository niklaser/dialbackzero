import base64
import copy
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))
import dialback_config
import forwarding
import ppp_wrapper as ppp


def config():
    value = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
    value["hub"] = {"local": "172.16.77.1", "peer": "172.16.77.2",
                    "server": "10.77.0.1", "dns": "10.77.0.1"}
    return value


def tunnel_text():
    return ("[Interface]\nPrivateKey = " + base64.b64encode(b'a' * 32).decode() +
            "\nAddress = 10.77.0.2/32\nTable = off\n[Peer]\nPublicKey = " +
            base64.b64encode(b'b' * 32).decode() +
            "\nEndpoint = 192.168.1.10:51820\nAllowedIPs = 10.77.0.1/32\nPersistentKeepalive = 25\n")


class NetworkRunner:
    def __init__(self, links=None, rules=None, routes=None):
        self.links, self.rules, self.routes = links or [], rules or [], routes or []
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        output = ""
        if "-j" in command:
            output = json.dumps(self.links if "link" in command else self.rules if "rule" in command else self.routes)
        return subprocess.CompletedProcess(command, 0, output, "")


class HubRoutingTests(unittest.TestCase):
    def test_profiles_have_distinct_interfaces_addresses_and_dns(self):
        internet = ppp.pppd_command(config(), "internet")
        hub = ppp.pppd_command(config(), "hub")
        self.assertIn("172.16.0.1:172.16.0.2", internet)
        self.assertIn("172.16.77.1:172.16.77.2", hub)
        for command, interface, dns in ((internet, "ppp-inet", "8.8.4.4"), (hub, "ppp-hub", "10.77.0.1")):
            self.assertEqual(command[command.index("ifname") + 1], interface)
            self.assertEqual(command[command.index("ms-dns") + 1], dns)
            self.assertEqual(command[command.index("mtu") + 1], "1280")
            self.assertEqual(command[command.index("mru") + 1], "1280")
            self.assertIn("nodefaultroute", command)
            self.assertIn("noipv6", command)
            self.assertNotIn("ipcp-accept-remote", command)
            self.assertNotIn("ipcp-accept-local", command)

    def test_tunnel_parser_rejects_hooks_defaults_broad_routes_and_address_reuse(self):
        good = tunnel_text()
        parsed = ppp.parse_tunnel_config(good, config())
        self.assertEqual(parsed["client"], "10.77.0.2")
        self.assertNotIn("Address", parsed["wg_config"])
        self.assertNotIn("Table", parsed["wg_config"])
        bad = [good + "PostUp = evil\n", good.replace("Table = off", "Table = auto"),
               good.replace("10.77.0.1/32", "0.0.0.0/0"),
               good.replace("10.77.0.2/32", "172.16.77.2/32"),
               good.replace("10.77.0.2/32", "10.77.0.2/24"),
               good.replace("10.77.0.1/32", "10.77.0.3/32"),
               good + "[DEFAULT]\nPostUp = evil\n", good + "[Peer]\nPublicKey = other\n",
               good.replace("192.168.1.10:51820", "$(evil):51820")]
        for text in bad:
            with self.subTest(text=text[-50:]), self.assertRaises(ValueError):
                ppp.parse_tunnel_config(text, config())

    def test_unknown_interface_and_policy_collisions_never_mutated(self):
        tunnel = ppp.parse_tunnel_config(tunnel_text(), config())
        cases = [NetworkRunner(links=[{"ifname": "wg-dbz", "linkinfo": {"info_kind": "wireguard"}}]),
                 NetworkRunner(rules=[{"priority": 10077, "table": 123}]),
                 NetworkRunner(routes=[{"dst": "default", "dev": "eth0", "protocol": "static"}])]
        for runner in cases:
            with self.subTest(runner=runner), mock.patch.object(ppp, "read_tunnel_config", return_value=tunnel):
                with self.assertRaises(ValueError):
                    ppp.open_tunnel(config(), runner=runner)
                self.assertTrue(all("-j" in command for command, _ in runner.calls))

    def test_owned_interface_recreated_and_host_only_policy_has_terminal_route(self):
        tunnel = ppp.parse_tunnel_config(tunnel_text(), config())
        runner = NetworkRunner(links=[{"ifname": "wg-dbz", "ifalias": ppp.INTERFACE_ALIAS,
                                      "linkinfo": {"info_kind": "wireguard"}}])
        with mock.patch.object(ppp, "read_tunnel_config", return_value=tunnel):
            self.assertTrue(ppp.open_tunnel(config(), runner=runner))
        commands = [command for command, _ in runner.calls]
        self.assertIn(["ip", "link", "delete", "dev", "wg-dbz"], commands)
        self.assertIn(["wg", "setconf", "wg-dbz", "/dev/stdin"], commands)
        self.assertTrue(any("unreachable" in c and "51877" in c for c in commands))
        self.assertTrue(any("10077" in c and "ppp-hub" in c for c in commands))
        self.assertTrue(any("10078" in c and "10.77.0.2/32" in c for c in commands))
        self.assertFalse(any("wg-quick" in c for c in commands))
        self.assertFalse(any("route" in c and "default" in c and "unreachable" not in c for c in commands))

    def test_failed_setup_deletes_only_created_interface(self):
        tunnel = ppp.parse_tunnel_config(tunnel_text(), config())
        runner = NetworkRunner()
        def failing(command, **kwargs):
            result = runner(command, **kwargs)
            if command[0] == "wg":
                raise subprocess.CalledProcessError(1, command)
            return result
        with mock.patch.object(ppp, "read_tunnel_config", return_value=tunnel):
            with self.assertRaises(subprocess.CalledProcessError):
                ppp.open_tunnel(config(), runner=failing)
        self.assertEqual(runner.calls[-1][0], ["ip", "link", "delete", "dev", "wg-dbz"])

    def test_firewall_transaction_precedes_forwarding_and_source_guards_precede_accepts(self):
        runner = mock.Mock()
        with mock.patch.object(forwarding, "load", return_value=config()):
            forwarding.apply(runner)
        self.assertEqual(runner.call_args_list[0].args[0], ["nft", "-f", "-"])
        rules = runner.call_args_list[0].kwargs["input"]
        self.assertTrue(rules.startswith("add table inet dialback_zero\ndelete table inet dialback_zero\n"))
        self.assertEqual(rules.count("delete table "), 1)
        self.assertNotIn("flush ruleset", rules)
        self.assertIn('iifname "wg-dbz" ct state { established, related } accept\n   iifname "wg-dbz" drop', rules)
        self.assertNotIn('iifname "ppp*" accept', rules)
        self.assertLess(rules.index('iifname "ppp-hub" ip saddr !='),
                        rules.index('iifname "ppp-hub" oifname "wg-dbz"'))
        self.assertIn('iifname "ppp-inet" oifname "wg-dbz" drop', rules)
        self.assertIn('oifname "ppp-hub" iifname "wg-dbz" ip saddr 10.77.0.1 ct state { established, related } accept', rules)
        self.assertEqual(runner.call_args_list[1].args[0][0], "sysctl")

    def test_failed_firewall_does_not_enable_forwarding(self):
        runner = mock.Mock(side_effect=subprocess.CalledProcessError(1, ["nft"]))
        with mock.patch.object(forwarding, "load", return_value=config()), self.assertRaises(subprocess.CalledProcessError):
            forwarding.apply(runner)
        self.assertEqual(runner.call_count, 1)

    def test_ppp_units_require_firewall_and_stop_never_deletes_guard(self):
        systemd = RUNTIME.parent / "systemd"
        for profile in ("internet", "hub"):
            text = (systemd / f"dialback-zero-ppp-{profile}.service").read_text()
            self.assertIn("BindsTo=dialback-zero-forwarding.service", text)
            self.assertIn("After=dialback-zero-activate.service dialback-zero-forwarding.service", text)
        self.assertNotIn("ExecStop=", (systemd / "dialback-zero-forwarding.service").read_text())


if __name__ == "__main__":
    unittest.main()
