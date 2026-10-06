from __future__ import annotations

import hashlib
import importlib.util
import contextlib
import io
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[2]
IMAGE = PROJECT / "image"
WORKFLOW = PROJECT / ".github" / "workflows" / "image.yml"
RUNTIME_UNITS = {
    "dialback-zero-activate.service",
    "dialback-zero-leds.service",
    "dialback-zero-forwarding.service",
    "dialback-zero-network.service",
    "dialback-zero-ppp-internet.service",
    "dialback-zero-ppp-hub.service",
    "dialback-zero-config.service",
    "dialback-zero-modem.service",
}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def unit_values(path: Path, key: str) -> set[str]:
    prefix = key + "="
    return {
        value
        for line in path.read_text().splitlines()
        if line.startswith(prefix)
        for value in line.removeprefix(prefix).split()
    }


class ImageDefinitionTests(unittest.TestCase):
    def test_pi_gen_is_full_commit_and_trixie_armhf(self) -> None:
        commit = (IMAGE / "pi-gen.commit").read_text().strip()
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        config = (IMAGE / "config").read_text()
        self.assertIn("RELEASE='trixie'", config)
        self.assertIn("STAGE_LIST='stage0 stage1 stage2 stage-dialback'", config)
        self.assertNotIn("arm64", config)

    def test_image_has_no_baked_credentials_or_remote_access(self) -> None:
        config = (IMAGE / "config").read_text()
        self.assertIn("ENABLE_CLOUD_INIT='1'", config)
        self.assertIn("ENABLE_SSH='0'", config)
        self.assertIn("DISABLE_FIRST_BOOT_USER_RENAME='0'", config)
        self.assertNotIn("FIRST_USER_PASS=", config)
        self.assertNotIn("PUBKEY_SSH_FIRST_USER=", config)
        self.assertNotIn("WPA_PASSWORD=", config)

    def test_runtime_and_build_packages_are_declared(self) -> None:
        package_file = IMAGE / "pi-gen-stage" / "00-dependencies" / "00-packages-nr"
        packages = {
            line.strip()
            for line in package_file.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        expected = {
            "python3",
            "python3-libgpiod",
            "ppp",
            "network-manager",
            "alsa-utils",
            "nftables",
            "iptables",
            "wireguard-tools",
            "build-essential",
            "libasound2-dev",
            "binutils",
        }
        self.assertTrue(expected.issubset(packages))

    def test_build_targets_armv6_and_uses_offline_installer(self) -> None:
        compile_script = (
            IMAGE / "pi-gen-stage" / "02-build" / "00-run-chroot.sh"
        ).read_text()
        self.assertIn("-march=armv6", compile_script)
        self.assertIn("-mfpu=vfp", compile_script)
        self.assertIn("-mfloat-abi=hard", compile_script)

        install_script = (
            IMAGE / "pi-gen-stage" / "03-install" / "00-run.sh"
        ).read_text()
        self.assertIn('software/install.py"', install_script)
        self.assertIn('--root "${ROOTFS_DIR}"', install_script)
        self.assertIn('--binary "${BINARY}"', install_script)
        self.assertIn("--hardware rev-c-ethernet", install_script)

        build_script = (IMAGE / "build.sh").read_text()
        self.assertIn('python3 "${SCRIPT_DIR}/patch_pi_gen.py"', build_script)
        self.assertIn("for directory in assets hardware runtime systemd vendor", build_script)
        self.assertIn('"${PROJECT_DIR}/software/update_recovery.py"', build_script)
        self.assertIn('"${PI_GEN_DIR}/dialback-source/software/update_recovery.py"', build_script)
        self.assertIn("--exclude '*.private'", build_script)
        self.assertIn("--exclude '.env'", build_script)
        self.assertNotIn('cp -a "${PROJECT_DIR}/software"', build_script)

    def test_stage_scripts_have_valid_shell_syntax(self) -> None:
        scripts = [IMAGE / "build.sh"]
        scripts.extend((IMAGE / "pi-gen-stage").rglob("*.sh"))
        for script in scripts:
            with self.subTest(script=script):
                subprocess.run(["bash", "-n", script], check=True)

    def test_privileged_image_build_never_runs_for_pull_requests(self) -> None:
        workflow = WORKFLOW.read_text()
        self.assertNotIn("pull_request:", workflow)
        self.assertIn("needs: test", workflow)
        self.assertIn("GH_REPO: ${{ github.repository }}", workflow)
        self.assertIn("runner.environment == 'github-hosted'", workflow)
        self.assertIn("/usr/local/lib/android", workflow)
        self.assertIn("/usr/share/dotnet", workflow)
        self.assertIn("/opt/ghc", workflow)
        self.assertNotRegex(workflow, r"uses:\s+[^\s]+@(main|master|v\d+)\s*$")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn('tags: ["v*"]', workflow)
        self.assertIn('"software/**"', workflow)
        self.assertIn('"image/**"', workflow)
        self.assertIn("Test Pi hardware configuration", workflow)
        self.assertNotIn('"hardware/**"', workflow)
        self.assertNotIn("tools/", workflow)

    def test_offline_installer_creates_expected_service_graph(self) -> None:
        """Exercise the production installer against a minimal mounted OS root."""
        installer = load_module("dialback_image_installer", PROJECT / "software/install.py")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            boot = root / "boot/firmware"
            (boot / "overlays").mkdir(parents=True)
            for overlay in ("audremap", "gpio-shutdown", "gpio-poweroff", "disable-bt", "w5500"):
                (boot / "overlays" / f"{overlay}.dtbo").write_bytes(b"fixture")
            (boot / "config.txt").write_text("# image fixture\n")
            (boot / "cmdline.txt").write_text("console=serial0,115200 root=fixture rw\n")
            (root / "etc/systemd/system").mkdir(parents=True)
            network_manager = root / "usr/lib/systemd/system/NetworkManager.service"
            network_manager.parent.mkdir(parents=True)
            network_manager.write_text("[Unit]\n")
            binary = root / "tmp/dialback-zero-modem"
            binary.parent.mkdir()
            binary.write_bytes(b"native fixture")
            binary.chmod(0o755)

            with contextlib.redirect_stdout(io.StringIO()):
                installer.install(root, binary, "rev-c-ethernet")

            units = root / "etc/systemd/system"
            wants = units / "multi-user.target.wants"
            self.assertEqual({entry.name for entry in wants.iterdir()}, {"dialback-zero.target"})
            self.assertTrue((wants / "dialback-zero.target").is_symlink())
            self.assertEqual(os.readlink(wants / "dialback-zero.target"), "../dialback-zero.target")

            target = units / "dialback-zero.target"
            self.assertEqual(unit_values(target, "Wants"), RUNTIME_UNITS)
            recovery = "dialback-zero-update-recovery.service"
            self.assertEqual(unit_values(target, "Requires"), {recovery})
            self.assertTrue((units / recovery).is_file())
            self.assertEqual(unit_values(units / recovery, "Before"), {
                "dialback-zero.target", "dialback-zero-ethernet.service", *RUNTIME_UNITS,
            })
            stable_recovery = root / "usr/local/lib/dialback-zero-recovery.py"
            self.assertTrue(stable_recovery.is_file())
            self.assertFalse(stable_recovery.is_symlink())
            self.assertIn("/usr/local/lib/dialback-zero-recovery.py", unit_values(units / recovery, "ExecStart"))
            worker = units / "dialback-zero-update.service"
            self.assertTrue(worker.is_file())
            self.assertNotIn("dialback-zero.target", unit_values(worker, "PartOf"))
            for unit in RUNTIME_UNITS:
                self.assertTrue((units / unit).is_file(), unit)
                self.assertIn(recovery, unit_values(units / unit, "Requires"))
                self.assertIn(recovery, unit_values(units / unit, "After"))

            ethernet = units / "dialback-zero-ethernet.service"
            drop_in = units / "NetworkManager.service.d/90-dialback-zero-ethernet.conf"
            self.assertTrue(ethernet.is_file())
            self.assertIn(recovery, unit_values(ethernet, "Requires"))
            self.assertIn(recovery, unit_values(ethernet, "After"))
            self.assertEqual(unit_values(drop_in, "Wants"), {"dialback-zero-ethernet.service"})
            self.assertEqual(unit_values(drop_in, "After"), {"dialback-zero-ethernet.service"})

            network = units / "dialback-zero-network.service"
            self.assertEqual(
                unit_values(network, "After"),
                {"dialback-zero-activate.service", "NetworkManager.service", recovery},
            )
            self.assertEqual(unit_values(network, "PartOf"), {"dialback-zero.target"})
            self.assertNotIn("dialback-zero-network.service", (units / "dialback-zero-modem.service").read_text())


class PackageArtifactsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        module_path = IMAGE / "package_artifacts.py"
        cls.module = load_module("package_artifacts", module_path)

    def test_safe_version(self) -> None:
        self.assertEqual(self.module.safe_version("v1.2.3"), "v1.2.3")
        self.assertEqual(self.module.safe_version("feature/test image"), "feature-test-image")
        with self.assertRaises(ValueError):
            self.module.safe_version("///")

    def test_sha256_streams_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.img.xz"
            path.write_bytes(b"dialback-zero")
            expected = hashlib.sha256(b"dialback-zero").hexdigest()
            self.assertEqual(self.module.sha256(path), expected)


class PiGenPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module("patch_pi_gen", IMAGE / "patch_pi_gen.py")

    def fixture(self, root: Path) -> None:
        stage0 = root / "stage0"
        configure = stage0 / "00-configure-apt"
        sources = configure / "files"
        sources.mkdir(parents=True)
        (stage0 / "prerun.sh").write_text(
            '#!/bin/bash -e\n\nif [ ! -d "${ROOTFS_DIR}" ]; then\n'
            '\tbootstrap ${RELEASE} "${ROOTFS_DIR}" '
            "http://raspbian.raspberrypi.com/raspbian/\nfi\n"
        )
        (sources / "raspbian.sources").write_text(
            "Types: deb\nURIs: http://raspbian.raspberrypi.com/raspbian/\nSuites: RELEASE\n"
        )
        (sources / "raspi.sources").write_text("Types: deb\nSuites: RELEASE\n")
        (sources / "raspberrypi-archive-keyring.pgp").write_bytes(b"fixture")
        (configure / "00-run.sh").write_text(
            '#!/bin/bash -e\n\n'
            'true > "${ROOTFS_DIR}/etc/apt/sources.list"\n'
            'install -m 644 files/raspbian.sources "${ROOTFS_DIR}/etc/apt/sources.list.d/"\n'
            'install -m 644 files/raspi.sources "${ROOTFS_DIR}/etc/apt/sources.list.d/"\n'
            'sed -i "s/RELEASE/${RELEASE}/g" "${ROOTFS_DIR}/etc/apt/sources.list.d/raspbian.sources"\n'
            'sed -i "s/RELEASE/${RELEASE}/g" "${ROOTFS_DIR}/etc/apt/sources.list.d/raspi.sources"\n\n'
            'if [ -n "$APT_PROXY" ]; then\n'
            '\tinstall -m 644 files/51cache "${ROOTFS_DIR}/etc/apt/apt.conf.d/51cache"\n'
            'else\n\trm -f "${ROOTFS_DIR}/etc/apt/apt.conf.d/51cache"\nfi\n\n'
            'if [ -n "$TEMP_REPO" ]; then\n'
            '\tinstall -m 644 /dev/null "${ROOTFS_DIR}/etc/apt/sources.list.d/00-temp.list"\n'
            'else\n\trm -f "${ROOTFS_DIR}/etc/apt/sources.list.d/00-temp.list"\nfi\n\n'
            'install -m 644 files/raspberrypi-archive-keyring.pgp "${ROOTFS_DIR}/usr/share/keyrings/"\n'
            'on_chroot <<- \\EOF\n\tapt-get update\n\tapt-get dist-upgrade -y\nEOF\n'
        )
        (root / "Dockerfile").write_text(
            "RUN apt-get install \\\n"
            "        ca-certificates fdisk gpg pigz arch-test \\\n"
            "    && rm -rf /var/lib/apt/lists/*\n"
        )
        (root / "build.sh").write_text(
            "#!/bin/bash -e\n\n# shellcheck disable=SC2119\n"
            "run_sub_stage()\n{\n"
            "\t\t\ton_chroot << EOF\n"
            "apt-get -o Acquire::Retries=3 install --no-install-recommends -y $PACKAGES\n"
            "EOF\n"
            "\t\t\ton_chroot << EOF\n"
            "apt-get -o Acquire::Retries=3 install -y $PACKAGES\n"
            "EOF\n}\n"
        )

    def test_patch_uses_primary_archive_and_adds_host_tools(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.module.patch_pi_gen(root, self.module.DEFAULT_MIRROR)
            self.assertIn(
                "https://archive.raspbian.org/raspbian/",
                (root / "stage0/prerun.sh").read_text(),
            )
            self.assertIn(
                "URIs: https://archive.raspbian.org/raspbian/",
                (root / "stage0/00-configure-apt/files/raspbian.sources").read_text(),
            )
            dockerfile = (root / "Dockerfile").read_text()
            self.assertIn("arch-test python3 binutils", dockerfile)
            build = (root / "build.sh").read_text()
            self.assertIn("--download-only install", build)
            self.assertIn("apt-get --no-download install", build)
            self.assertNotIn("Acquire::Retries=3", build)
            self.assertEqual(build.count("install_packages $PACKAGES"), 1)
            self.assertEqual(build.count("install_packages --no-install-recommends $PACKAGES"), 1)
            self.assertNotIn(self.module.UPSTREAM_MIRROR, "".join(
                path.read_text() for path in root.rglob("*") if path.is_file()
            ))

    def test_retry_policy_exists_before_first_target_apt_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.module.patch_pi_gen(root, self.module.DEFAULT_MIRROR)
            target = root / "rootfs"
            for relative in (
                "etc/apt/apt.conf.d",
                "etc/apt/sources.list.d",
                "usr/share/keyrings",
            ):
                (target / relative).mkdir(parents=True)
            commands = root / "commands"
            commands.mkdir()
            marker = root / "first-target-apt"
            on_chroot = commands / "on_chroot"
            on_chroot.write_text(
                "#!/bin/sh\nset -eu\n"
                'test "$(cat "$ROOTFS_DIR/etc/apt/apt.conf.d/80-dialback-retries")" = '
                "'Acquire::Retries \"5\";'\n"
                "grep -q 'apt-get update'\n"
                'touch "$APT_CALL_MARKER"\n'
            )
            on_chroot.chmod(0o755)
            environment = os.environ.copy()
            environment.update({
                "APT_CALL_MARKER": str(marker),
                "APT_PROXY": "",
                "PATH": str(commands) + os.pathsep + environment["PATH"],
                "RELEASE": "trixie",
                "ROOTFS_DIR": str(target),
                "TEMP_REPO": "",
            })
            subprocess.run(
                ["bash", "00-run.sh"],
                cwd=root / "stage0/00-configure-apt",
                env=environment,
                check=True,
            )
            self.assertTrue(marker.is_file())

    def run_install_helper(
        self, download_statuses: list[int], install_status: int, no_recommends: bool
    ) -> tuple[subprocess.CompletedProcess[str], list[list[str]], list[str]]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            self.module.patch_pi_gen(root, self.module.DEFAULT_MIRROR)
            commands = root / "commands"
            commands.mkdir()
            state = root / "statuses.json"
            log = root / "apt-log.jsonl"
            sleeps = root / "sleep-log"
            state.write_text(json.dumps(download_statuses))
            apt_get = commands / "apt-get"
            apt_get.write_text(
                "#!/usr/bin/env python3\n"
                "import json, os, pathlib, sys\n"
                "state = pathlib.Path(os.environ['APT_STATUS_FILE'])\n"
                "log = pathlib.Path(os.environ['APT_LOG_FILE'])\n"
                "with log.open('a') as stream: stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "if '--download-only' in sys.argv:\n"
                "    statuses = json.loads(state.read_text())\n"
                "    status = statuses.pop(0)\n"
                "    state.write_text(json.dumps(statuses))\n"
                "    if status: print('unexpected EOF', file=sys.stderr)\n"
                "    raise SystemExit(status)\n"
                "raise SystemExit(int(os.environ['APT_INSTALL_STATUS']))\n"
            )
            apt_get.chmod(0o755)
            sleep = commands / "sleep"
            sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$1" >> "$APT_SLEEP_LOG"\n')
            sleep.chmod(0o755)
            runner = root / "runner.sh"
            arguments = "--no-install-recommends fixture-package" if no_recommends else "fixture-package"
            runner.write_text(
                (root / "build.sh").read_text()
                + "\non_chroot() { /bin/sh -e; }\n"
                + f"install_packages {arguments}\n"
            )
            environment = os.environ.copy()
            environment.update({
                "APT_INSTALL_STATUS": str(install_status),
                "APT_LOG_FILE": str(log),
                "APT_SLEEP_LOG": str(sleeps),
                "APT_STATUS_FILE": str(state),
                "PATH": str(commands) + os.pathsep + environment["PATH"],
            })
            result = subprocess.run(
                ["bash", runner], env=environment, text=True, capture_output=True
            )
            calls = [json.loads(line) for line in log.read_text().splitlines()]
            delays = sleeps.read_text().splitlines() if sleeps.exists() else []
            return result, calls, delays

    def test_prefetch_retries_eof_then_installs_once_in_both_modes(self) -> None:
        for no_recommends in (False, True):
            with self.subTest(no_recommends=no_recommends):
                result, calls, delays = self.run_install_helper([100, 0], 0, no_recommends)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(sum("--download-only" in call for call in calls), 2)
                self.assertEqual(sum("--no-download" in call for call in calls), 1)
                self.assertEqual(delays, ["2"])
                for call in calls:
                    self.assertEqual("--no-install-recommends" in call, no_recommends)

    def test_prefetch_exhaustion_returns_100_without_install(self) -> None:
        result, calls, delays = self.run_install_helper([100, 100, 100], 0, False)
        self.assertEqual(result.returncode, 100)
        self.assertEqual(sum("--download-only" in call for call in calls), 3)
        self.assertFalse(any("--no-download" in call for call in calls))
        self.assertEqual(delays, ["2", "4"])

    def test_install_failure_is_not_retried(self) -> None:
        result, calls, delays = self.run_install_helper([0], 42, False)
        self.assertEqual(result.returncode, 42)
        self.assertEqual(sum("--download-only" in call for call in calls), 1)
        self.assertEqual(sum("--no-download" in call for call in calls), 1)
        self.assertEqual(delays, [])

    def test_mirror_override_is_validated_and_upstream_drift_is_rejected(self) -> None:
        self.assertEqual(
            self.module.validated_mirror("http://mirror.example.test/raspbian"),
            "http://mirror.example.test/raspbian/",
        )
        for invalid in (
            "file:///srv/raspbian",
            "https://user:secret@mirror.example/raspbian/",
            "https://mirror.example/raspbian/?suite=trixie",
            "https://mirror.example/raspbian/; touch /tmp/unsafe",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.module.validated_mirror(invalid)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            build = root / "build.sh"
            build.write_text(build.read_text().replace(
                "apt-get -o Acquire::Retries=3 install -y $PACKAGES",
                "apt-get -o Acquire::Retries=4 install -y $PACKAGES",
            ))
            before = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            with self.assertRaisesRegex(ValueError, "content changed"):
                self.module.patch_pi_gen(root, self.module.DEFAULT_MIRROR)
            after = {
                path.relative_to(root): path.read_bytes()
                for path in root.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
