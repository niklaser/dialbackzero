import json
import os
import pty
import select
import shutil
import socket
import subprocess
import tempfile
import termios
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TCPSER = ROOT / "vendor" / "tcpser" / "tcpser"


def unused_tcp_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ModemProcess:
    def __init__(self, test_case, extra_args=(), extra_env=None):
        self.test_case = test_case
        self.tempdir = tempfile.TemporaryDirectory()
        self.event_path = str(Path(self.tempdir.name) / "events.sock")
        self.events = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.events.bind(self.event_path)
        self.events.setblocking(False)
        self.master_fd, slave_fd = pty.openpty()
        slave_name = os.ttyname(slave_fd)
        self.slave_name = slave_name
        os.set_blocking(self.master_fd, False)
        self.serial_data = bytearray()
        self.event_backlog = []
        listen_port = unused_tcp_port()
        env = os.environ.copy()
        env.update(
            {
                "DIALBACK_EVENT_SOCKET": self.event_path,
                "DIALBACK_SOUND_MODE": "off",
            }
        )
        if extra_env:
            env.update(extra_env)
        self.proc = subprocess.Popen(
            [
                str(TCPSER),
                "-d",
                slave_name,
                "-s",
                "38400",
                "-p",
                str(listen_port),
                *extra_args,
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        os.close(slave_fd)
        self.wait_event("ready", 1, timeout=4)

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.proc.stderr.close()
        os.close(self.master_fd)
        self.events.close()
        self.tempdir.cleanup()

    def assert_running(self):
        if self.proc.poll() is not None:
            stderr = self.proc.stderr.read().decode("utf-8", "replace")
            self.test_case.fail(
                f"tcpser exited with {self.proc.returncode}: {stderr}"
            )

    def write(self, data):
        offset = 0
        while offset < len(data):
            try:
                offset += os.write(self.master_fd, data[offset:])
            except BlockingIOError:
                select.select([], [self.master_fd], [], 1)

    def read_until(self, expected, timeout=4):
        deadline = time.monotonic() + timeout
        while expected not in self.serial_data and time.monotonic() < deadline:
            self.assert_running()
            readable, _, _ = select.select(
                [self.master_fd], [], [], min(0.1, deadline - time.monotonic())
            )
            if readable:
                try:
                    self.serial_data.extend(os.read(self.master_fd, 4096))
                except BlockingIOError:
                    pass
        self.test_case.assertIn(expected, self.serial_data)
        return bytes(self.serial_data)

    def clear_serial(self):
        self.serial_data.clear()
        while select.select([self.master_fd], [], [], 0)[0]:
            try:
                os.read(self.master_fd, 4096)
            except BlockingIOError:
                break

    def command(self, command, expected=b"OK", timeout=4):
        self.clear_serial()
        self.write(command + b"\r")
        return self.read_until(expected, timeout)

    def _receive_event(self, timeout):
        if self.event_backlog:
            return self.event_backlog.pop(0)
        readable, _, _ = select.select([self.events], [], [], timeout)
        if not readable:
            return None
        return json.loads(self.events.recv(512).decode("ascii"))

    def wait_event(self, name, value=None, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            event = self._receive_event(max(0, deadline - time.monotonic()))
            if event is None:
                break
            if event.get("event") == name and (
                value is None or event.get("value") == value
            ):
                return event
        self.assert_running()
        self.test_case.fail(f"event {name}={value!r} was not received")


class TcpserIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not TCPSER.exists():
            raise unittest.SkipTest("build software/vendor/tcpser/tcpser first")

    def setUp(self):
        self.modems = []

    def tearDown(self):
        for modem in reversed(self.modems):
            modem.close()

    def start_modem(self, *args, extra_env=None):
        modem = ModemProcess(self, args, extra_env)
        self.modems.append(modem)
        return modem

    def test_at_ok_events_autoanswer_and_heartbeat(self):
        modem = self.start_modem()

        serial_fd = os.open(modem.slave_name, os.O_RDWR | os.O_NOCTTY)
        try:
            control_flags = termios.tcgetattr(serial_fd)[2]
        finally:
            os.close(serial_fd)
        self.assertEqual(control_flags & termios.CSIZE, termios.CS8)
        self.assertFalse(control_flags & termios.PARENB)
        self.assertFalse(control_flags & termios.CSTOPB)
        if hasattr(termios, "CRTSCTS"):
            self.assertFalse(control_flags & termios.CRTSCTS)

        response = modem.command(b"AT", expected=b"\r\nOK\r\n")
        self.assertIn(b"\r\nOK\r\n", response)
        modem.wait_event("rx")
        modem.wait_event("tx")
        modem.command(b"AT$CONFIG", expected=b"ERROR")
        modem.command(b"AT$CONFIGX", expected=b"ERROR")

        modem.command(b"ATS0=2")
        modem.wait_event("autoanswer", 2)
        modem.command(b"ATD" + (b"1" * 300), expected=b"ERROR")
        modem.command(b"AT")
        modem.wait_event("ready", 1, timeout=3)
        modem.wait_event("connected", 0)
        modem.wait_event("offhook", 0)

    def test_config_menu_idle_only_and_binary_dial_relay(self):
        payload = b"\x7e\xff\x03\xc0!\x00\x80\xfe\x7e"
        reply = b"\x7e\xff\x03\x80!\x01\x00\xfe\x7e"
        received = bytearray()
        peer_ready = threading.Event()
        release_peer = threading.Event()
        marker = Path(tempfile.mkdtemp(dir="/tmp")) / "menu-runs"
        self.addCleanup(shutil.rmtree, marker.parent, True)
        helper = marker.parent / "config-menu"
        helper.write_text(
            "#!/usr/bin/python3\n"
            "import os\n"
            f"marker = {str(marker)!r}\n"
            "with open(marker, 'ab') as stream: stream.write(b'1')\n"
            "os.write(1, b'\\r\\nCONFIG MENU\\r\\n0. Exit\\r\\nChoice: ')\n"
            "answer = b''\n"
            "while not answer.endswith(b'\\r'):\n"
            "    chunk = os.read(0, 1)\n"
            "    if not chunk: break\n"
            "    answer += chunk\n"
            "os.write(1, b'\\r\\nSaved for restart\\r\\n')\n"
        )
        helper.chmod(0o755)

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        dial_port = listener.getsockname()[1]

        def peer():
            connection, _ = listener.accept()
            connection.settimeout(4)
            peer_ready.set()
            try:
                while payload not in received:
                    data = connection.recv(4096)
                    if not data:
                        return
                    received.extend(data)
                connection.sendall(reply)
                release_peer.wait(5)
            finally:
                connection.close()

        peer_thread = threading.Thread(target=peer, daemon=True)
        peer_thread.start()
        modem = self.start_modem(
            f"-n2242525=127.0.0.1:{dial_port}",
            extra_env={"DIALBACK_CONFIG_MENU": str(helper)},
        )

        modem.clear_serial()
        modem.write(b"AT$CONFIG\r")
        modem.read_until(b"Choice: ")
        modem.write(b"0\r")
        menu_response = modem.read_until(b"\r\nOK\r\n")
        self.assertIn(b"CONFIG MENU", menu_response)
        self.assertEqual(marker.read_bytes(), b"1")

        modem.command(b"ATDT2242525", expected=b"CONNECT")
        self.assertTrue(peer_ready.wait(2))
        modem.wait_event("connected", 1)
        modem.wait_event("offhook", 1)

        modem.clear_serial()
        modem.write(payload)
        modem.read_until(reply)
        deadline = time.monotonic() + 2
        while payload not in received and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn(payload, received)

        time.sleep(1.2)
        modem.clear_serial()
        modem.write(b"+++")
        modem.read_until(b"\r\nOK\r\n", timeout=3)
        modem.command(b"AT$CONFIG", expected=b"ERROR")
        self.assertEqual(marker.read_bytes(), b"1")

        modem.command(b"ATH", expected=b"NO CARRIER")
        modem.wait_event("disconnected", 1)
        modem.wait_event("onhook", 1)
        release_peer.set()
        peer_thread.join(timeout=2)
        listener.close()

    def test_incoming_ring_autoanswer_and_binary_relay(self):
        listen_port = unused_tcp_port()
        modem = ModemProcess(
            self,
            ("-p", str(listen_port)),
        )
        self.modems.append(modem)
        modem.command(b"ATS0=1")
        modem.wait_event("autoanswer", 1)

        client = socket.create_connection(("127.0.0.1", listen_port), timeout=3)
        try:
            response = modem.read_until(b"CONNECT", timeout=4)
            self.assertIn(b"RING", response)
            modem.wait_event("ring", 1)
            modem.wait_event("connected", 1)
            modem.wait_event("offhook", 1)
            incoming = b"\x7e\xff\x03\xc0!\x00\x7e"
            modem.clear_serial()
            client.sendall(incoming)
            modem.read_until(incoming)
        finally:
            client.close()
        modem.wait_event("disconnected", 1)
        modem.wait_event("onhook", 1)

    def test_second_incoming_call_resets_unfinished_telnet_state(self):
        listen_port = unused_tcp_port()
        modem = ModemProcess(self, ("-p", str(listen_port)))
        self.modems.append(modem)
        modem.command(b"ATS0=1")

        first = socket.create_connection(("127.0.0.1", listen_port), timeout=3)
        modem.read_until(b"CONNECT", timeout=4)
        modem.clear_serial()
        first.sendall(b"\xff\xfa\x18\x01\xff")
        time.sleep(0.1)
        first.close()
        modem.wait_event("disconnected", 1)

        modem.clear_serial()
        second = socket.create_connection(("127.0.0.1", listen_port), timeout=3)
        try:
            modem.read_until(b"CONNECT", timeout=4)
            second.sendall(b"\xff\xfb\x00")
            self.assertEqual(second.recv(3), b"\xff\xfd\x00")
        finally:
            second.close()
        modem.wait_event("disconnected", 1)

    def test_split_telnet_negotiation_and_large_binary_stream(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        dial_port = listener.getsockname()[1]
        modem = self.start_modem(f"-n5551212=127.0.0.1:{dial_port}")

        modem.clear_serial()
        modem.write(b"ATDT5551212\r")
        peer, _ = listener.accept()
        self.addCleanup(peer.close)
        peer.settimeout(4)
        modem.read_until(b"CONNECT", timeout=4)

        def receive_until(expected):
            received = bytearray()
            deadline = time.monotonic() + 4
            while expected not in received and time.monotonic() < deadline:
                received.extend(peer.recv(65536))
            self.assertIn(expected, received)
            return bytes(received)

        def send_split(*parts):
            for part in parts:
                peer.sendall(part)
                time.sleep(0.08)

        initial_negotiation = receive_until(
            b"\xff\xfb\x01\xff\xfb\x00\xff\xfd\x00"
        )
        self.assertEqual(
            initial_negotiation,
            b"\xff\xfb\x01\xff\xfb\x00\xff\xfd\x00",
        )

        send_split(b"\xff", b"\xfb", b"\x00")
        self.assertEqual(receive_until(b"\xff\xfd\x00"), b"\xff\xfd\x00")
        send_split(b"\xff", b"\xfd", b"\x00")
        self.assertEqual(receive_until(b"\xff\xfb\x00"), b"\xff\xfb\x00")

        modem.clear_serial()
        send_split(b"\xff", b"\xfa", b"\x18", b"\x01", b"\xff")
        self.assertFalse(select.select([modem.master_fd], [], [], 0.2)[0])
        self.assertFalse(select.select([peer], [], [], 0)[0])
        peer.sendall(b"\xf0")
        subnegotiation_response = b"\xff\xfa\x18\x00VT100\xff\xf0"
        self.assertEqual(
            receive_until(subnegotiation_response), subnegotiation_response
        )

        modem.clear_serial()
        send_split(b"A", b"\xff", b"\xff", b"B")
        modem.read_until(b"A\xffB")

        large_payload = b"\x7e\xff\x00" * 4096
        encoded_payload = b"\x7e\xff\xff\x00" * 4096
        modem.write(large_payload)
        self.assertEqual(receive_until(encoded_payload), encoded_payload)

        large_reply = b"\x7e\xff\x00" * 4096
        encoded_reply = b"\x7e\xff\xff\x00" * 4096
        modem.clear_serial()
        peer.sendall(encoded_reply)
        modem.read_until(large_reply, timeout=6)
        peer.close()
        modem.wait_event("disconnected", 1)


if __name__ == "__main__":
    unittest.main()
