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

    def install(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.root, self.binary, "rev-c-ethernet", **kwargs)

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
        current = self.root / "opt/dialback-zero/current"
        first_release = os.readlink(current)
        backups = list(self.root.glob("var/lib/dialback-zero/backups/*"))
        self.install()
        self.assertEqual(os.readlink(current), first_release)
        self.assertEqual(list(self.root.glob("var/lib/dialback-zero/backups/*")), backups)
        self.assertEqual(len(list((self.root / "opt/dialback-zero/releases").iterdir())), 1)
        for relative, wanted in installer.PUBLIC_LINKS.items():
            self.assertEqual(os.readlink(self.root / relative), wanted)
            self.assertFalse(Path(wanted).is_absolute())
        manifest = installer.release.validate_release(current.resolve())
        self.assertEqual(manifest["version"], "dev")
        self.assertEqual(manifest["source_commit"], "unknown")
        self.assertTrue((self.root / "usr/local/lib/dialback-zero/prepare-ethernet.py").is_file())
        self.assertEqual(json.loads(config_path.read_text())["audio"]["volume_percent"], 19)
        link = self.root / "etc/systemd/system/multi-user.target.wants/dialback-zero.target"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "../dialback-zero.target")
        self.assertFalse((self.root / "etc/systemd/system/multi-user.target.wants/dialback-zero-modem.service").exists())

    def test_new_release_preserves_existing_release_and_all_appliance_settings(self):
        self.install(version="v1.0.0", source_commit="a" * 40)
        current = self.root / "opt/dialback-zero/current"
        previous = current.resolve()
        original_manifest = (previous / "release.json").read_bytes()
        recovery = self.root / "usr/local/lib/dialback-zero-recovery.py"
        recovery_bytes = recovery.read_bytes()
        self.assertFalse(recovery.is_symlink())
        settings = {
            "etc/dialback-zero/config.json": b'{"my_config":true}\n',
            "var/lib/dialback-zero/known-state.json": b"persistent state\n",
            "etc/NetworkManager/system-connections/private.nmconnection": b"secret wifi\n",
            "etc/ppp/chap-secrets": b"ppp secret\n",
            "etc/wireguard/hub.conf": b"private wireguard key\n",
            "etc/hostname": b"my-dialback\n",
        }
        for name, data in settings.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
        self.binary.write_bytes(b"updated native fixture")
        self.install(version="v1.1.0", source_commit="b" * 40)
        self.assertNotEqual(current.resolve(), previous)
        self.assertEqual((previous / "release.json").read_bytes(), original_manifest)
        self.assertEqual((previous / "bin/dialback-zero-modem").read_bytes(), b"native fixture")
        manifest = installer.release.validate_release(current.resolve())
        self.assertEqual(manifest["version"], "v1.1.0")
        self.assertEqual(manifest["source_commit"], "b" * 40)
        self.assertEqual(recovery.read_bytes(), recovery_bytes)
        for name, data in settings.items():
            self.assertEqual((self.root / name).read_bytes(), data)
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600)

    def test_migrates_physical_installation_with_complete_backups(self):
        originals = {
            "usr/local/lib/dialback-zero/launcher.py": b"old launcher\n",
            "usr/local/lib/dialback-zero/obsolete.py": b"obsolete but preserved\n",
            "usr/local/bin/dialback-zero-modem": b"old native modem\n",
            "usr/share/dialback-zero/sounds/dial-up.wav": b"old sound\n",
        }
        for name, data in originals.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        old_unit = self.root / "etc/systemd/system/dialback-zero-modem.service"
        old_unit.write_bytes(b"old modem service\n")
        old_recovery = self.root / "usr/local/lib/dialback-zero-recovery.py"
        old_recovery.write_bytes(b"old platform recovery\n")
        self.install()
        manifests = list(self.root.glob("var/lib/dialback-zero/backups/*/manifest.json"))
        app_backup = next(path.parent for path in manifests if "-application" in path.parent.name)
        for name, data in originals.items():
            self.assertEqual((app_backup / name).read_bytes(), data)
        self.assertFalse((self.root / "usr/local/lib/dialback-zero/obsolete.py").exists())
        unit_backups = list(self.root.glob("var/lib/dialback-zero/backups/*/etc/systemd/system/dialback-zero-modem.service"))
        self.assertEqual(len(unit_backups), 1)
        self.assertEqual(unit_backups[0].read_bytes(), b"old modem service\n")
        recovery_backups = list(self.root.glob("var/lib/dialback-zero/backups/*/usr/local/lib/dialback-zero-recovery.py"))
        self.assertEqual(len(recovery_backups), 1)
        self.assertEqual(recovery_backups[0].read_bytes(), b"old platform recovery\n")

    def test_dry_run_and_invalid_version_do_not_modify_target(self):
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        self.install(dry_run=True)
        with self.assertRaisesRegex(ValueError, "version"):
            self.install(version="../../escaped")
        after = {str(path.relative_to(self.root)): path.read_bytes()
                 for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertFalse((self.root / "opt").exists())

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

    def test_preflight_rejects_dangling_parent_escape(self):
        (self.root / "usr/local").symlink_to(self.root.parent / "missing-dialback-destination")
        before = (self.root / "boot/firmware/config.txt").read_bytes()
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.install()
        self.assertEqual((self.root / "boot/firmware/config.txt").read_bytes(), before)

    def test_preflight_refuses_unknown_public_alias_and_corrupt_current(self):
        self.install()
        path = self.root / "usr/local/bin/dialback-zero-modem"
        path.unlink()
        path.symlink_to("/bin/true")
        with self.assertRaisesRegex(ValueError, "unknown application symlink"):
            self.install()
        path.unlink()
        path.symlink_to(installer.PUBLIC_LINKS["usr/local/bin/dialback-zero-modem"])
        (self.root / "opt/dialback-zero/current/runtime/launcher.py").write_text("tampered\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.install()

    def test_cli_accepts_release_metadata_and_rejects_binary_symlink(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer.main(["--root", str(self.root), "--binary", str(self.binary),
                "--hardware", "rev-c-ethernet", "--version", "v2.3.4", "--source-commit", "c" * 40]), 0)
        manifest = json.loads((self.root / "opt/dialback-zero/current/release.json").read_text())
        self.assertEqual(manifest["version"], "v2.3.4")
        self.assertEqual(manifest["source_commit"], "c" * 40)
        alias = self.root / "tmp/modem-link"
        alias.symlink_to(self.binary)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            installer.main(["--root", str(self.root), "--binary", str(alias), "--hardware", "rev-c-ethernet"])

    def test_root_cli_is_required_and_live_root_needs_extra_flag(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                installer.main(["--binary", str(self.binary), "--hardware", "rev-c-ethernet"])
            with self.assertRaises(SystemExit):
                installer.main(["--root", "/", "--binary", str(self.binary),
                                "--hardware", "rev-c-ethernet"])


if __name__ == "__main__":
    unittest.main()
