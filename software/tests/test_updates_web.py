"""Exercise the local update controls without downloading or installing software."""
import copy
from io import BytesIO
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import config_web
import dialback_config
import update_manager


class HandlerHarness(config_web.ConfigHandler):
    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        self.response_headers[keyword] = value

    def end_headers(self):
        pass


class UpdateWebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_path = self.root / "config.json"
        dialback_config._atomic_write(self.config_path, copy.deepcopy(dialback_config.DEFAULT_CONFIG))
        self.state = {"installed_version": "1.0.0", "available_version": None,
                      "phase": "idle", "message": "Check for updates when connected to the Internet."}
        self.backend = SimpleNamespace(get_status=mock.Mock(side_effect=lambda: self.state.copy()),
                                       request_check=mock.Mock(), request_install=mock.Mock())

    def request(self, method, path, fields=None, address="127.0.0.1", host="localhost"):
        encoded = urlencode(fields if fields is not None else {"csrf": "known-token"}).encode("ascii")
        handler = HandlerHarness.__new__(HandlerHarness)
        handler.server = SimpleNamespace(config_path=self.config_path, state_dir=self.root / "state",
                                         csrf="known-token", update_backend=self.backend)
        handler.client_address = (address, 12345)
        handler.headers = {"Content-Length": str(len(encoded)),
                           "Content-Type": "application/x-www-form-urlencoded"}
        if host is not None:
            handler.headers["Host"] = host
        handler.path = path
        handler.rfile, handler.wfile = BytesIO(encoded), BytesIO()
        handler.response_headers = {}
        getattr(handler, "do_" + method)()
        return handler.status, handler.wfile.getvalue().decode("ascii"), handler.response_headers

    def test_settings_shows_versions_and_browser_compatible_controls(self):
        status, page, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("HTML 3.2", page)
        self.assertIn("Software updates", page)
        self.assertIn("Installed version: 1.0.0", page)
        self.assertIn("Latest version: Not known", page)
        self.assertIn('ACTION="/updates/check"', page)
        self.assertIn('NAME="csrf" VALUE="known-token"', page)
        self.assertNotIn('ACTION="/updates/install"', page)
        self.assertNotIn("<SCRIPT", page.upper())
        self.assertNotIn("HTTP-EQUIV", page.upper())
        self.assertNotIn(" DISABLED", page.upper())
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["Content-Type"], "text/html; charset=us-ascii")

    def test_available_update_has_install_warning_and_status_refresh_link(self):
        self.state.update(phase="available", available_version="1.1.0", message="A new version is available.")
        status, page, _ = self.request("GET", "/updates", host=None)
        self.assertEqual(status, 200)
        self.assertIn("Latest version: 1.1.0", page)
        self.assertIn('ACTION="/updates/install"', page)
        self.assertIn("disconnects the current call", page)
        self.assertIn("Reconnect afterward", page)
        self.assertIn('HREF="/updates">Refresh update status', page)
        self.backend.request_check.assert_not_called()
        self.backend.request_install.assert_not_called()

    def test_status_values_are_escaped_and_ascii(self):
        self.state.update(installed_version='<script>"bad"</script>',
                          message="Connection failed <retry> & try again: \N{SNOWMAN}")
        status, page, _ = self.request("GET", "/updates")
        self.assertEqual(status, 200)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("&lt;retry&gt; &amp;", page)
        self.assertIn("&#9731;", page)
        self.assertNotIn("<script>", page)

    def test_each_terminal_result_is_visible_after_reconnecting(self):
        for phase, label in (("succeeded", "Update complete"),
                             ("rolled_back", "Previous version restored"),
                             ("failed", "Update failed")):
            with self.subTest(phase=phase):
                self.state.update(phase=phase, message="Result detail.")
                status, page, _ = self.request("GET", "/updates")
                self.assertEqual(status, 200)
                self.assertIn(label, page)
                self.assertIn("Result detail.", page)
                self.assertIn('ACTION="/updates/check"', page)

    def test_check_and_install_enqueue_only_fixed_no_argument_requests(self):
        status, page, _ = self.request("POST", "/updates/check")
        self.assertEqual(status, 202)
        self.assertIn("background", page)
        self.backend.request_check.assert_called_once_with()
        self.backend.request_install.assert_not_called()
        self.state.update(phase="available", available_version="1.1.0")
        status, page, _ = self.request("POST", "/updates/install")
        self.assertEqual(status, 202)
        self.assertIn("will disconnect", page)
        self.backend.request_install.assert_called_once_with()

    def test_update_actions_reject_untrusted_sources_hosts_and_tokens(self):
        self.state.update(phase="available", available_version="1.1.0")
        for path in ("/updates/check", "/updates/install"):
            for kwargs in ({"address": "192.168.1.10"}, {"host": "evil.example"},
                           {"fields": {"csrf": "wrong"}}, {"fields": {}},
                           {"fields": [("csrf", "known-token"), ("csrf", "known-token")]}):
                with self.subTest(path=path, kwargs=kwargs):
                    status, _, _ = self.request("POST", path, **kwargs)
                    self.assertEqual(status, 403)
        self.backend.request_check.assert_not_called()
        self.backend.request_install.assert_not_called()

    def test_status_page_is_restricted_like_settings(self):
        for kwargs in ({"address": "192.168.1.10"}, {"host": "evil.example"}):
            with self.subTest(kwargs=kwargs):
                status, _, _ = self.request("GET", "/updates", **kwargs)
                self.assertEqual(status, 403)
        self.backend.get_status.assert_not_called()

    def test_update_requests_reject_custom_release_and_url_fields(self):
        self.state.update(phase="available", available_version="1.1.0")
        for path in ("/updates/check", "/updates/install"):
            for key in ("url", "version", "channel"):
                with self.subTest(path=path, key=key):
                    status, _, _ = self.request("POST", path,
                                                {"csrf": "known-token", key: "untrusted"})
                    self.assertEqual(status, 400)
        self.backend.request_check.assert_not_called()
        self.backend.request_install.assert_not_called()

    def test_install_requires_an_available_release_and_backend_acceptance(self):
        for phase in ("idle", "checking", "downloading", "installing", "verifying", "succeeded", "failed", "rolled_back"):
            with self.subTest(phase=phase):
                self.state.update(phase=phase, available_version="1.1.0")
                status, page, _ = self.request("POST", "/updates/install")
                self.assertEqual(status, 409)
                self.assertNotIn('ACTION="/updates/install"', page)
        self.state.update(phase="available", available_version=None)
        self.assertEqual(self.request("POST", "/updates/install")[0], 409)
        self.backend.request_install.assert_not_called()
        self.state.update(phase="available", available_version="1.1.0")
        for path, action in (("/updates/install", self.backend.request_install),
                             ("/updates/check", self.backend.request_check)):
            with self.subTest(path=path):
                action.side_effect = update_manager.UpdateError("Another request is active <wait>.")
                status, page, _ = self.request("POST", path)
                self.assertEqual(status, 409)
                self.assertIn("Another request is active &lt;wait&gt;.", page)

    def test_settings_and_wifi_writes_are_blocked_during_update(self):
        with mock.patch.object(config_web, "stage") as stage, mock.patch.object(config_web, "apply_wifi") as apply:
            for phase in ("downloading", "installing", "verifying"):
                with self.subTest(phase=phase):
                    self.state.update(phase=phase, available_version="1.1.0")
                    status, page, _ = self.request("GET", "/")
                    self.assertEqual(status, 200)
                    self.assertNotIn('ACTION="/save"', page)
                    self.assertNotIn('ACTION="/apply-network"', page)
                    self.assertIn("changes are paused", page)
                    for path in ("/save", "/apply-network"):
                        self.assertEqual(self.request("POST", path)[0], 409)
            stage.assert_not_called()
            apply.assert_not_called()

    def test_unreadable_update_state_blocks_writes_without_leaking_diagnostics(self):
        self.backend.get_status.side_effect = OSError("private filesystem diagnostic")
        with mock.patch.object(config_web, "apply_wifi") as apply:
            status, page, _ = self.request("POST", "/apply-network")
        self.assertEqual(status, 409)
        self.assertIn("Update status is unavailable", page)
        self.assertNotIn("private filesystem", page)
        apply.assert_not_called()

    def test_queue_storage_failure_returns_safe_error_page(self):
        self.state.update(phase="available", available_version="1.1.0")
        for path, action in (("/updates/install", self.backend.request_install),
                             ("/updates/check", self.backend.request_check)):
            with self.subTest(path=path):
                action.side_effect = OSError("private filesystem diagnostic")
                status, page, _ = self.request("POST", path)
                self.assertEqual(status, 503)
                self.assertIn("Could not save the update request", page)
                self.assertNotIn("private filesystem", page)

    def test_get_requests_cannot_start_check_or_install(self):
        self.state.update(phase="available", available_version="1.1.0")
        for path in ("/updates/check", "/updates/install"):
            with self.subTest(path=path):
                self.assertEqual(self.request("GET", path)[0], 404)
        self.backend.request_check.assert_not_called()
        self.backend.request_install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
