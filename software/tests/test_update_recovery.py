import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOFTWARE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOFTWARE))
import update_recovery as recovery


class StableRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "opt/dialback-zero"
        self.state = self.root / "var/lib/dialback-zero/updates"
        self.state.mkdir(parents=True)
        self.previous_name = "releases/v1.0.0-" + "a" * 16
        self.candidate_name = "releases/v1.1.0-" + "b" * 16
        for name in (self.previous_name, self.candidate_name):
            (self.app / name / "runtime").mkdir(parents=True)
        (self.app / "current").symlink_to(self.candidate_name)
        self.previous_script = self.app / self.previous_name / "runtime/update_manager.py"
        # A known-good previous updater records execution and restores its link.
        # The selected candidate is deliberately not even valid Python.
        self.previous_script.write_text(
            "import json, os, sys\nfrom pathlib import Path\n"
            f"app = Path({str(self.app)!r})\nstate = Path({str(self.state)!r})\n"
            "journal = json.loads((state / 'transaction.json').read_text())\n"
            "link = app / '.recovered'\nlink.symlink_to(journal['previous'])\n"
            "os.replace(link, app / 'current')\n"
            "(state / 'executed').write_text(sys.argv[1])\n"
            "(state / 'transaction.json').unlink()\n")
        (self.app / self.candidate_name / "runtime/update_manager.py").write_text("this is invalid Python !!!\n")

    def transaction(self, **changes):
        journal = {"previous": self.previous_name, "candidate": self.candidate_name}
        journal.update(changes)
        (self.state / "transaction.json").write_text(json.dumps(journal))

    def queue(self):
        (self.state / "request.json").write_text('{"action": "check"}\n')
        (self.state / "status.json").write_text('{"phase": "checking", "available_version": "v1.1.0"}\n')

    def test_broken_current_cannot_prevent_execution_of_previous_recovery(self):
        self.transaction()
        selected = self.app / "current"
        stable = self.root / "usr/local/lib/dialback-zero-recovery.py"
        stable.parent.mkdir(parents=True)
        stable.write_bytes((SOFTWARE / "update_recovery.py").read_bytes())
        unchanged = stable.read_bytes()
        execute = mock.Mock(wraps=subprocess.run)
        recovery.recover("recover", self.app, self.state, execute=execute)
        execute.assert_called_once_with([sys.executable, "-B", str(self.previous_script), "recover"], check=True)
        self.assertEqual(os.readlink(selected), self.previous_name)
        self.assertEqual((self.state / "executed").read_text(), "recover")
        self.assertFalse((self.state / "transaction.json").exists())
        self.assertEqual(stable.read_bytes(), unchanged)

    def test_normal_boot_never_loads_or_executes_selected_updater(self):
        execute = mock.Mock()
        recovery.recover("recover", self.app, self.state, execute=execute)
        execute.assert_not_called()
        self.assertEqual(os.readlink(self.app / "current"), self.candidate_name)
        self.assertFalse((self.state / "status.json").exists())

    def test_boot_clears_queued_check_without_reading_candidate_code(self):
        self.queue()
        execute = mock.Mock()
        recovery.recover("recover", self.app, self.state, execute=execute)
        execute.assert_not_called()
        self.assertFalse((self.state / "request.json").exists())
        self.assertEqual(json.loads((self.state / "status.json").read_text())["phase"], "failed")

    def test_successful_finish_preserves_fresh_queued_request(self):
        self.queue()
        before = (self.state / "request.json").read_bytes()
        with mock.patch.dict(os.environ, {"SERVICE_RESULT": "success"}):
            recovery.recover("finish", self.app, self.state, execute=mock.Mock())
        self.assertEqual((self.state / "request.json").read_bytes(), before)
        self.assertEqual(json.loads((self.state / "status.json").read_text())["phase"], "checking")

    def test_failed_start_before_running_marker_clears_queued_request(self):
        self.queue()
        with mock.patch.dict(os.environ, {"SERVICE_RESULT": "exit-code"}):
            recovery.recover("finish", self.app, self.state, execute=mock.Mock())
        self.assertFalse((self.state / "request.json").exists())
        self.assertEqual(json.loads((self.state / "status.json").read_text())["phase"], "failed")

    def test_running_marker_is_cleared_after_interruption(self):
        self.queue()
        (self.state / "running").write_text('{"started": 1}\n')
        with mock.patch.dict(os.environ, {"SERVICE_RESULT": "timeout"}):
            recovery.recover("finish", self.app, self.state, execute=mock.Mock())
        self.assertFalse((self.state / "running").exists())
        self.assertFalse((self.state / "request.json").exists())
        self.assertEqual(json.loads((self.state / "status.json").read_text())["phase"], "failed")

    def test_untrusted_journal_and_symlinked_previous_updater_are_refused(self):
        execute = mock.Mock()
        for target in ("../../escaped", "/tmp/escaped", "releases/../escaped", "releases/.hidden"):
            with self.subTest(target=target):
                self.transaction(previous=target)
                with self.assertRaises(ValueError):
                    recovery.recover("recover", self.app, self.state, execute=execute)
        self.transaction()
        self.previous_script.unlink()
        self.previous_script.symlink_to(self.app / self.candidate_name / "runtime/update_manager.py")
        with self.assertRaisesRegex(ValueError, "symlink"):
            recovery.recover("recover", self.app, self.state, execute=execute)
        execute.assert_not_called()

    def test_duplicate_oversized_and_symlinked_journals_are_refused(self):
        journal = self.state / "transaction.json"
        execute = mock.Mock()
        for data in ('{"previous":"x","previous":"y"}', '{"previous": NaN}', " " * (recovery.MAX_JSON + 1)):
            journal.write_text(data)
            with self.assertRaises(ValueError):
                recovery.recover("recover", self.app, self.state, execute=execute)
        journal.unlink()
        journal.symlink_to(self.root / "missing.json")
        with self.assertRaises(ValueError):
            recovery.recover("recover", self.app, self.state, execute=execute)
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
