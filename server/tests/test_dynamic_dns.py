"""Exercise the real dnsmasq config without Docker or external DNS traffic."""
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

from smoke import dns_query


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("dnsmasq"), "dnsmasq is required")
class DynamicDnsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="dialback-dns-test-")
        cls.addClassCleanup(cls.scratch.cleanup)
        cls.root = Path(cls.scratch.name)
        cls.hosts = cls.root / "hosts"
        cls.hosts.mkdir()
        cls.registry = cls.hosts / "registered-hosts"
        cls.registry.write_text("")
        cls.upstream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cls.upstream.bind(("127.0.0.1", 0))
        cls.upstream.settimeout(0.1)
        cls.addClassCleanup(cls.upstream.close)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]

        # Only relocate listening address, runtime identity and the shared
        # directory. All record and forwarding policy comes from production.
        overrides = {
            "port": str(cls.port), "listen-address": "127.0.0.1",
            "user": pwd.getpwuid(os.getuid()).pw_name,
            "hostsdir": str(cls.hosts),
        }
        config = []
        for line in (ROOT / "dns/dnsmasq.conf").read_text().splitlines():
            key = line.partition("=")[0]
            if key == "group":
                continue
            config.append(f"{key}={overrides[key]}" if key in overrides else line)
        # Even an explicitly configured upstream must not receive unknown
        # names: local=/#/ makes every name local. This UDP sink is loopback.
        config.append(f"server=127.0.0.1#{cls.upstream.getsockname()[1]}")
        path = cls.root / "dnsmasq.conf"
        path.write_text("\n".join(config) + "\n")
        cls.log = (cls.root / "dnsmasq.log").open("w+")
        cls.addClassCleanup(cls.log.close)
        cls.process = subprocess.Popen(
            ["dnsmasq", "--keep-in-foreground", "--conf-file=" + str(path)],
            stdout=cls.log, stderr=subprocess.STDOUT)
        cls.addClassCleanup(cls.stop_dns)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if cls.process.poll() is not None:
                cls.log.seek(0)
                raise AssertionError("dnsmasq failed to start: " + cls.log.read())
            try:
                if cls.query("retro.net")[0][1] & 15 == 0:
                    return
            except OSError:
                pass
            time.sleep(0.02)
        raise AssertionError("dnsmasq did not become ready")

    @classmethod
    def stop_dns(cls):
        if cls.process.poll() is None:
            cls.process.terminate()
        cls.process.wait(timeout=3)

    @classmethod
    def query(cls, name, **kwargs):
        return dns_query(name, server="127.0.0.1", port=cls.port, timeout=0.3, **kwargs)

    def publish(self, text):
        temporary = self.root / "next-hosts"
        temporary.write_text(text)
        os.replace(temporary, self.registry)

    def assert_record(self, name, address=None, tcp=False):
        deadline = time.monotonic() + 2
        while True:
            fields, packet = self.query(name, tcp=tcp)
            if address is None:
                matches = fields[1] & 15 == 3 and fields[3] == 0
            else:
                matches = (fields[1] & 15 == 0 and fields[3] == 1
                           and socket.inet_aton(address) in packet)
            if matches:
                return
            if time.monotonic() >= deadline:
                self.fail(f"{name}: expected {address or 'NXDOMAIN'}, got {fields}")
            time.sleep(0.02)

    def test_registration_replacement_and_removal_are_live_and_exact(self):
        self.publish("10.77.0.1 niklas.com www.niklas.com\n")
        for tcp in (False, True):
            for name in ("niklas.com", "www.niklas.com"):
                self.assert_record(name, "10.77.0.1", tcp=tcp)
            for name in ("unregistered.com", "extra.niklas.com", "notniklas.com"):
                self.assert_record(name, tcp=tcp)

        self.publish("10.77.0.9 niklas.com\n10.77.0.1 games.net www.games.net\n")
        for tcp in (False, True):
            self.assert_record("niklas.com", "10.77.0.9", tcp=tcp)
            self.assert_record("www.niklas.com", tcp=tcp)
            self.assert_record("games.net", "10.77.0.1", tcp=tcp)
            self.assert_record("www.games.net", "10.77.0.1", tcp=tcp)

        self.registry.unlink()
        for tcp in (False, True):
            for name in ("niklas.com", "games.net", "www.games.net"):
                self.assert_record(name, tcp=tcp)
            self.assert_record("retro.net", "10.77.0.1", tcp=tcp)

    def test_unknown_public_names_never_reach_even_explicit_upstream(self):
        for tcp in (False, True):
            for name in ("www.example.com", "missing.retro.net", "missing.retro.home.arpa"):
                self.assert_record(name, tcp=tcp)
            fields, _ = self.query("www.example.com", qtype=28, tcp=tcp)
            self.assertEqual(fields[1] & 15, 3)
            self.assertEqual(fields[3], 0)
        with self.assertRaises(socket.timeout):
            self.upstream.recvfrom(4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
