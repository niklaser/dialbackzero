import importlib.util
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

SOFTWARE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dialback_offline_installer", SOFTWARE / "install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        boot = self.root / "boot/firmware"
        (boot / "overlays").mkdir(parents=True)
        for overlay in ("audremap", "gpio-shutdown", "gpio-poweroff", "disable-bt", "w5500"):
            (boot / "overlays" / (overlay + ".dtbo")).write_bytes(b"fixture")
        (boot / "config.txt").write_text("# user setting\n")
        (boot / "cmdline.txt").write_text("console=serial0,115200 root=fixture rw\n")
        (self.root / "etc/systemd/system").mkdir(parents=True)
        # Hardware detection accepts either lib or usr/lib path as a service file.
        nm = self.root / "usr/lib/systemd/system/NetworkManager.service"
        nm.parent.mkdir(parents=True)
        nm.write_text("[Unit]\n")
        self.binary = self.root / "tmp/dialback-zero-modem"
        self.binary.parent.mkdir()
        self.binary.write_bytes(b"native fixture")
        self.binary.chmod(0o755)

    def tearDown(self):
        self.temporary.cleanup()

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.root, self.binary, "rev-c-ethernet")

    def test_offline_install_is_idempotent_preserves_config_and_enables_target(self):
        self.install()
        installed = self.root / "usr/local/bin/dialback-zero-modem"
        self.assertEqual(installed.read_bytes(), b"native fixture")
        self.assertEqual(installed.stat().st_mode & 0o777, 0o755)
        config_path = self.root / "etc/dialback-zero/config.json"
        config = json.loads(config_path.read_text())
        self.assertTrue(config["modem"]["numbers"]["2242525"]["enabled"])
        self.assertFalse(config["modem"]["numbers"]["777"]["enabled"])
        config["audio"]["volume_percent"] = 19
        config_path.write_text(json.dumps(config))
        self.install()
        self.assertEqual(json.loads(config_path.read_text())["audio"]["volume_percent"], 19)
        link = self.root / "etc/systemd/system/multi-user.target.wants/dialback-zero.target"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../dialback-zero.target")
        self.assertFalse((self.root / "etc/systemd/system/multi-user.target.wants/dialback-zero-modem.service").exists())

    def test_preflight_rejects_symlink_escape_before_hardware_mutation(self):
        outside = Path(self.temporary.name + "-outside")
        outside.mkdir()
        self.addCleanup(outside.rmdir)
        (self.root / "usr").mkdir(exist_ok=True)
        (self.root / "usr/local").symlink_to(outside)
        before = (self.root / "boot/firmware/config.txt").read_text()
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.install()
        self.assertEqual((self.root / "boot/firmware/config.txt").read_text(), before)
        self.assertEqual(list(outside.iterdir()), [])

    def test_root_cli_is_required_and_live_root_needs_extra_flag(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                installer.main(["--binary", str(self.binary), "--hardware", "rev-c-ethernet"])
            with self.assertRaises(SystemExit):
                installer.main(["--root", "/", "--binary", str(self.binary),
                                "--hardware", "rev-c-ethernet"])


if __name__ == "__main__":
    unittest.main()
