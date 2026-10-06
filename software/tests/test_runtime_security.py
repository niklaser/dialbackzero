import copy
from contextlib import redirect_stderr
from array import array
from io import BytesIO, StringIO
import json
import math
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlencode
import wave

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))

import config_web
import dialback_config
import launcher
import network


class HandlerHarness(config_web.ConfigHandler):
    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        pass

    def end_headers(self):
        pass


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.config_path = root / "config.json"
        self.state_dir = root / "state"
        dialback_config._atomic_write(self.config_path, copy.deepcopy(dialback_config.DEFAULT_CONFIG))

    def tearDown(self):
        self.temporary.cleanup()

    def request(self, method, path, body=None, headers=None):
        headers = {"Host": "localhost", **(headers or {})}
        encoded = (body or "").encode("ascii")
        headers.setdefault("Content-Length", str(len(encoded)))
        handler = HandlerHarness.__new__(HandlerHarness)
        handler.server = SimpleNamespace(config_path=self.config_path, state_dir=self.state_dir,
                                         csrf="known-token")
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = headers
        handler.path = path
        handler.rfile = BytesIO(encoded)
        handler.wfile = BytesIO()
        getattr(handler, "do_" + method)()
        return handler.status, handler.wfile.getvalue().decode("ascii")

    def form(self, token="known-token"):
        return urlencode({"csrf": token, "sound_mode": "off", "volume": "100",
                          "baud": "38400", "host_2242525": "127.0.0.1", "port_2242525": "5432",
                          "host_777": "127.0.0.1", "port_777": "6543",
                          "number_2242525": "yes", "wifi_ssid": "Retro Net",
                          "wifi_password": "new private password"})

    def test_http_10_html_has_no_secret_or_script(self):
        current = dialback_config.load(self.config_path)
        current["wifi"]["password"] = "never-render-this"
        dialback_config._atomic_write(self.config_path, current)
        status, page = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("HTML 3.2", page)
        self.assertNotIn("never-render-this", page)
        self.assertNotIn("<SCRIPT", page.upper())
        self.assertIn("Volume (0-100)", page)
        self.assertIn('NAME="volume" SIZE="3" MAXLENGTH="3"', page)

    def test_host_and_csrf_are_enforced_and_save_is_staged(self):
        body = self.form()
        status, _ = self.request("POST", "/save", body,
                                 {"Host": "evil.example", "Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 403)
        status, _ = self.request("POST", "/save", self.form("wrong"),
                                 {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 403)
        status, page = self.request("POST", "/save", body,
                                    {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        self.assertIn("staged", page.lower())
        self.assertEqual(dialback_config.load(self.config_path)["audio"]["volume_percent"], 70)
        pending = dialback_config.load(self.state_dir / "pending-config.json")
        self.assertEqual(pending["audio"]["volume_percent"], 100)
        self.assertEqual(pending["wifi"]["password"], "new private password")

    def test_rejects_oversize_and_non_form_bodies(self):
        status, _ = self.request("POST", "/save", "x" * (config_web.MAX_BODY + 1),
                                 {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 413)
        status, _ = self.request("POST", "/save", "{}", {"Content-Type": "application/json"})
        self.assertEqual(status, 413)

    def test_network_apply_requires_csrf_and_calls_array_api(self):
        with mock.patch.object(config_web, "apply_wifi") as apply:
            status, _ = self.request("POST", "/apply-network", urlencode({"csrf": "known-token"}),
                                     {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 200)
        apply.assert_called_once()


class AccessAndNetworkTests(unittest.TestCase):
    def test_client_and_host_allow_only_loopback_or_configured_peer(self):
        value = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        self.assertTrue(config_web.client_allowed("127.0.0.1", value))
        self.assertTrue(config_web.client_allowed("172.16.0.2", value))
        self.assertFalse(config_web.client_allowed("192.168.1.20", value))
        self.assertTrue(config_web.host_allowed("172.16.0.1:80", value))
        self.assertFalse(config_web.host_allowed("192.168.1.4:80", value))
        self.assertTrue(config_web.host_allowed(None, value))
        self.assertFalse(config_web.host_allowed("localhost@evil", value))

    def test_wifi_password_is_stdin_only_and_command_is_an_array(self):
        value = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        value["wifi"].update(managed=True, enabled=True, ssid="Retro Net", password="highly private")
        runner = mock.Mock()
        runner.return_value.stdout = ""
        network.apply_wifi(value, runner=runner)
        for call in runner.call_args_list:
            self.assertIsInstance(call.args[0], list)
            self.assertNotIn("highly private", " ".join(call.args[0]))
        args, kwargs = runner.call_args
        self.assertIn("highly private", kwargs["input"])
        self.assertIs(kwargs["stdout"], __import__("subprocess").DEVNULL)

    def test_unmanaged_wifi_preserves_existing_image_profiles(self):
        value = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        runner = mock.Mock()
        self.assertIsNone(network.apply_wifi(value, runner=runner))
        runner.assert_not_called()

    def test_disabled_managed_wifi_only_touches_owned_profile_and_is_idempotent(self):
        value = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        value["wifi"]["managed"] = True
        runner = mock.Mock()
        runner.return_value.stdout = "user-home\n"
        self.assertIsNone(network.apply_wifi(value, runner=runner))
        self.assertEqual(runner.call_count, 1)
        runner.reset_mock()
        runner.return_value.stdout = network.PROFILE + "\nuser-home\n"
        network.apply_wifi(value, runner=runner)
        rendered = [call.args[0] for call in runner.call_args_list]
        self.assertTrue(all(network.PROFILE in args or args[-2:] == ["connection", "show"] for args in rendered))

    def test_launcher_only_renders_enabled_numbers_without_shell(self):
        value = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        args = launcher.modem_argv(value, "/native/modem")
        self.assertEqual(args[0], "/native/modem")
        self.assertIn("2242525=127.0.0.1:5432", args)
        self.assertNotIn("777=127.0.0.1:6543", args)
        value["modem"]["numbers"]["777"]["enabled"] = True
        self.assertIn("777=127.0.0.1:6543", launcher.modem_argv(value))

    def test_launcher_maps_amplitude_percent_to_decibels_and_mutes_zero(self):
        value = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        self.assertEqual(
            launcher.mixer_argv(value),
            ["amixer", "-q", "-c", "Headphones", "--", "sset", "PCM", "-0.10dB", "unmute"],
        )
        value["audio"]["volume_percent"] = 50
        self.assertEqual(launcher.mixer_argv(value)[-2:], ["-3.02dB", "unmute"])
        value["audio"]["volume_percent"] = 100
        self.assertEqual(launcher.mixer_argv(value)[-2:], ["3.00dB", "unmute"])
        value["audio"]["volume_percent"] = 0
        self.assertEqual(launcher.mixer_argv(value)[-2:], ["0.00dB", "mute"])

    def test_mixer_failure_is_reported_without_blocking_modem_startup(self):
        value = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        runner = mock.Mock(return_value=SimpleNamespace(returncode=1))
        warning = StringIO()
        with redirect_stderr(warning):
            self.assertFalse(launcher.apply_mixer(value, runner=runner))
        self.assertIn("continuing", warning.getvalue())
        self.assertEqual(runner.call_args.args[0], launcher.mixer_argv(value))

    def test_bundled_audio_has_peak_headroom_at_maximum_gain(self):
        gain = 10 ** (launcher.MAX_PCM_GAIN_DB / 20)
        for name in ("dial-up.wav", "busy-signal.wav"):
            with self.subTest(name=name), wave.open(str(RUNTIME.parent / "assets" / name), "rb") as stream:
                self.assertEqual((stream.getnchannels(), stream.getsampwidth()), (2, 2))
                samples = array("h", stream.readframes(stream.getnframes()))
            if sys.byteorder != "little":
                samples.byteswap()
            mono_peak = max(abs(samples[index] + samples[index + 1]) / 2
                            for index in range(0, len(samples), 2)) / 32768
            self.assertLess(mono_peak * gain, 1.0)


if __name__ == "__main__":
    unittest.main()
