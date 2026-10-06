#!/usr/bin/env python3
"""Launch the native modem using settings activated by the target."""

import math
import os
import subprocess
import sys

from dialback_config import load

MAX_PCM_GAIN_DB = 3


def modem_argv(config, binary="/usr/local/bin/dialback-zero-modem"):
    modem = config["modem"]
    args = [binary, "-d", modem["device"], "-s", str(modem["baud"]),
            "-S", "28800", "-i", "&k0"]
    for number, endpoint in sorted(modem["numbers"].items()):
        if endpoint["enabled"]:
            args += ["-n", f"{number}={endpoint['host']}:{endpoint['port']}"]
    return args


def mixer_argv(config):
    volume = config["audio"]["volume_percent"]
    # Treat the setting as linear signal amplitude relative to the supported
    # maximum. Addressing the mixer in dB avoids device-specific percentage mappings.
    level = ("0.00dB" if volume == 0 else
             f"{MAX_PCM_GAIN_DB + 20 * math.log10(volume / 100):.2f}dB")
    switch = "mute" if volume == 0 else "unmute"
    return ["amixer", "-q", "-c", "Headphones", "--", "sset", "PCM", level, switch]


def apply_mixer(config, runner=subprocess.run):
    try:
        result = runner(mixer_argv(config), check=False, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL, timeout=10)
    except (OSError, subprocess.SubprocessError):
        result = None
    if result is None or result.returncode != 0:
        print("dialback-zero: could not apply Headphones PCM volume; continuing", file=sys.stderr)
        return False
    return True


def main():
    config = load()
    apply_mixer(config)
    environment = os.environ.copy()
    environment.setdefault("DIALBACK_EVENT_SOCKET", "/run/dialback-zero/events.sock")
    environment.setdefault("DIALBACK_CONFIG_MENU", "/usr/local/lib/dialback-zero/config-menu")
    environment["DIALBACK_SOUND_MODE"] = config["modem"]["sound_mode"]
    binary = environment.get("DIALBACK_MODEM_BINARY", "/usr/local/bin/dialback-zero-modem")
    os.execve(binary, modem_argv(config, binary), environment)


if __name__ == "__main__":
    main()
