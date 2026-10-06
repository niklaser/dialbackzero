"""Rev C checks use mounted-image fixtures and NetworkManager's offline parser."""

import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("ethernet_carrier_install", "install.py")
ethernet = load("ethernet_prepare", "prepare-ethernet.py")


class EthernetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.boot = self.root / "boot/firmware"
        (self.boot / "overlays").mkdir(parents=True)
        (self.root / "etc/systemd/system").mkdir(parents=True)
        unit = self.root / "usr/lib/systemd/system/NetworkManager.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=/usr/sbin/NetworkManager --no-daemon\n")
        for name in ("audremap", "gpio-shutdown", "gpio-poweroff", "disable-bt", "w5500"):
            (self.boot / "overlays" / f"{name}.dtbo").write_bytes(b"fixture")
        (self.boot / "config.txt").write_text("# Image fixture\n")
        (self.boot / "cmdline.txt").write_text("console=tty1 root=PARTUUID=fixture rw\n")
        device_tree = self.root / "proc/device-tree"
        device_tree.mkdir(parents=True)
        (device_tree / "model").write_text("Raspberry Pi Zero W Rev 1.1\0")
        (device_tree / "serial-number").write_text("00000000abcdef12\0")
        self.wifi = self.root / "etc/NetworkManager/system-connections/existing-wifi.nmconnection"
        self.wifi.parent.mkdir(parents=True)
        self.wifi.write_text("# Existing Wi-Fi must stay byte-for-byte intact\n")

    def tearDown(self):
        self.temp.cleanup()

    def install(self, hardware="rev-c-ethernet", dry_run=False):
        plan, masks = installer.build_plan(self.root, hardware)
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.root, plan, masks, dry_run)

    def test_boot_identity_is_stable_private_unicast_and_different_between_pis(self):
        self.assertEqual(ethernet.mac_from_serial("00000000abcdef12\0"), "ba:55:73:eb:5f:e6")
        self.assertEqual(ethernet.mac_from_serial("00000000ABCDEF12"), "ba:55:73:eb:5f:e6")
        self.assertNotEqual(ethernet.mac_from_serial("00000000abcdef13"), "ba:55:73:eb:5f:e6")
        for value in ("", "0000000000000000", "../etc/passwd", "not-a-pi-serial"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ethernet.mac_from_serial(value)

    def test_install_and_first_boot_preserve_wifi_and_are_idempotent(self):
        wifi_before = self.wifi.read_bytes()
        self.install()
        config = (self.boot / "dialback-zero.txt").read_text()
        self.assertIn("dtoverlay=w5500,int_pin=4,cs=0,speed=10000000", config)
        self.assertIn("dtoverlay=gpio-poweroff,gpiopin=26,active_low=0", config)
        self.assertIn("dtoverlay=audremap,pins_12_13", config)
        self.assertIn("gpio=5,6,22,23,24,25,27=op,dl", config)
        self.assertFalse((self.root / ethernet.PROFILE).exists())
        ethernet.prepare(self.root)
        profile = self.root / ethernet.PROFILE
        before = profile.read_bytes(), profile.stat().st_mtime_ns
        self.install()
        ethernet.prepare(self.root)
        self.assertEqual((profile.read_bytes(), profile.stat().st_mtime_ns), before)
        self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.wifi.read_bytes(), wifi_before)
        self.assertEqual(len(list(self.root.glob("var/lib/dialback-zero/backups/*/manifest.json"))), 1)

    def test_moving_image_to_another_pi_preserves_user_network_changes(self):
        self.install()
        ethernet.prepare(self.root)
        profile = self.root / ethernet.PROFILE
        profile.write_text(profile.read_text().replace("method=auto", "method=manual", 1)
                           .replace("route-metric=100", "route-metric=150", 1)
                           .replace("[ipv4]\n", "[ipv4]\naddress1=192.0.2.40/24,192.0.2.1\ndns=192.0.2.1;\n"))
        (self.root / "proc/device-tree/serial-number").write_text("00000000abcdef13\0")
        mac = ethernet.prepare(self.root)
        updated = profile.read_text()
        self.assertIn("cloned-mac-address=" + mac, updated)
        self.assertNotIn("ba:55:73:eb:5f:e6", updated)
        for setting in ("method=manual", "route-metric=150", "address1=192.0.2.40/24,192.0.2.1", "dns=192.0.2.1;"):
            self.assertIn(setting, updated)

    def test_no_host_file_following_or_shared_mac_fallback(self):
        self.install()
        profile = self.root / ethernet.PROFILE
        profile.symlink_to("/tmp/never-write-host-profile.nmconnection")
        with self.assertRaisesRegex(ValueError, "symlink"):
            ethernet.prepare(self.root)
        profile.unlink()
        (self.root / "proc/device-tree/serial-number").write_text("0000000000000000\0")
        with self.assertRaises(ValueError):
            ethernet.prepare(self.root)
        self.assertFalse(profile.exists())

    def test_dry_run_writes_nothing_and_rev_b_has_no_ethernet(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.install(dry_run=True)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()})
        self.install(hardware="rev-b-ltc2954")
        self.assertNotIn("w5500", (self.boot / "dialback-zero.txt").read_text())
        self.assertFalse((self.root / "etc/systemd/system/dialback-zero-ethernet.service").exists())

    def test_rev_c_requires_driver_overlay_and_networkmanager(self):
        overlay = self.boot / "overlays/w5500.dtbo"
        overlay.unlink()
        with self.assertRaisesRegex(ValueError, "w5500.dtbo"):
            self.install()
        overlay.write_bytes(b"fixture")
        (self.root / "usr/lib/systemd/system/NetworkManager.service").unlink()
        with self.assertRaisesRegex(ValueError, "NetworkManager"):
            self.install()
        self.assertFalse((self.boot / "dialback-zero.txt").exists())

    def test_existing_spi0_and_irq_claims_are_rejected_before_changes(self):
        for setting in ("dtoverlay=w5500", "dtoverlay=spi0-1cs", "dtoverlay=w1-gpio", "gpio=4=op,dh", "gpio=8-11=op,dh"):
            with self.subTest(setting=setting):
                (self.boot / "config.txt").write_text(setting + "\n")
                with self.assertRaisesRegex(ValueError, "Conflicting"):
                    self.install()
                self.assertFalse((self.boot / "dialback-zero.txt").exists())

    @unittest.skipUnless(shutil.which("nmcli"), "NetworkManager offline parser unavailable")
    def test_real_networkmanager_keyfile_parser_without_daemon_or_network_access(self):
        self.install()
        ethernet.prepare(self.root)
        result = subprocess.run(
            ["nmcli", "--offline", "connection", "modify", "connection.id", "Dialback Zero Ethernet"],
            input=(self.root / ethernet.PROFILE).read_text(), text=True,
            capture_output=True, check=True, timeout=10,
        )
        self.assertIn("driver=w5100;", result.stdout)
        self.assertIn("cloned-mac-address=BA:55:73:EB:5F:E6", result.stdout)
        self.assertIn("route-metric=100", result.stdout)


if __name__ == "__main__":
    unittest.main()
