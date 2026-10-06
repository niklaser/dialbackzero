"""Run inside an isolated WireGuard client container, against the real services."""
import socket
import struct
import time
import unittest


SERVER = "10.77.0.1"


def dns_query(name, qtype=1, tcp=False, server=SERVER, port=53, timeout=3):
    question = b"".join(bytes([len(part)]) + part.encode("ascii")
                        for part in name.split(".")) + b"\0" + struct.pack("!HH", qtype, 1)
    packet = struct.pack("!6H", 1234, 0x0100, 1, 0, 0, 0) + question
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout)
        sock.connect((server, port))
        if tcp:
            sock.sendall(struct.pack("!H", len(packet)) + packet)
            prefix = sock.recv(2)
            length = struct.unpack("!H", prefix)[0]
            response = b""
            while len(response) < length:
                part = sock.recv(length - len(response))
                if not part:
                    raise IOError("Short DNS response")
                response += part
        else:
            sock.send(packet)
            response = sock.recv(4096)
    return struct.unpack("!6H", response[:12]), response


def http(path="/", host="www.retro.home.arpa", method="GET", version="1.0"):
    with socket.create_connection((SERVER, 80), timeout=3) as sock:
        request = f"{method} {path} HTTP/{version}\r\n"
        if host is not None:
            request += f"Host: {host}\r\n"
        sock.sendall((request + "Connection: close\r\n\r\n").encode("ascii"))
        response = b""
        while True:
            block = sock.recv(8192)
            if not block:
                break
            response += block
    headers, body = response.split(b"\r\n\r\n", 1)
    return int(headers.split(b" ")[1]), headers, body


class TunnelServices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for attempt in range(30):
            try:
                if http()[0] == 200:
                    return
            except OSError:
                pass
            time.sleep(0.3)
        raise AssertionError("HTTP never became reachable through WireGuard")

    def test_http_10_without_host_and_read_only_files(self):
        status, _, body = http(host=None)
        self.assertEqual(status, 200)
        self.assertIn(b"Welcome home.", body)
        self.assertNotIn(b"<script", body.lower())
        for host, path in (("files.retro.home.arpa", "/hello.txt"),
                           ("www.retro.home.arpa", "/files/hello.txt")):
            with self.subTest(host=host):
                status, _, body = http(path, host)
                self.assertEqual(status, 200)
                self.assertIn(b"private file download works", body)
                self.assertEqual(http(path, host, method="HEAD")[2], b"")
                self.assertEqual(http(path, host, method="PUT")[0], 403)

    def test_no_path_or_hidden_file_escape(self):
        for host, path in (("www.retro.home.arpa", "/files/../../etc/passwd"),
                           ("files.retro.home.arpa", "/%2e%2e/etc/passwd"),
                           ("files.retro.home.arpa", "/.private")):
            with self.subTest(path=path):
                status, _, body = http(path, host)
                self.assertIn(status, (400, 403, 404))
                self.assertNotIn(b"root:x:", body)

    def test_private_dns_udp_and_tcp(self):
        for tcp in (False, True):
            for name in ("ns.retro.home.arpa", "www.retro.home.arpa", "files.retro.home.arpa",
                         "retro.net", "www.retro.net", "files.retro.net", "members.retro.net"):
                with self.subTest(name=name, tcp=tcp):
                    fields, packet = dns_query(name, tcp=tcp)
                    self.assertEqual(fields[0], 1234)
                    self.assertEqual(fields[1] & 15, 0)
                    self.assertTrue(fields[1] & 0x0400)  # authoritative answer
                    self.assertEqual(fields[3], 1)
                    self.assertIn(socket.inet_aton(SERVER), packet)

    def test_unknown_name_nxdomain_and_no_public_recursion(self):
        # dnsmasq advertises RA in local mode; local=/#/ and no-resolv
        # prevent forwarding. The response must still be an exact negative.
        for tcp in (False, True):
            for name in ("missing.retro.home.arpa", "missing.retro.net", "www.example.com"):
                with self.subTest(name=name, tcp=tcp):
                    fields, _ = dns_query(name, tcp=tcp)
                    self.assertEqual(fields[1] & 15, 3)
                    self.assertEqual(fields[3], 0)

    def test_unused_tunnel_port_is_blocked(self):
        with self.assertRaises(OSError):
            socket.create_connection((SERVER, 443), timeout=0.5)

    def test_bridge_address_cannot_bypass_tunnel(self):
        bridge = socket.gethostbyname("wireguard")
        self.assertNotEqual(bridge, SERVER)
        with self.assertRaises(OSError):
            socket.create_connection((bridge, 80), timeout=0.5)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            query = (struct.pack("!6H", 1234, 0x0100, 1, 0, 0, 0)
                     + b"\x03www\x05retro\x04home\x04arpa\0"
                     + struct.pack("!HH", 1, 1))
            sock.sendto(query, (bridge, 53))
            with self.assertRaises(OSError):
                sock.recv(4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
