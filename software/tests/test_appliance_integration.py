"""Exercise the real native/Python boundary and old-browser wire protocol."""
import copy
import json
from pathlib import Path
import shutil
import socket
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
import config_web
import dialback_config
from test_modem_integration import ModemProcess, TCPSER


class ApplianceIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(TCPSER.exists(), "build the native modem first")
    def test_real_serial_menu_stages_settings_and_returns_uart_to_modem(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            state = root / "state"
            dialback_config._atomic_write(config, copy.deepcopy(dialback_config.DEFAULT_CONFIG))
            runtime = Path(__file__).resolve().parents[1] / "runtime"
            library = root / "runtime"
            shutil.copytree(runtime, library, ignore=shutil.ignore_patterns("__pycache__"))
            menu = library / "config-menu"
            shutil.copyfile(library / "config_menu.py", menu)
            menu.chmod(0o755)
            modem = ModemProcess(self, extra_env={
                "DIALBACK_CONFIG_MENU": str(menu),
                "DIALBACK_CONFIG": str(config),
                "DIALBACK_STATE_DIR": str(state),
            })
            try:
                modem.command(b"AT")
                modem.clear_serial()
                modem.write(b"AT$CONFIG\r")
                modem.read_until(b"> ")
                modem.clear_serial()
                modem.write(b"7\r")
                modem.read_until(b"input hidden): ")
                modem.clear_serial()
                modem.write(b" local-secret-test \r")
                output = modem.read_until(b"> ")
                self.assertNotIn(b"local-secret-test", output)
                modem.clear_serial()
                modem.write(b"3\r")
                modem.read_until(b"Volume 0-100: ")
                modem.clear_serial()
                modem.write(b"100\r")
                modem.read_until(b"> ")
                modem.clear_serial()
                modem.write(b"s\r")
                modem.read_until(b"\r\nOK\r\n")
                staged = json.loads((state / "pending-config.json").read_text())
                self.assertEqual(staged["audio"]["volume_percent"], 100)
                self.assertEqual(staged["wifi"]["password"], " local-secret-test ")
                self.assertEqual(dialback_config.load(config)["audio"]["volume_percent"], 70)
                modem.command(b"AT")
            finally:
                modem.close()

    def test_http_10_client_without_host_can_load_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            dialback_config._atomic_write(config, copy.deepcopy(dialback_config.DEFAULT_CONFIG))
            server = config_web.ConfigServer(("127.0.0.1", 0), config_path=config,
                                             state_dir=root / "state")
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.create_connection(server.server_address, timeout=3) as client:
                    client.sendall(b"GET / HTTP/1.0\r\n\r\n")
                    chunks = []
                    while chunk := client.recv(4096):
                        chunks.append(chunk)
                response = b"".join(chunks)
                headers, page = response.split(b"\r\n\r\n", 1)
                self.assertTrue(headers.startswith(b"HTTP/1.0 200"))
                self.assertIn(b"HTML 3.2", page)
                self.assertIn(b'Dialback Zero', page)
                self.assertNotIn(b"<script", page.lower())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(3)


if __name__ == "__main__":
    unittest.main()
