import copy
import fcntl
import json
import sys
from pathlib import Path
import tempfile
import unittest

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))

import dialback_config as config


class ConfigTests(unittest.TestCase):
    def test_defaults_lock_in_public_numbers_and_usable_audio(self):
        validated = config.validate(copy.deepcopy(config.DEFAULT_CONFIG))
        self.assertEqual(validated["modem"]["numbers"]["2242525"],
                         {"enabled": True, "host": "127.0.0.1", "port": 5432})
        self.assertFalse(validated["modem"]["numbers"]["777"]["enabled"])
        self.assertEqual(validated["audio"]["volume_percent"], 70)

    def test_volume_accepts_full_closed_range(self):
        for volume in (0, 1, 70, 100):
            candidate = copy.deepcopy(config.DEFAULT_CONFIG)
            candidate["audio"]["volume_percent"] = volume
            self.assertEqual(config.validate(candidate)["audio"]["volume_percent"], volume)
        for volume in (-1, 101, True):
            candidate = copy.deepcopy(config.DEFAULT_CONFIG)
            candidate["audio"]["volume_percent"] = volume
            with self.assertRaises(config.ConfigError):
                config.validate(candidate)

    def test_rejects_unknowns_shellish_hosts_and_bad_ranges(self):
        for mutate in (
            lambda value: value.update({"surprise": True}),
            lambda value: value["modem"]["numbers"]["2242525"].update(host="x;reboot"),
            lambda value: value["audio"].update(volume_percent=101),
            lambda value: value["ppp"].update(peer="not-an-address"),
            lambda value: value["web"].update(bind="192.168.1.5"),
        ):
            candidate = copy.deepcopy(config.DEFAULT_CONFIG)
            mutate(candidate)
            with self.assertRaises(config.ConfigError):
                config.validate(candidate)

    def test_stage_is_atomic_and_only_activates_explicitly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            active = root / "etc/config.json"
            state = root / "state"
            original = copy.deepcopy(config.DEFAULT_CONFIG)
            config._atomic_write(active, original)
            changed = copy.deepcopy(original)
            changed["audio"]["volume_percent"] = 22
            pending = config.stage(changed, active, state)
            self.assertEqual(config.load(active)["audio"]["volume_percent"], 70)
            self.assertEqual(json.loads(pending.read_text())["audio"]["volume_percent"], 22)
            self.assertTrue(config.activate_pending(active, state))
            self.assertFalse(pending.exists())
            self.assertEqual(config.load(active)["audio"]["volume_percent"], 22)
            self.assertFalse(config.activate_pending(active, state))

    def test_redaction_does_not_mutate_or_reveal_password(self):
        value = copy.deepcopy(config.DEFAULT_CONFIG)
        value["wifi"]["password"] = "private value"
        rendered = json.dumps(config.redacted(value))
        self.assertNotIn("private value", rendered)
        self.assertEqual(value["wifi"]["password"], "private value")

    def test_serial_and_web_staging_refuse_an_active_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            active = root / "config.json"
            original = copy.deepcopy(config.DEFAULT_CONFIG)
            config._atomic_write(active, original)
            updates = state / "updates"
            updates.mkdir(parents=True)
            with (updates / "lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                with self.assertRaisesRegex(config.ConfigError, "update is running"):
                    config.stage(original, active, state)
            (updates / "status.json").write_text('{"phase":"downloading"}')
            with self.assertRaisesRegex(config.ConfigError, "update is running"):
                config.stage(original, active, state)
            self.assertFalse(config.pending_path(active, state).exists())
            self.assertEqual(config.load(active), original)
            (updates / "status.json").write_text('{"phase":"succeeded"}')
            self.assertTrue(config.stage(original, active, state).exists())


if __name__ == "__main__":
    unittest.main()
