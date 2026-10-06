#!/usr/bin/env python3
"""Build and test an isolated server + WireGuard peer; publishes no host ports."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("retro_setup", ROOT / "setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def main():
    project = f"dbz-test-{os.getpid()}"
    client = project + "-client"
    with tempfile.TemporaryDirectory(prefix="dialback-server-test-") as scratch:
        scratch = Path(scratch)
        setup.create_bundle("192.168.1.50:51820", scratch / "keys")
        profile = json.loads((scratch / "keys/pi-hub.json").read_text())
        override = scratch / "compose.test.yaml"
        override.write_text("services:\n  wireguard:\n    ports: !reset []\n")
        compose = ["docker", "compose", "--project-name", project,
                   "--env-file", str(scratch / "keys/server.env"),
                   "-f", str(ROOT / "compose.yaml"), "-f", str(override)]
        try:
            run(compose + ["up", "--build", "--wait", "--wait-timeout", "90"])
            image = project + ":check"
            run(["docker", "build", "--target", "check", "-t", image, str(ROOT)])
            run(["docker", "run", "-d", "--name", client,
                 "--network", project + "_default", "--cap-drop", "ALL",
                 "--cap-add", "NET_ADMIN", "--security-opt", "no-new-privileges:true",
                 image], stdout=subprocess.DEVNULL)
            config = (f"[Interface]\nPrivateKey = {profile['private_key']}\n"
                      f"[Peer]\nPublicKey = {profile['server_public_key']}\n"
                      "AllowedIPs = 10.77.0.1/32\nEndpoint = wireguard:51820\n")
            # Private key travels only on stdin, not in argv, output or Docker env.
            run(["docker", "exec", "-i", client, "sh", "-c",
                 "umask 077; cat > /tmp/wg.conf; ip link add wg-test type wireguard; "
                 "wg setconf wg-test /tmp/wg.conf; rm /tmp/wg.conf; "
                 "ip address add 10.77.0.2/32 dev wg-test; "
                 "ip link set wg-test up; ip route add 10.77.0.1/32 dev wg-test"],
                input=config)
            run(["docker", "exec", client, "python3", "/smoke.py"])
            run(["docker", "exec", client, "python3", "/sites_smoke.py"])
            run(compose + ["up", "-d", "--no-deps", "--force-recreate", "--wait", "sites", "ftp", "dns"])
            run(["docker", "exec", client, "python3", "/sites_smoke.py", "--verify-existing"])
            print("WireGuard, HTTP 1.0, member sites, FTP, private DNS and persistence passed.")
        except Exception:
            subprocess.run(compose + ["logs", "--no-color", "--tail", "25"], check=False)
            raise
        finally:
            subprocess.run(["docker", "rm", "-f", client], capture_output=True)
            subprocess.run(compose + ["down", "--volumes", "--remove-orphans"], check=False)


if __name__ == "__main__":
    main()
