import os
from pathlib import Path
import sys
import threading
import tty
import unittest

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
sys.path.insert(0, str(RUNTIME))

import config_menu
import led_daemon


class MenuTests(unittest.TestCase):
    def test_real_pty_cr_backspace_and_hidden_password(self):
        master, slave = os.openpty()
        tty.setraw(slave)
        io = config_menu.SerialIO(slave, slave)
        result = []
        def read_values():
            result.append(io.readline("Name: ", maximum=8))
            result.append(io.readline("Password: ", maximum=8, secret=True))
        thread = threading.Thread(target=read_values, daemon=True)
        thread.start()
        os.write(master, b"abx\x08cd\rsecret\r\n")
        thread.join(2)
        os.set_blocking(master, False)
        output = bytearray()
        try:
            while True:
                output.extend(os.read(master, 4096))
        except BlockingIOError:
            pass
        finally:
            os.close(master)
            os.close(slave)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ["abcd", "secret"])
        self.assertIn(b"abx", output)
        self.assertNotIn(b"secret", output)


class FakeLights:
    def __init__(self):
        self.values = {}
    def set(self, name, value):
        self.values[name] = value


class LedTests(unittest.TestCase):
    def test_snapshot_values_authoritatively_clear_modem_leds(self):
        lights = FakeLights()
        led_daemon.apply_event(lights, {"event": "connected", "value": 1})
        led_daemon.apply_event(lights, {"event": "connected", "value": 0})
        led_daemon.apply_event(lights, {"event": "offhook", "value": 1})
        led_daemon.apply_event(lights, {"event": "offhook", "value": 0})
        self.assertFalse(lights.values["CD"])
        self.assertFalse(lights.values["OH"])
        self.assertNotIn("NET", lights.values)

    def test_pin_map_never_uses_reserved_carrier_pins(self):
        self.assertTrue(set(led_daemon.PINS.values()).isdisjoint({3, 4, 8, 9, 10, 11, 12, 13, 14, 15, 26}))


if __name__ == "__main__":
    unittest.main()
