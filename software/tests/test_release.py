import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

SOFTWARE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("dialback_test_release", SOFTWARE / "release.py")
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "software"
        self.source.mkdir()
        for name in ("runtime", "systemd", "hardware", "assets"):
            shutil.copytree(SOFTWARE / name, self.source / name,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copyfile(SOFTWARE / "release.py", self.source / "release.py")
        shutil.copyfile(SOFTWARE / "update_recovery.py", self.source / "update_recovery.py")
        self.dependencies = self.root / "packages"
        self.dependencies.write_text("# Packages\npython3\nppp\nnetwork-manager\n")
        self.binary = self.root / "modem"
        self.binary.write_bytes(b"native fixture")

    def build(self, name="release", **kwargs):
        directory = self.root / name
        manifest = release.build_payload(self.binary, directory, source_root=self.source,
                                         dependencies=self.dependencies, **kwargs)
        return directory, manifest

    def test_payload_is_reproducible_complete_and_records_source(self):
        directory, manifest = self.build(version="v1.2.3", source_commit="a" * 40)
        second, other = self.build("second", version="v1.2.3", source_commit="a" * 40)
        self.assertEqual(manifest, other)
        self.assertEqual((directory / "release.json").read_bytes(), (second / "release.json").read_bytes())
        self.assertEqual(manifest["architecture"], "armhf")
        self.assertEqual(manifest["baseline"], "armv6")
        self.assertEqual(manifest["version"], "v1.2.3")
        self.assertEqual(manifest["source_commit"], "a" * 40)
        self.assertEqual(release.validate_release(directory), manifest)
        self.assertRegex(release.release_id(manifest), r"^v1\.2\.3-[a-f0-9]{16}$")
        self.assertIn("runtime/release.py", manifest["files"])
        self.assertIn("runtime/prepare-ethernet.py", manifest["files"])
        self.assertIn("runtime/ethernet.nmconnection.example", manifest["files"])
        self.assertIn("sounds/dial-up.wav", manifest["files"])
        self.assertFalse(any(name.startswith(("etc/", "var/")) for name in manifest["files"]))
        self.assertEqual((directory / "runtime/config-menu").stat().st_mode & 0o777, 0o755)

    def test_application_changes_leave_platform_compatible(self):
        _, first = self.build("first")
        for name in ("runtime/launcher.py", "hardware/prepare-ethernet.py", "hardware/ethernet.nmconnection.example"):
            with (self.source / name).open("a") as stream:
                stream.write("\n# app change\n")
        self.binary.write_bytes(b"changed binary")
        _, updated = self.build("updated")
        self.assertEqual(first["platform"], updated["platform"])
        self.assertNotEqual(first["files"], updated["files"])
        self.assertNotEqual(release.release_id(first), release.release_id(updated))

    def test_fixed_platform_files_and_dependencies_change_compatibility(self):
        baseline = release.platform_fingerprint(self.source, self.dependencies)
        for relative in ("hardware/asound.conf", "hardware/ethernet.service", "systemd/dialback-zero-modem.service", "update_recovery.py"):
            path = self.source / relative
            original = path.read_bytes()
            path.write_bytes(original + b"\n# changed\n")
            self.assertNotEqual(release.platform_fingerprint(self.source, self.dependencies), baseline)
            path.write_bytes(original)
        self.dependencies.write_text("network-manager\n# reordered\nppp\npython3\npython3\n")
        self.assertEqual(release.platform_fingerprint(self.source, self.dependencies), baseline)
        self.dependencies.write_text("python3\nppp\nnetwork-manager\nnew-package\n")
        self.assertNotEqual(release.platform_fingerprint(self.source, self.dependencies), baseline)

    def test_checksums_and_file_inventory_are_enforced(self):
        directory, manifest = self.build()
        binary = directory / "bin/dialback-zero-modem"
        binary.write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "checksum"):
            release.validate_release(directory)
        binary.write_bytes(self.binary.read_bytes())
        extra = directory / "runtime/unlisted.py"
        extra.write_text("unlisted\n")
        with self.assertRaisesRegex(ValueError, "file list"):
            release.validate_release(directory)
        extra.unlink()
        binary.unlink()
        with self.assertRaisesRegex(ValueError, "file list"):
            release.validate_release(directory)

    def test_no_symlink_or_special_file_is_allowed(self):
        directory, _ = self.build()
        launcher = directory / "runtime/launcher.py"
        launcher.unlink()
        launcher.symlink_to(self.source / "runtime/launcher.py")
        with self.assertRaisesRegex(ValueError, "symlink"):
            release.validate_release(directory)
        launcher.unlink()
        shutil.copyfile(self.source / "runtime/launcher.py", launcher)
        metadata = directory / "release.json"
        saved = self.root / "saved.json"
        metadata.replace(saved)
        metadata.symlink_to(saved)
        with self.assertRaisesRegex(ValueError, "regular file"):
            release.validate_release(directory)

    def test_manifest_rejects_unsafe_paths_wrong_platform_and_invalid_metadata(self):
        _, manifest = self.build()
        for name in ("../etc/config", "/etc/config", "runtime/../oops.py", "runtime/subdir/file.py",
                     "runtime//file.py", "runtime\\file.py", "etc/config.json", "sounds/.secret.wav"):
            bad = copy.deepcopy(manifest)
            bad["files"][name] = "a" * 64
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.validate_manifest(bad)
        for key, value in (("format", True), ("format", 2), ("architecture", "aarch64"),
                           ("baseline", "armv7"), ("version", "../../oops"), ("version", "v1.2.3\n"),
                           ("platform", "not a digest"), ("source_commit", "")):
            bad = copy.deepcopy(manifest)
            bad[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                release.validate_manifest(bad)
        with self.assertRaisesRegex(ValueError, "full image"):
            release.validate_manifest(manifest, expected_platform="0" * 64)
        for required in ("runtime/update_manager.py", "runtime/config_web.py", "runtime/ppp_wrapper.py"):
            bad = copy.deepcopy(manifest)
            del bad["files"][required]
            with self.subTest(required=required), self.assertRaisesRegex(ValueError, "required"):
                release.validate_manifest(bad)

    def test_duplicate_manifest_keys_and_nonempty_destination_are_refused(self):
        directory, manifest = self.build()
        path = directory / "release.json"
        path.write_text(json.dumps(manifest).replace('"format": 1', '"format": 1, "format": 1'))
        with self.assertRaisesRegex(ValueError, "duplicate"):
            release.validate_release(directory)
        with self.assertRaisesRegex(ValueError, "empty physical"):
            self.build()


if __name__ == "__main__":
    unittest.main()
