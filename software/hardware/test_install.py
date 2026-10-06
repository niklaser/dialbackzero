"""Tests use temporary image trees and an ALSA file/null sink, never real GPIO/audio."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest
import wave

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("carrier_install", HERE / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.boot = self.root / "boot/firmware"
        (self.boot / "overlays").mkdir(parents=True)
        (self.root / "etc/systemd/system").mkdir(parents=True)
        for overlay in ("audremap", "gpio-shutdown", "gpio-poweroff", "disable-bt"):
            (self.boot / "overlays" / f"{overlay}.dtbo").write_bytes(b"fixture")
        (self.boot / "config.txt").write_text("# Preserve user settings\n[pi4]\narm_boost=1\n")
        (self.boot / "cmdline.txt").write_text("console=serial0,115200 console=tty1 root=PARTUUID=sample rw quiet\n")
        (self.root / "etc/asound.conf").write_text("# previous ALSA configuration\n")

    def tearDown(self):
        self.temp.cleanup()

    def run_install(self, dry_run=False):
        plan, masks = installer.build_plan(self.root)
        with contextlib.redirect_stdout(io.StringIO()):
            installer.install(self.root, plan, masks, dry_run)

    def test_dry_run_does_not_change_image(self):
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.run_install(dry_run=True)
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_install_is_idempotent_and_backs_up_existing_settings(self):
        self.run_install()
        config = (self.boot / "config.txt").read_text()
        self.assertIn("arm_boost=1", config)
        self.assertIn("[all]\ninclude dialback-zero.txt", config)
        self.assertEqual(config.count(installer.START), 1)
        self.assertEqual((self.boot / "cmdline.txt").read_text(), "console=tty1 root=PARTUUID=sample rw quiet\n")
        for unit in installer.MASKS:
            self.assertEqual(os.readlink(self.root / "etc/systemd/system" / unit), "/dev/null")
        manifests = list(self.root.glob("var/lib/dialback-zero/backups/*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        self.assertEqual((manifests[0].parent / "etc/asound.conf").read_text(), "# previous ALSA configuration\n")
        self.assertTrue(json.loads(manifests[0].read_text()))
        self.run_install()
        self.assertEqual(len(list(self.root.glob("var/lib/dialback-zero/backups/*/manifest.json"))), 1)

    def test_rejects_conflicting_overlay_in_included_file_before_any_write(self):
        (self.boot / "config.txt").write_text("include old-audio.txt\n")
        (self.boot / "old-audio.txt").write_text("dtoverlay=audremap,pins_18_19\n")
        with self.assertRaisesRegex(ValueError, "Conflicting overlay"):
            self.run_install()
        self.assertFalse((self.root / "var").exists())
        self.assertFalse((self.boot / "dialback-zero.txt").exists())

    def test_offline_image_cannot_replace_host_file_through_symlink(self):
        target = self.root / "etc/asound.conf"
        target.unlink()
        target.symlink_to("/etc/asound.conf")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.run_install()

    def test_rejects_boot_gpio_that_could_assert_kill_early(self):
        (self.boot / "config.txt").write_text("gpio=22-27=op,dh\n")
        with self.assertRaisesRegex(ValueError, "Conflicting power GPIO"):
            self.run_install()
        self.assertFalse((self.boot / "dialback-zero.txt").exists())

    def test_power_gpio_polarity_and_short_controller_pulse(self):
        settings = (HERE / "config.txt.example").read_text().splitlines()
        self.assertIn("gpio=26=op,dl", settings)
        self.assertIn("dtoverlay=gpio-poweroff,gpiopin=26,active_low=0", settings)
        self.assertIn("dtoverlay=gpio-shutdown,gpio_pin=3,active_low=1,gpio_pull=up,debounce=10", settings)
        self.assertIn("dtparam=i2c_arm=off", settings)
        self.assertIn("HandlePowerKey=poweroff", (HERE / "logind.conf").read_text())


@unittest.skipUnless(shutil.which("aplay"), "aplay is required for the actual ALSA matrix test")
class AlsaMixTests(unittest.TestCase):
    def mix(self, samples, channels):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            output = directory / "captured.raw"
            config = (HERE / "asound.conf").read_text().replace("hw:CARD=Headphones,DEV=0", "test_sink")
            config += f'\npcm.null {{ type null }}\npcm.test_sink {{ type file slave.pcm "null" file "{output}" format "raw" }}\n'
            (directory / "alsa.conf").write_text(config)
            audio = directory / "input.wav"
            with wave.open(str(audio), "wb") as wav:
                wav.setnchannels(channels)
                wav.setsampwidth(2)
                wav.setframerate(48000)
                wav.writeframes(struct.pack("<" + "h" * len(samples), *samples))
            env = {**os.environ, "ALSA_CONFIG_PATH": str(directory / "alsa.conf")}
            subprocess.run(["aplay", "-q", "-D", "default", str(audio)], env=env, check=True, capture_output=True, timeout=10)
            count = len(samples) // channels * 2
            return struct.unpack("<" + "h" * count, output.read_bytes()[: count * 2])

    def test_stereo_left_right_and_antiphase_mix_without_clipping(self):
        self.assertEqual(self.mix([10000, 0, 0, 10000, -10000, 10000, 12000, 4000, 32766, 32766], 2),
                         (5000, 5000, 5000, 5000, 0, 0, 8000, 8000, 32766, 32766))

    def test_mono_input_retains_level(self):
        self.assertEqual(self.mix([10000, -10000, 16000, 0], 1),
                         (10000, 10000, -10000, -10000, 16000, 16000, 0, 0))


if __name__ == "__main__":
    unittest.main()
