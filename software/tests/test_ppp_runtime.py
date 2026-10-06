import copy
import os
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))

import dialback_config
import ppp_wrapper

ECHO = [sys.executable, "-u", "-c", "import os\nwhile True:\n d=os.read(0,4096)\n if not d: break\n os.write(1,d)\n"]


class PPPTests(unittest.TestCase):
    def test_pppd_uses_validated_config_and_separate_diagnostic_fd(self):
        value = copy.deepcopy(dialback_config.DEFAULT_CONFIG)
        value["ppp"].update(local="10.44.0.1", peer="10.44.0.2", dns="1.1.1.1")
        command = ppp_wrapper.pppd_command(value, "internet")
        self.assertIn("10.44.0.1:10.44.0.2", command)
        self.assertEqual(command[-2:], ["logfd", "2"])
        with mock.patch.object(ppp_wrapper.subprocess, "Popen") as popen:
            ppp_wrapper.spawn_pppd(91, "internet", command)
        self.assertEqual(popen.call_args.kwargs["stdin"], 91)
        self.assertEqual(popen.call_args.kwargs["stdout"], 91)
        self.assertIsNone(popen.call_args.kwargs["stderr"])
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_binary_relay_preserves_all_octets_and_reaps_child(self):
        try:
            server, client = socket.socketpair()
        except PermissionError:
            self.skipTest("socket creation denied by test sandbox")
        stop = threading.Event()
        failures = []
        thread = threading.Thread(target=lambda: ppp_wrapper.handle_session(server, "internet", stop, ECHO), daemon=True)
        thread.start()
        client.settimeout(5)
        payload = bytes(range(256)) * 16
        denied = False
        try:
            try:
                client.sendall(payload)
                received = bytearray()
                while len(received) < len(payload):
                    received.extend(client.recv(len(payload) - len(received)))
                self.assertEqual(bytes(received), payload)
            except PermissionError:
                denied = True
        finally:
            client.close()
            stop.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        if denied:
            self.skipTest("socket I/O denied by test sandbox")



if __name__ == "__main__":
    unittest.main()
