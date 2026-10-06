"""Exercise release installation and recovery with real small on-disk bundles."""

import io
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

SOFTWARE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOFTWARE))
sys.path.insert(0, str(SOFTWARE / "runtime"))

import release
import update_manager as updates


class PowerLoss(BaseException):
    """Leave the durable journal behind, as an abruptly stopped worker would."""


class UpdateManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.app = self.root / "opt/dialback-zero"
        self.state = self.root / "var/lib/dialback-zero/updates"
        (self.app / "releases").mkdir(parents=True)
        self.state.mkdir(parents=True)
        self.source = self.root / "build/software"
        for name in ("runtime", "hardware", "systemd", "assets"):
            (self.source / name).mkdir(parents=True)
        for name in ("launcher.py", "dialback_config.py", "config_menu.py", "update_manager.py",
                     "config_web.py", "led_daemon.py", "forwarding.py", "network.py",
                     "activate_config.py", "ppp_wrapper.py", "hub_status.py", "hub_setup.py"):
            (self.source / "runtime" / name).write_text("# Tiny application fixture\n")
        for name in release.HARDWARE_PLATFORM_FILES:
            (self.source / "hardware" / name).write_text("fixed platform fixture\n")
        (self.source / "hardware/prepare-ethernet.py").write_text("# Ethernet helper\n")
        (self.source / "hardware/ethernet.nmconnection.example").write_text("[connection]\n")
        (self.source / "systemd/dialback-zero.target").write_text("[Unit]\nDescription=Fixture\n")
        (self.source / "assets/dial-up.wav").write_bytes(b"tiny sound fixture")
        shutil.copyfile(SOFTWARE / "release.py", self.source / "release.py")
        shutil.copyfile(SOFTWARE / "update_recovery.py", self.source / "update_recovery.py")
        self.dependencies = self.root / "packages"
        self.dependencies.write_text("python3\nppp\n")
        self.binary = self.root / "fixture-modem"
        self.binary.write_bytes(b"native executable checked by injected preflight")
        self.serial = 0
        initial, self.installed = self.payload("v1.0.0")
        self.previous = self.app / "releases" / release.release_id(self.installed)
        initial.rename(self.previous)
        (self.app / "current").symlink_to("releases/" + self.previous.name)
        self.previous_files = {path.relative_to(self.previous).as_posix(): path.read_bytes()
                               for path in self.previous.rglob("*") if path.is_file()}
        self.settings = {
            "etc/dialback-zero/config.json": b'{ "private": "preserve spacing and values" }\n',
            "etc/NetworkManager/system-connections/home.nmconnection": b"wifi-secret\n",
            "etc/ppp/chap-secrets": b"ppp-secret\n",
            "etc/wireguard/hub.conf": b"wireguard-secret\n",
            "var/lib/dialback-zero/hub-state.json": b'{"persistent": true}\n',
        }
        for name, data in self.settings.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o600)
        self.patch = mock.patch.multiple(updates, APP=self.app, STATE=self.state,
                                        CONFIG=self.root / "etc/dialback-zero/config.json",
                                        PENDING_CONFIG=self.root / "var/lib/dialback-zero/pending-config.json")
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def payload(self, version):
        self.serial += 1
        destination = self.root / f"payload-{self.serial}"
        manifest = release.build_payload(self.binary, destination, version=version,
                                         source_commit="a" * 40, source_root=self.source,
                                         dependencies=self.dependencies)
        return destination, manifest

    def bundle(self, version="v1.1.0", platform=None, malicious=None):
        directory, manifest = self.payload(version)
        if platform is not None:
            manifest["platform"] = platform
            (directory / "release.json").write_bytes(release.manifest_bytes(manifest))
        archive = self.root / f"bundle-{self.serial}.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for name in sorted({"release.json", *manifest["files"]}):
                # Only files, no directory headers, are part of the format.
                bundle.add(directory / name, arcname=name, recursive=False)
            if malicious is not None:
                member, data = malicious
                bundle.addfile(member, io.BytesIO(data) if data is not None else None)
        filename = f"dialback-zero-{version}-armv6-update.tar.gz"
        candidate = {
            "version": version,
            "url": f"https://github.com/{updates.REPOSITORY}/releases/download/{version}/{filename}",
            "size": archive.stat().st_size,
            "sha256": release.file_hash(archive),
        }

        def fetch(url, destination=None, limit=None):
            self.assertEqual(url, candidate["url"])
            self.assertIsNotNone(destination)
            shutil.copyfile(archive, destination)
            return b""

        return candidate, manifest, mock.Mock(side_effect=fetch)

    def assert_settings_unchanged(self):
        for name, data in self.settings.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)
            self.assertEqual((self.root / name).stat().st_mode & 0o777, 0o600, name)
        self.assertEqual({path.relative_to(self.previous).as_posix(): path.read_bytes()
                          for path in self.previous.rglob("*") if path.is_file()}, self.previous_files)

    def assert_previous_selected(self):
        self.assertEqual((self.app / "current").resolve(), self.previous)
        self.assertEqual(updates.get_status(self.app, self.state)["installed_version"], "v1.0.0")
        self.assert_settings_unchanged()

    def service_fakes(self, outcomes=(True,)):
        restarted = []
        restart = mock.Mock(side_effect=lambda: restarted.append((self.app / "current").resolve()))
        check = mock.Mock(side_effect=outcomes)
        verify = mock.Mock()
        return restarted, restart, check, verify

    def test_success_switches_complete_release_preserves_settings_and_previous_version(self):
        obsolete, obsolete_manifest = self.payload("v0.9.0")
        obsolete_path = self.app / "releases" / release.release_id(obsolete_manifest)
        obsolete.rename(obsolete_path)
        unrelated = self.app / "releases/user-files"
        unrelated.mkdir()
        (unrelated / "note").write_text("keep this")
        candidate, manifest, fetch = self.bundle()
        restarted, restart, check, verify = self.service_fakes()
        updates.install_candidate(self.app, self.state, candidate, fetch=fetch,
                                  restart=restart, check=check, verify=verify)
        selected = (self.app / "current").resolve()
        self.assertEqual(selected.name, release.release_id(manifest))
        self.assertEqual(release.validate_release(selected), manifest)
        self.assertEqual(restarted, [selected])
        verify.assert_called_once()
        check.assert_called_once()
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(os.readlink(self.app / "current").startswith("/"))
        self.assertFalse((self.state / "transaction.json").exists())
        self.assertEqual(updates.get_status(self.app, self.state)["phase"], "succeeded")
        self.assertFalse(obsolete_path.exists())
        self.assertTrue(self.previous.is_dir())
        self.assertEqual((unrelated / "note").read_text(), "keep this")
        self.assert_settings_unchanged()

    def test_bad_archive_checksum_never_switches_or_restarts(self):
        candidate, _, fetch = self.bundle()
        candidate["sha256"] = "0" * 64
        _, restart, check, verify = self.service_fakes()
        with self.assertRaisesRegex(updates.UpdateError, "checksum"):
            updates.install_candidate(self.app, self.state, candidate, fetch=fetch,
                                      restart=restart, check=check, verify=verify)
        restart.assert_not_called()
        verify.assert_not_called()
        self.assertFalse((self.state / "transaction.json").exists())
        self.assert_previous_selected()

    def test_incompatible_platform_is_refused_before_running_new_code(self):
        candidate, _, fetch = self.bundle(platform="0" * 64)
        _, restart, check, verify = self.service_fakes()
        with self.assertRaisesRegex(updates.UpdateError, "platform"):
            updates.install_candidate(self.app, self.state, candidate, fetch=fetch,
                                      restart=restart, check=check, verify=verify)
        restart.assert_not_called()
        verify.assert_not_called()
        self.assert_previous_selected()

    def test_archive_traversal_symlinks_hardlinks_and_duplicate_entries_are_refused(self):
        for kind in ("traversal", "absolute", "symlink", "hardlink", "duplicate"):
            with self.subTest(kind=kind):
                name = {"traversal": "../../escaped", "absolute": "/tmp/dialback-escape",
                        "duplicate": "release.json"}.get(kind, "runtime/link.py")
                entry = tarfile.TarInfo(name)
                if kind in {"symlink", "hardlink"}:
                    entry.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                    entry.linkname = "../../etc/dialback-zero/config.json"
                    data = None
                else:
                    data = b"unexpected"
                    entry.size = len(data)
                candidate, _, fetch = self.bundle(malicious=(entry, data))
                _, restart, check, verify = self.service_fakes()
                with self.assertRaisesRegex(updates.UpdateError, "Unsafe"):
                    updates.install_candidate(self.app, self.state, candidate, fetch=fetch,
                                              restart=restart, check=check, verify=verify)
                restart.assert_not_called()
                verify.assert_not_called()
                self.assert_previous_selected()
        self.assertEqual(list((self.app / "releases").iterdir()), [self.previous])

    def test_failed_new_version_health_rolls_back_and_restarts_previous_version(self):
        candidate, manifest, fetch = self.bundle()
        restarted, restart, check, verify = self.service_fakes(outcomes=(False, True))
        with self.assertRaisesRegex(updates.UpdateError, "previous version was restored"):
            updates.install_candidate(self.app, self.state, candidate, fetch=fetch,
                                      restart=restart, check=check, verify=verify)
        self.assertEqual([path.name for path in restarted], [release.release_id(manifest), self.previous.name])
        self.assertEqual(check.call_count, 2)
        self.assertEqual(updates.get_status(self.app, self.state)["phase"], "rolled_back")
        self.assertFalse((self.state / "transaction.json").exists())
        self.assert_previous_selected()

    def test_complete_staged_release_can_be_retried_after_rollback(self):
        candidate, manifest, fetch = self.bundle()
        _, restart, check, verify = self.service_fakes(outcomes=(False, True))
        with self.assertRaises(updates.UpdateError):
            updates.install_candidate(self.app, self.state, candidate, fetch=fetch,
                                      restart=restart, check=check, verify=verify)
        self.assert_previous_selected()
        _, restart, check, verify = self.service_fakes()
        updates.install_candidate(self.app, self.state, candidate, fetch=fetch,
                                  restart=restart, check=check, verify=verify)
        self.assertEqual((self.app / "current").resolve().name, release.release_id(manifest))
        self.assertEqual(len(list((self.app / "releases").iterdir())), 2)
        self.assertEqual(updates.get_status(self.app, self.state)["phase"], "succeeded")
        self.assert_settings_unchanged()

    def test_settings_staged_during_download_prevent_switch_and_are_preserved(self):
        candidate, _, fetch = self.bundle()
        _, restart, check, verify = self.service_fakes()
        staged = b'{ "staged": "new settings" }\n'
        verify.side_effect = lambda directory: updates.PENDING_CONFIG.write_bytes(staged)
        with self.assertRaisesRegex(updates.UpdateError, "staged settings"):
            updates.install_candidate(self.app, self.state, candidate, fetch=fetch,
                                      restart=restart, check=check, verify=verify)
        restart.assert_not_called()
        self.assertEqual(updates.PENDING_CONFIG.read_bytes(), staged)
        self.assertFalse((self.state / "transaction.json").exists())
        self.assert_previous_selected()

    def test_boot_recovery_restores_previous_release_after_interrupted_switch(self):
        candidate, _, fetch = self.bundle()
        with self.assertRaises(PowerLoss):
            updates.install_candidate(self.app, self.state, candidate, fetch=fetch,
                                      restart=mock.Mock(side_effect=PowerLoss),
                                      check=mock.Mock(), verify=mock.Mock())
        self.assertNotEqual((self.app / "current").resolve(), self.previous)
        self.assertTrue((self.state / "transaction.json").exists())
        with mock.patch.object(updates, "_restart") as restart, mock.patch.object(updates, "healthy") as check:
            updates.recover(self.app, self.state)
            restart.assert_not_called()
            check.assert_not_called()
        self.assertFalse((self.state / "transaction.json").exists())
        self.assertEqual(updates.get_status(self.app, self.state)["phase"], "rolled_back")
        self.assert_previous_selected()
        updates.recover(self.app, self.state)
        self.assert_previous_selected()

    def test_stop_hook_recovers_failed_worker_without_waiting_for_reboot(self):
        candidate, _, fetch = self.bundle()
        with self.assertRaises(PowerLoss):
            updates.install_candidate(self.app, self.state, candidate, fetch=fetch,
                                      restart=mock.Mock(side_effect=PowerLoss),
                                      check=mock.Mock(), verify=mock.Mock())
        with mock.patch.object(updates, "_restart") as restart, mock.patch.object(updates, "healthy", return_value=True):
            updates.finish(self.app, self.state)
            restart.assert_called_once()
        self.assertFalse((self.state / "transaction.json").exists())
        self.assertEqual(updates.get_status(self.app, self.state)["phase"], "rolled_back")
        self.assert_previous_selected()

    def test_interrupted_queued_check_recovers_and_accepts_another_check(self):
        with mock.patch.object(updates, "_run"):
            updates.request_check()
        self.assertEqual(updates.get_status(self.app, self.state)["phase"], "checking")
        self.assertTrue((self.state / "request.json").exists())
        self.assertFalse((self.state / "running").exists())
        updates.recover(self.app, self.state)
        self.assertEqual(updates.get_status(self.app, self.state)["phase"], "failed")
        self.assertFalse((self.state / "request.json").exists())
        with mock.patch.object(updates, "_run"):
            updates.request_check()
        self.assertTrue((self.state / "request.json").exists())
        self.assert_previous_selected()

    def test_previous_stop_hook_cannot_consume_a_fresh_update_request(self):
        import update_recovery
        commands = []

        def run(command, **kwargs):
            commands.append(command[1])
            if command[1] == "stop":
                self.assertFalse((self.state / "request.json").exists())
                with mock.patch.dict(os.environ, {"SERVICE_RESULT": "signal"}):
                    update_recovery.recover("finish", self.app, self.state)
                with self.assertRaises(updates.UpdateError):
                    updates.request_check()
            else:
                self.assertTrue((self.state / "request.json").exists())

        with mock.patch.object(updates, "_run", side_effect=run):
            updates.request_check()
        self.assertEqual(commands, ["stop", "start"])
        self.assertEqual(updates.get_status(self.app, self.state)["phase"], "checking")
        self.assertEqual(json.loads((self.state / "request.json").read_text()), {"action": "check"})

    def github_response(self, version="v1.1.0", **changes):
        filename = f"dialback-zero-{version}-armv6-update.tar.gz"
        prefix = f"https://github.com/{updates.REPOSITORY}/releases/download/{version}/"
        response = {
            "tag_name": version, "draft": False, "prerelease": False,
            "assets": [
                {"name": filename, "size": 1234, "browser_download_url": prefix + filename,
                 "digest": "sha256:" + "a" * 64},
                {"name": "SHA256SUMS", "browser_download_url": prefix + "SHA256SUMS"},
            ],
        }
        response.update(changes)

        def fetch(url):
            if url == updates.API:
                return json.dumps(response).encode()
            self.assertEqual(url, prefix + "SHA256SUMS")
            return ("a" * 64 + "  " + filename + "\n").encode()

        return response, mock.Mock(side_effect=fetch)

    def test_queued_check_worker_reports_available_release_without_changing_settings(self):
        _, fetch = self.github_response()
        with mock.patch.object(updates, "_run"):
            updates.request_check()
        updates.worker(self.app, self.state, fetch=fetch)
        status = updates.get_status(self.app, self.state)
        self.assertEqual(status["phase"], "available")
        self.assertEqual(status["available_version"], "v1.1.0")
        candidate = json.loads((self.state / "candidate.json").read_text())
        self.assertEqual(candidate["version"], "v1.1.0")
        self.assertEqual(candidate["sha256"], "a" * 64)
        self.assertFalse((self.state / "request.json").exists())
        self.assertFalse((self.state / "running").exists())
        self.assert_previous_selected()

    def test_same_or_older_release_is_not_offered_or_installed(self):
        for version in ("v1.0.0", "v0.9.9"):
            with self.subTest(version=version):
                _, fetch = self.github_response(version)
                with mock.patch.object(updates, "_run"):
                    updates.request_check()
                updates.worker(self.app, self.state, fetch=fetch)
                status = updates.get_status(self.app, self.state)
                self.assertEqual(status["phase"], "idle")
                self.assertIsNone(status["available_version"])
                self.assertFalse((self.state / "candidate.json").exists())
                fetch.assert_called_once_with(updates.API)
                no_download = mock.Mock()
                with self.assertRaisesRegex(updates.UpdateError, "not newer"):
                    updates.install_candidate(self.app, self.state, {"version": version}, fetch=no_download)
                no_download.assert_not_called()
                self.assert_previous_selected()

    def test_untrusted_asset_addresses_and_prereleases_are_rejected(self):
        for reason in ("foreign host", "foreign repository", "http", "checksum asset", "prerelease", "draft", "tag suffix"):
            with self.subTest(reason=reason):
                response, fetch = self.github_response()
                if reason == "foreign host":
                    response["assets"][0]["browser_download_url"] = "https://evil.example/bundle.tar.gz"
                elif reason == "foreign repository":
                    response["assets"][0]["browser_download_url"] = response["assets"][0]["browser_download_url"].replace(
                        updates.REPOSITORY, "someone/else")
                elif reason == "http":
                    response["assets"][0]["browser_download_url"] = response["assets"][0]["browser_download_url"].replace("https:", "http:")
                elif reason == "checksum asset":
                    response["assets"][1]["browser_download_url"] = "https://evil.example/SHA256SUMS"
                elif reason == "tag suffix":
                    response["tag_name"] = "v1.1.0-rc1"
                else:
                    response[reason] = True
                with self.assertRaises(updates.UpdateError):
                    updates.discover(self.installed, fetch=fetch)
                fetch.assert_called_once_with(updates.API)
                self.assert_previous_selected()

    def test_install_refuses_tampered_cached_url_without_download(self):
        candidate, _, _ = self.bundle()
        candidate["url"] = "https://evil.example/bundle.tar.gz"
        fetch = mock.Mock()
        with self.assertRaisesRegex(updates.UpdateError, "address"):
            updates.install_candidate(self.app, self.state, candidate, fetch=fetch)
        fetch.assert_not_called()
        self.assert_previous_selected()


if __name__ == "__main__":
    unittest.main()
