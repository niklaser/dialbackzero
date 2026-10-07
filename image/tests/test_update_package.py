from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[2]


def load_module(name, relative):
    spec = importlib.util.spec_from_file_location(name, PROJECT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arm_elf(*, machine=40, cpu=6, fp=2, vfp_args=1, flags=0x05000400):
    """Small ELF fixture with real .ARM.attributes, inspected by host readelf."""
    attributes = bytes([6, cpu, 10, fp, 28, vfp_args])
    subsection = bytes([1]) + struct.pack("<I", 5 + len(attributes)) + attributes
    section = b"A" + struct.pack("<I", 10 + len(subsection)) + b"aeabi\0" + subsection
    strings = b"\0.shstrtab\0.ARM.attributes\0"
    header = b"\x7fELF\x01\x01\x01" + b"\0" * 9
    header += struct.pack(
        "<HHIIIIIHHHHHH", 2, machine, 1, 0, 0, 52 + len(section) + len(strings),
        flags, 52, 0, 0, 40, 3, 1,
    )
    string_table = struct.pack("<IIIIIIIIII", 1, 3, 0, 0, 52 + len(section), len(strings), 0, 0, 1, 0)
    attribute_table = struct.pack("<IIIIIIIIII", 11, 0x70000003, 0, 0, 52, len(section), 0, 0, 1, 0)
    return header + section + strings + b"\0" * 40 + string_table + attribute_table


class UpdatePackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = load_module("test_package_update", "image/package_update.py")
        cls.artifacts = load_module("test_update_artifacts", "image/package_artifacts.py")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.binary = self.root / "modem"
        self.binary.write_bytes(arm_elf())
        self.binary.chmod(0o755)

    def payload(self):
        directory = self.root / "release"
        self.package.release.build_payload(
            self.binary, directory, version="v1.2.3", source_commit="a" * 40,
        )
        return directory

    def deploy(self):
        directory = self.root / "deploy"
        self.package.package_update(self.payload(), directory)
        (directory / "image_pi-gen.img.xz").write_bytes(b"compressed-image-fixture")
        return directory

    def arguments(self, deploy, *, version="v1.2.3", source_commit="a" * 40):
        return ["--deploy", str(deploy), "--output", str(self.root / "output"),
                "--version", version, "--source-commit", source_commit,
                "--pi-gen-commit", "b" * 40]

    def test_armv6_checks_real_elf_attributes_and_hard_float(self):
        self.package.validate_armv6_binary(self.binary)
        for values in (
            {"machine": 62}, {"cpu": 10}, {"cpu": 11}, {"fp": 3},
            {"vfp_args": 0}, {"flags": 0x05000200}, {"flags": 0x05000000},
        ):
            with self.subTest(values=values):
                self.binary.write_bytes(arm_elf(**values))
                with self.assertRaises(ValueError):
                    self.package.validate_armv6_binary(self.binary)
        self.binary.write_bytes(b"#!/bin/sh\nexit 0\n")
        with self.assertRaisesRegex(ValueError, "32-bit ELF"):
            self.package.validate_armv6_binary(self.binary)

    def test_archive_is_reproducible_flat_and_matches_installed_payload(self):
        directory = self.payload()
        current = self.root / "current"
        current.symlink_to("release")
        first = self.package.package_update(current, self.root / "first")
        for source in directory.rglob("*"):
            os.utime(source, (123456789, 123456789))
            if source.is_file():
                source.chmod(0o600)
        second = self.package.package_update(current, self.root / "second")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(first.name, "dialback-zero-v1.2.3-armv6-update.tar.gz")
        manifest = self.package.release.validate_release(directory)
        with tarfile.open(first, "r:gz") as archive:
            self.assertEqual(set(archive.getnames()), {"release.json", *manifest["files"]})
            for member in archive:
                self.assertTrue(member.isfile())
                self.assertEqual((member.uid, member.gid, member.mtime), (0, 0, 0))
                self.assertEqual((member.uname, member.gname), ("", ""))
                self.assertEqual(archive.extractfile(member).read(), (directory / member.name).read_bytes())
            self.assertEqual(archive.getmember("runtime/config-menu").mode, 0o755)
            self.assertEqual(archive.getmember("sounds/dial-up.wav").mode, 0o644)
        self.assertEqual(self.artifacts.update_manifest(first), manifest)

    def test_changed_payload_unlisted_secrets_and_links_refuse_export(self):
        directory = self.payload()
        secret = directory / "device.private"
        secret.write_text("private device data")
        with self.assertRaisesRegex(ValueError, "file list"):
            self.package.package_update(directory, self.root / "output")
        secret.unlink()
        path = directory / "runtime/config-menu"
        original = path.read_bytes()
        path.write_bytes(original + b"# altered\n")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.package.package_update(directory, self.root / "output")
        path.unlink()
        path.symlink_to(directory / "runtime/config_menu.py")
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.package.package_update(directory, self.root / "output")
        self.assertFalse((self.root / "output").exists())

    def test_image_and_update_are_published_with_matching_checksums_and_metadata(self):
        deploy = self.deploy()
        self.assertEqual(self.artifacts.main(self.arguments(deploy)), 0)
        output = self.root / "output"
        checksums = (output / "SHA256SUMS").read_text().splitlines()
        self.assertEqual(len(checksums), 2)
        for line in checksums:
            digest, name = line.split("  ")
            self.assertEqual(digest, hashlib.sha256((output / name).read_bytes()).hexdigest())
        metadata = json.loads((output / "build-metadata.json").read_text())
        self.assertEqual(metadata["artifact"], "dialback-zero-v1.2.3-raspios-trixie-armhf.img.xz")
        self.assertEqual(metadata["update_artifact"], "dialback-zero-v1.2.3-armv6-update.tar.gz")
        self.assertEqual(metadata["source_commit"], "a" * 40)
        self.assertEqual(metadata["platform"], self.package.release.platform_fingerprint())
        self.assertEqual(metadata["update_format"], 1)
        self.assertFalse(metadata["boot_tested"])

    def test_stale_bundle_or_missing_update_cannot_publish_an_image(self):
        deploy = self.deploy()
        for override in ({"version": "v1.2.4"}, {"source_commit": "c" * 40}):
            with self.subTest(override=override), self.assertRaisesRegex(SystemExit, "identity"):
                self.artifacts.main(self.arguments(deploy, **override))
        next(deploy.glob("*.tar.gz")).unlink()
        with self.assertRaisesRegex(SystemExit, "expected one ARMv6"):
            self.artifacts.main(self.arguments(deploy))
        self.assertFalse((self.root / "output").exists())

    def test_publication_rechecks_archive_checksums_and_rejects_unsafe_members(self):
        deploy = self.deploy()
        bundle = next(deploy.glob("*.tar.gz"))
        with tarfile.open(bundle) as archive:
            originals = [(member, archive.extractfile(member).read()) for member in archive]
        for mode in ("tamper", "traversal", "duplicate", "symlink", "directory"):
            with self.subTest(mode=mode):
                with tarfile.open(bundle, "w:gz") as archive:
                    for original, data in originals:
                        member = tarfile.TarInfo(original.name)
                        if mode == "tamper" and original.name == "runtime/config-menu":
                            data += b"# changed\n"
                        member.size = len(data)
                        archive.addfile(member, io.BytesIO(data))
                    if mode != "tamper":
                        extra = tarfile.TarInfo({
                            "traversal": "../private", "duplicate": "release.json",
                            "symlink": "runtime/unlisted.py", "directory": "runtime",
                        }[mode])
                        if mode == "symlink":
                            extra.type = tarfile.SYMTYPE
                            extra.linkname = "/etc/shadow"
                        if mode == "directory":
                            extra.type = tarfile.DIRTYPE
                        archive.addfile(extra)
                with self.assertRaises(ValueError):
                    self.artifacts.update_manifest(bundle)

    def run_image_stage(self, stage, rootfs, deploy):
        environment = {
            **os.environ, "ROOTFS_DIR": str(rootfs), "DEPLOY_DIR": str(deploy),
            "DIALBACK_IMAGE_VERSION": "v1.2.3", "DIALBACK_SOURCE_COMMIT": "a" * 40,
        }
        # GNU file otherwise follows links implicitly, hiding the regression.
        environment.pop("POSIXLY_CORRECT", None)
        return subprocess.run(
            ["bash", "-e", PROJECT / "image/pi-gen-stage" / stage / "00-run.sh"],
            env=environment, text=True, capture_output=True,
        )

    def installed_image(self, fixture_name="rootfs"):
        rootfs = self.root / fixture_name
        boot = rootfs / "boot/firmware"
        (boot / "overlays").mkdir(parents=True)
        for overlay in ("audremap", "gpio-shutdown", "gpio-poweroff", "disable-bt", "w5500"):
            (boot / "overlays" / f"{overlay}.dtbo").write_bytes(b"fixture")
        (boot / "config.txt").write_text("# fixture\n")
        (boot / "cmdline.txt").write_text("console=serial0,115200 root=fixture rw\n")
        (rootfs / "etc/systemd/system").mkdir(parents=True)
        (rootfs / "etc/ssh").mkdir()
        network_manager = rootfs / "usr/lib/systemd/system/NetworkManager.service"
        network_manager.parent.mkdir(parents=True)
        network_manager.write_text("[Unit]\n")
        staged = rootfs / "tmp/dialback-zero-build"
        software = staged / "software"
        for name in ("runtime", "hardware", "assets", "systemd"):
            shutil.copytree(PROJECT / "software" / name, software / name)
        for name in ("install.py", "release.py", "update_recovery.py"):
            shutil.copyfile(PROJECT / "software" / name, software / name)
        binary = software / "vendor/tcpser/tcpser"
        binary.parent.mkdir(parents=True)
        shutil.copyfile(self.binary, binary)
        binary.chmod(0o755)
        dependency = staged / "image/pi-gen-stage/00-dependencies/00-packages-nr"
        dependency.parent.mkdir(parents=True)
        shutil.copyfile(PROJECT / "image/pi-gen-stage/00-dependencies/00-packages-nr", dependency)
        shutil.copyfile(PROJECT / "image/package_update.py", staged / "image/package_update.py")
        deploy = self.root / (fixture_name + "-deploy")
        result = self.run_image_stage("03-install", rootfs, deploy)
        self.assertEqual(result.returncode, 0, result.stderr)
        return rootfs, deploy

    def test_pi_gen_install_stage_exports_the_same_release_as_the_image(self):
        rootfs, deploy = self.installed_image()
        installed = (rootfs / "opt/dialback-zero/current").resolve()
        manifest = self.package.release.validate_release(installed)
        self.assertEqual(manifest["version"], "v1.2.3")
        self.assertEqual(manifest["source_commit"], "a" * 40)
        self.assertEqual(manifest["platform"], self.package.release.platform_fingerprint())
        stable_recovery = rootfs / "usr/local/lib/dialback-zero-recovery.py"
        self.assertFalse(stable_recovery.is_symlink())
        self.assertEqual(stable_recovery.read_bytes(), (PROJECT / "software/update_recovery.py").read_bytes())
        self.assertEqual(self.artifacts.update_manifest(next(deploy.glob("*.tar.gz"))), manifest)
        self.assertEqual((rootfs / "usr/local/bin/dialback-zero-modem").read_bytes(), self.binary.read_bytes())

    def test_pi_gen_verify_stage_accepts_versioned_symlinks_and_cleans_build(self):
        rootfs, deploy = self.installed_image()
        for relative in (
            "opt/dialback-zero/current", "usr/local/bin/dialback-zero-modem",
            "usr/local/lib/dialback-zero", "usr/share/dialback-zero/sounds",
        ):
            self.assertTrue((rootfs / relative).is_symlink(), relative)
        staged = rootfs / "tmp/dialback-zero-build"
        self.assertTrue(staged.is_dir())

        result = self.run_image_stage("04-verify", rootfs, deploy)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(staged.exists())

    def test_pi_gen_verify_stage_rejects_invalid_images_before_cleanup(self):
        for invalid in (
            "non-arm", "armv7", "dangling-binary", "service-graph", "update-worker-scope",
            "network-before-modem", "modem-network-dependency", "ssh-key",
        ):
            with self.subTest(invalid=invalid):
                rootfs, deploy = self.installed_image(invalid)
                binary = rootfs / "usr/local/bin/dialback-zero-modem"
                if invalid == "non-arm":
                    binary.write_bytes(arm_elf(machine=62))
                elif invalid == "armv7":
                    binary.write_bytes(arm_elf(cpu=10))
                elif invalid == "dangling-binary":
                    binary.resolve().unlink()
                elif invalid == "service-graph":
                    (rootfs / "etc/systemd/system/dialback-zero.target").write_text("[Unit]\n")
                elif invalid in {"update-worker-scope", "network-before-modem", "modem-network-dependency"}:
                    unit, forbidden = {
                        "update-worker-scope": ("update", "PartOf=dialback-zero.target"),
                        "network-before-modem": ("network", "Before=dialback-zero-modem.service"),
                        "modem-network-dependency": ("modem", "Requires=dialback-zero-network.service"),
                    }[invalid]
                    with (rootfs / f"etc/systemd/system/dialback-zero-{unit}.service").open("a") as stream:
                        stream.write("\n" + forbidden + "\n")
                else:
                    (rootfs / "etc/ssh/ssh_host_ed25519_key").write_text("private key fixture\n")

                result = self.run_image_stage("04-verify", rootfs, deploy)

                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Image verification failed at line", result.stderr)
                self.assertTrue((rootfs / "tmp/dialback-zero-build").is_dir())


if __name__ == "__main__":
    unittest.main()
