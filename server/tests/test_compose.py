"""Keep runtime-only secrets out of Compose build requirements."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_KEY = base64.b64encode(b"p" * 32).decode("ascii")


def clean_environment():
    env = os.environ.copy()
    for name in ("WG_PRIVATE_KEY", "WG_PEER_PUBLIC_KEY", "DBZ_BIND_IP", "DBZ_WG_PORT",
                 "COMPOSE_PROJECT_NAME", "COMPOSE_FILE"):
        env.pop(name, None)
    return env


@unittest.skipUnless(shutil.which("docker"), "Docker Compose is required")
class ComposeBuildTests(unittest.TestCase):
    def test_compose_and_build_plan_work_without_runtime_private_key(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "build-time.env"
            env_file.write_text("DBZ_BIND_IP=192.168.1.50\nWG_PEER_PUBLIC_KEY=" + PUBLIC_KEY + "\n")
            command = ["docker", "compose", "--env-file", str(env_file),
                       "-f", str(ROOT / "compose.yaml")]
            result = subprocess.run(command + ["config", "--format", "json"],
                                    env=clean_environment(), capture_output=True, text=True, check=True)
            model = json.loads(result.stdout)
            self.assertEqual(model["services"]["wireguard"]["environment"]["WG_PRIVATE_KEY"], "")
            # --print resolves the same Compose build inputs without requiring
            # a daemon, downloading images, or using any runtime private key.
            result = subprocess.run(command + ["build", "--print"], env=clean_environment(),
                                    capture_output=True, text=True, check=True)
            plan = json.loads(result.stdout)
            targets = {"wireguard": "wireguard", "http": "http", "dns": "dns", "sites": "sites", "ftp": "sites"}
            self.assertEqual(set(plan["target"]), set(targets))
            for service, target in targets.items():
                self.assertEqual(plan["target"][service]["target"], target)


    def test_explicit_and_directory_project_names_use_identical_images(self):
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            project = "a1b2c3d4e5f6g7h8"
            runtime = scratch / project
            runtime.mkdir()
            # Compose infers the project name from the containing directory
            # when --project-name is omitted.
            compose_file = runtime / "docker-compose.yaml"
            compose_file.write_text((ROOT / "compose.yaml").read_text())
            env_file = scratch / "build-time.env"
            env_file.write_text("DBZ_BIND_IP=192.168.1.50\nWG_PEER_PUBLIC_KEY=" + PUBLIC_KEY + "\n")
            build = ["docker", "compose", "--project-name", project,
                     "--env-file", str(env_file), "-f", str(ROOT / "compose.yaml")]
            up = ["docker", "compose", "--env-file", str(env_file), "-f", str(compose_file)]
            def images(command):
                result = subprocess.run(command + ["config", "--images"], env=clean_environment(),
                                        capture_output=True, text=True, check=True)
                return set(result.stdout.splitlines())
            expected = {project + "-" + service for service in ("wireguard", "http", "dns", "sites", "ftp")}
            self.assertEqual(images(build), expected)
            self.assertEqual(images(up), expected)


class RuntimeKeyTests(unittest.TestCase):
    def run_startup(self, private_key=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tools = root / "bin"
            tools.mkdir()
            # Do not modify any real firewall or network interface. The real
            # entrypoint must reject the key before an ip/wg-set command.
            (tools / "iptables-restore").write_text("#!/bin/sh\ncat >/dev/null\n")
            (tools / "ip").write_text("#!/bin/sh\nprintf unexpected > \"$TEST_NETWORK_CALL\"\nexit 91\n")
            for path in tools.iterdir():
                path.chmod(0o755)
            env = clean_environment()
            env.update(PATH=str(tools) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                       WG_PEER_PUBLIC_KEY=PUBLIC_KEY, TEST_NETWORK_CALL=str(root / "network-called"))
            if private_key is not None:
                env["WG_PRIVATE_KEY"] = private_key
            # Only relocate the private temporary key directory. All startup
            # validation and command ordering remain the actual entrypoint's.
            script = (ROOT / "wireguard/start.sh").read_text().replace("/run/wireguard", str(root / "wireguard"))
            result = subprocess.run(["sh", "-s"], input=script, env=env,
                                    capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "network-called").exists(), "network setup ran with an invalid private key")
            self.assertNotIn("Retro hub ready", result.stdout)
            return result

    def test_runtime_rejects_absent_and_empty_private_key(self):
        for key in (None, ""):
            with self.subTest(key=key):
                result = self.run_startup(key)
                self.assertIn("WG_PRIVATE_KEY must be provisioned explicitly", result.stderr)

    @unittest.skipUnless(shutil.which("wg"), "wireguard-tools is required")
    def test_runtime_rejects_malformed_private_key_without_logging_it(self):
        key = "malformed-private-key-test-marker"
        result = self.run_startup(key)
        self.assertNotIn(key, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
