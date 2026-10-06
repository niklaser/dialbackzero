#!/usr/bin/env python3
"""Bounded, CR/LF-tolerant ASCII settings menu used by AT$CONFIG."""

import os
import sys

from dialback_config import ConfigError, load, stage
from hub_status import status as hub_status


class SerialIO:
    def __init__(self, input_fd=None, output_fd=None):
        self.input_fd = sys.stdin.fileno() if input_fd is None else input_fd
        self.output_fd = sys.stdout.fileno() if output_fd is None else output_fd
        self.skip_lf = False

    def write(self, text):
        data = text.replace("\n", "\r\n").encode("ascii", "replace")
        while data:
            data = data[os.write(self.output_fd, data):]

    def readline(self, prompt, maximum=127, secret=False):
        self.write(prompt)
        result = bytearray()
        while True:
            char = os.read(self.input_fd, 1)
            if not char:
                return ""
            if self.skip_lf:
                self.skip_lf = False
                if char == b"\n":
                    continue
            if char in (b"\r", b"\n"):
                self.skip_lf = char == b"\r"
                self.write("\n")
                return result.decode("ascii", "ignore")
            if char in (b"\x08", b"\x7f"):
                if result:
                    result.pop()
                    if not secret:
                        os.write(self.output_fd, b"\b \b")
                continue
            if 32 <= char[0] < 127:
                if len(result) >= maximum:
                    os.write(self.output_fd, b"\x07")
                    continue
                result.extend(char)
                if not secret:
                    os.write(self.output_fd, char)


def _endpoint(io, config, number, name):
    endpoint = config["modem"]["numbers"][number]
    value = io.readline(f"{name} host [{endpoint['host']}]: ", maximum=120)
    if value:
        endpoint["host"] = value
    value = io.readline(f"{name} port [{endpoint['port']}]: ", maximum=5)
    if value:
        endpoint["port"] = int(value)


def run(io=None):
    io = io or SerialIO()
    config = load()
    changed = False
    while True:
        wifi = config["wifi"]
        io.write("\nDIALBACK ZERO SETTINGS\n"
                 f"1. Serial speed: {config['modem']['baud']} bps\n"
                 f"2. Sound: {config['modem']['sound_mode']}\n"
                 f"3. Volume: {config['audio']['volume_percent']}%\n"
                 f"4. Manage Wi-Fi: {'yes' if wifi['managed'] else 'no'}\n"
                 f"5. Wi-Fi enabled: {'yes' if wifi['enabled'] else 'no'}\n"
                 f"6. Wi-Fi name: {wifi['ssid'] or '(none)'}\n"
                 "7. Set Wi-Fi password\n"
                 f"8. Internet 2242525: {config['modem']['numbers']['2242525']['host']}:{config['modem']['numbers']['2242525']['port']}\n"
                 f"9. Hub 777: {'enabled' if config['modem']['numbers']['777']['enabled'] else 'disabled'}\n"
                 "H. Private network status\nS. Save for next restart or boot\nQ. Quit\n")
        choice = io.readline("> ", maximum=1).lower()
        try:
            if choice == "1":
                config["modem"]["baud"] = int(io.readline("Speed (300-115200): ", maximum=6))
                changed = True
            elif choice == "2":
                config["modem"]["sound_mode"] = "off" if config["modem"]["sound_mode"] != "off" else "dial_and_busy"
                changed = True
            elif choice == "3":
                config["audio"]["volume_percent"] = int(io.readline("Volume 0-100: ", maximum=3))
                changed = True
            elif choice == "4":
                wifi["managed"] = not wifi["managed"]
                changed = True
            elif choice == "5":
                wifi["enabled"] = not wifi["enabled"]
                changed = True
            elif choice == "6":
                wifi["ssid"] = io.readline("Wi-Fi name: ", maximum=32)
                changed = True
            elif choice == "7":
                wifi["password"] = io.readline("Wi-Fi password (input hidden): ", maximum=63, secret=True)
                changed = True
            elif choice == "8":
                _endpoint(io, config, "2242525", "Internet")
                changed = True
            elif choice == "9":
                endpoint = config["modem"]["numbers"]["777"]
                endpoint["enabled"] = not endpoint["enabled"]
                changed = True
            elif choice == "h":
                io.write(hub_status(config) + "\n")
                io.write("Dial 777, then open http://retro.net/\n")
            elif choice == "s":
                stage(config)
                io.write("Saved. Restart the device to apply.\n")
                return 0
            elif choice == "q" or not choice:
                if changed:
                    io.write("Changes discarded.\n")
                return 0
            else:
                io.write("Unknown selection.\n")
        except (ConfigError, OSError, ValueError) as exc:
            io.write(f"Not changed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(run())
