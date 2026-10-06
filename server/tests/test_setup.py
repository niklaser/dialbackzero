import importlib.util
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("retro_setup", ROOT / "setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


class EndpointTests(unittest.TestCase):
    def test_lan_endpoint(self):
        self.assertEqual(setup.parse_endpoint("192.168.1.50:51820"),
                         ("192.168.1.50", 51820))

    def test_rejects_unintended_hosts_or_invalid_ports(self):
        for endpoint in ("0.0.0.0:51820", "127.0.0.1:51820", "8.8.8.8:51820",
                         "10.77.0.1:51820", "example.org:51820", "192.168.1.50:0",
                         "192.168.1.50:65536", "192.168.1.50:5\n1820",
                         "192.168.1.50", "[::1]:51820"):
            with self.subTest(endpoint=endpoint), self.assertRaises(setup.argparse.ArgumentTypeError):
                setup.parse_endpoint(endpoint)


class LocalBundleTests(unittest.TestCase):
    def test_default_output_uses_private_work_directory(self):
        with patch.object(setup, "create_bundle", return_value=("server", "pi")) as create, \
                patch("sys.argv", ["setup.py", "--endpoint", "192.168.1.50:51820"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            setup.main()
        create.assert_called_once_with(
            "192.168.1.50:51820", setup.WORK_DIR / "private" / "retro-hub",
        )

    def test_nested_bundle_permissions_and_no_overwrite_without_key_generation(self):
        with tempfile.TemporaryDirectory() as parent, \
                patch.object(setup.shutil, "which", return_value="wg"), \
                patch.object(setup, "keypair", side_effect=[
                    ("test-server-private", "test-server-public"),
                    ("test-pi-private", "test-pi-public"),
                ]) as keys:
            output = Path(parent) / "private" / "retro-hub"
            setup.create_bundle("192.168.1.50:51820", output)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for name in ("server.env", "pi-hub.json"):
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)
            original = (output / "server.env").read_bytes()
            with self.assertRaises(FileExistsError):
                setup.create_bundle("192.168.1.50:51820", output)
            self.assertEqual((output / "server.env").read_bytes(), original)
            self.assertEqual(keys.call_count, 2)


@unittest.skipUnless(shutil.which("wg"), "wireguard-tools is required")
class BundleTests(unittest.TestCase):
    def test_distinct_keypairs_permissions_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as parent:
            output = Path(parent) / "bundle"
            server_public, pi_public = setup.create_bundle("192.168.1.50:51820", output)
            env = dict(line.split("=", 1) for line in
                       (output / "server.env").read_text().splitlines())
            profile = json.loads((output / "pi-hub.json").read_text())
            self.assertEqual(profile["server_public_key"], server_public)
            self.assertEqual(env["WG_PEER_PUBLIC_KEY"], pi_public)
            self.assertNotEqual(env["WG_PRIVATE_KEY"], profile["private_key"])
            for private, expected in ((env["WG_PRIVATE_KEY"], server_public),
                                      (profile["private_key"], pi_public)):
                actual = subprocess.run(["wg", "pubkey"], input=private + "\n",
                                        text=True, capture_output=True, check=True).stdout.strip()
                self.assertEqual(actual, expected)
            self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
            for name in ("server.env", "pi-hub.json"):
                self.assertEqual(stat.S_IMODE((output / name).stat().st_mode), 0o600)
            original = (output / "server.env").read_bytes()
            with self.assertRaises(FileExistsError):
                setup.create_bundle("192.168.1.50:51820", output)
            self.assertEqual((output / "server.env").read_bytes(), original)

    def test_cli_does_not_print_private_keys(self):
        with tempfile.TemporaryDirectory() as parent:
            output = Path(parent) / "bundle"
            result = subprocess.run(
                ["python3", str(ROOT / "setup.py"), "--endpoint", "192.168.1.50:51820",
                 "--output", str(output)], text=True, capture_output=True, check=True,
            )
            env = dict(line.split("=", 1) for line in
                       (output / "server.env").read_text().splitlines())
            profile = json.loads((output / "pi-hub.json").read_text())
            self.assertNotIn(env["WG_PRIVATE_KEY"], result.stdout + result.stderr)
            self.assertNotIn(profile["private_key"], result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
