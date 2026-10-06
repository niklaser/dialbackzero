import base64
import copy
import json
from pathlib import Path
import socket
import stat
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import dialback_config
import hub_setup
import hub_status
import ppp_wrapper
import config_web


def profile():
    return {"version": 1, "endpoint": "192.168.1.59:51820",
            "server_public_key": base64.b64encode(b"s" * 32).decode(),
            "private_key": base64.b64encode(b"p" * 32).decode(),
            "address": "10.77.0.2/32", "server": "10.77.0.1", "dns": "10.77.0.1"}


class HubSetupTests(unittest.TestCase):
    def test_old_config_gains_disabled_hub_without_losing_settings(self):
        old = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        del old["hub"]
        old["audio"]["volume_percent"] = 44
        upgraded = dialback_config.validate(old)
        self.assertNotIn("hub", old)
        self.assertEqual(upgraded["audio"]["volume_percent"], 44)
        self.assertEqual(upgraded["hub"]["dns"], "10.77.0.1")
        self.assertFalse(upgraded["modem"]["numbers"]["777"]["enabled"])

    def test_import_preserves_pending_settings_and_does_not_activate_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active = root / "etc/dialback-zero/config.json"
            state = root / "var/lib/dialback-zero"
            current = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
            dialback_config._atomic_write(active, current)
            pending = copy.deepcopy(current)
            pending["audio"]["volume_percent"] = 55
            pending["wifi"]["ssid"] = "Keep this network"
            dialback_config.stage(pending, active, state)
            destination = hub_setup.import_profile(profile(), root)
            rendered = destination.read_text()
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(ppp_wrapper.parse_tunnel_config(rendered, current)["server"], "10.77.0.1")
            staged = dialback_config.load(state / "pending-config.json")
            self.assertTrue(staged["modem"]["numbers"]["777"]["enabled"])
            self.assertEqual(staged["audio"]["volume_percent"], 55)
            self.assertEqual(staged["wifi"]["ssid"], "Keep this network")
            self.assertEqual(dialback_config.load(active), current)
            hub_setup.import_profile(profile(), root)  # same key is idempotent
            changed = profile()
            changed["private_key"] = base64.b64encode(b"x" * 32).decode()
            with self.assertRaises(ValueError):
                hub_setup.import_profile(changed, root)
            self.assertEqual(destination.read_text(), rendered)

    def test_rejects_keys_hooks_public_endpoints_and_broad_addresses(self):
        for change in ({"private_key": "invalid"}, {"version": True},
                       {"private_key": base64.b64encode(bytes(32)).decode()},
                       {"PostUp": "anything"}, {"address": "10.77.0.2/24"},
                       {"endpoint": "8.8.8.8:51820"}, {"endpoint": "192.168.1.59:0"},
                       {"endpoint": "192.168.1.59:51820\nPostUp=x"},
                       {"dns": "8.8.8.8"}, {"address": "10.77.0.1/32"}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                hub_setup.validate_profile({**profile(), **change})

    def test_rejects_symlink_destination_without_writing_external_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            outside = root / "outside"
            outside.mkdir()
            (root / "etc/wireguard").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                hub_setup.import_profile(profile(), root)
            self.assertEqual(list(outside.iterdir()), [])

    def test_config_rejects_cross_profile_addresses_and_nonlocal_hub_endpoint(self):
        for change in (lambda cfg: cfg["hub"].update(peer=cfg["ppp"]["peer"]),
                       lambda cfg: cfg["hub"].update(server="8.8.8.8"),
                       lambda cfg: cfg["hub"].update(dns="192.168.1.1"),
                       lambda cfg: cfg["modem"]["numbers"]["777"].update(host="other.example")):
            candidate = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
            change(candidate)
            with self.assertRaises(dialback_config.ConfigError):
                dialback_config.validate(candidate)

    def test_hub_peer_can_reach_config_but_lan_and_tunnel_peer_cannot(self):
        config = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        self.assertTrue(config_web.client_allowed("172.16.77.2", config))
        self.assertTrue(config_web.host_allowed("172.16.77.1", config))
        self.assertFalse(config_web.client_allowed("10.77.0.1", config))
        self.assertFalse(config_web.client_allowed("192.168.1.59", config))


class HubStatusTests(unittest.TestCase):
    def test_status_uses_only_public_handshake_data_and_never_calls_dump(self):
        config = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        runner = mock.Mock()
        self.assertEqual(hub_status.status(config, runner), "Disabled")
        runner.assert_not_called()
        config["modem"]["numbers"]["777"]["enabled"] = True
        runner.return_value = mock.Mock(returncode=0, stdout="publickey\t900\n")
        self.assertIn("100 seconds", hub_status.status(config, runner, now=1000))
        self.assertEqual(runner.call_args.args[0], ["wg", "show", "wg-dbz", "latest-handshakes"])
        runner.return_value.stdout = "publickey\t0\n"
        self.assertIn("first", hub_status.status(config, runner))

    def test_dns_probe_checks_id_question_record_and_rcode(self):
        query = hub_status.dns_query(23)
        answer = (struct.pack("!6H", 23, 0x8400, 1, 1, 0, 0) + query[12:]
                  + b"\xc0\x0c" + struct.pack("!2HIH", 1, 1, 60, 4)
                  + socket.inet_aton("10.77.0.1"))
        self.assertTrue(hub_status.dns_answer_matches(answer, query, "10.77.0.1"))
        for bad in (answer[:20], b"\x00\x18" + answer[2:],
                    answer[:3] + b"\x03" + answer[4:], answer[:-1] + b"\x02"):
            self.assertFalse(hub_status.dns_answer_matches(bad, query, "10.77.0.1"))

    def test_probe_binds_each_socket_to_tunnel_and_times_out_without_fallback(self):
        config = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        config["modem"]["numbers"]["777"]["enabled"] = True
        runner = mock.Mock(return_value=mock.Mock(stdout=json.dumps([
            {"addr_info": [{"family": "inet", "local": "10.77.0.2"}]}])))
        connection = mock.MagicMock()
        connection.__enter__.return_value = connection
        connection.connect.side_effect = TimeoutError()
        factory = mock.Mock(return_value=connection)
        results = hub_status.probe(config, runner, factory)
        self.assertEqual(len(results), 2)
        self.assertTrue(all(not ok for _, ok, _ in results))
        self.assertEqual(connection.bind.call_args_list, [mock.call(("10.77.0.2", 0))] * 2)
        self.assertEqual(connection.setsockopt.call_args_list,
                         [mock.call(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"wg-dbz\0")] * 2)


if __name__ == "__main__":
    unittest.main()
