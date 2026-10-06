#!/usr/bin/env python3
"""Explicit NetworkManager apply operation; never logs credentials."""

import subprocess
from dialback_config import load

PROFILE = "dialback-zero-wifi"


def apply_wifi(config, runner=subprocess.run):
    """Apply only the profile owned by Dialback Zero, using stdin for its secret."""
    wifi = config["wifi"]
    if not wifi["managed"]:
        return None
    common = {"text": True, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
              "timeout": 40, "check": True}
    shown = runner(["nmcli", "-t", "-f", "NAME", "connection", "show"],
                   text=True, capture_output=True, timeout=20, check=True)
    exists = PROFILE in shown.stdout.splitlines()
    if not wifi["enabled"]:
        if not exists:
            return None
        runner(["nmcli", "connection", "modify", "id", PROFILE,
                "connection.autoconnect", "no"], **common)
        down = dict(common)
        down["check"] = False
        return runner(["nmcli", "--wait", "20", "connection", "down", "id", PROFILE], **down)
    if not exists:
        runner(["nmcli", "connection", "add", "type", "wifi", "ifname", wifi["interface"],
                "con-name", PROFILE, "ssid", wifi["ssid"]], **common)
    else:
        runner(["nmcli", "connection", "modify", "id", PROFILE,
                "802-11-wireless.ssid", wifi["ssid"],
                "connection.interface-name", wifi["interface"]], **common)
    runner(["nmcli", "connection", "modify", "id", PROFILE,
            "connection.autoconnect", "yes"], **common)
    if wifi["password"]:
        runner(["nmcli", "connection", "modify", "id", PROFILE,
                "802-11-wireless-security.key-mgmt", "wpa-psk"], **common)
        secret_input = "802-11-wireless-security.psk:" + wifi["password"] + "\n"
    else:
        runner(["nmcli", "connection", "modify", "id", PROFILE,
                "802-11-wireless-security.key-mgmt", "none"], **common)
        secret_input = None
    up = ["nmcli", "--wait", "30", "connection", "up", "id", PROFILE,
          "ifname", wifi["interface"]]
    if secret_input is not None:
        up += ["passwd-file", "/dev/stdin"]
    return runner(up,
                  input=secret_input, **common)


def main():
    apply_wifi(load())


if __name__ == "__main__":
    main()
