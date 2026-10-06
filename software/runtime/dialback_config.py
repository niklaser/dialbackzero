#!/usr/bin/env python3
"""Validated configuration and staged persistence for Dialback Zero."""

from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path
import tempfile

CONFIG_PATH = Path(os.environ.get("DIALBACK_CONFIG", "/etc/dialback-zero/config.json"))
STATE_DIR = Path(os.environ.get("DIALBACK_STATE_DIR", "/var/lib/dialback-zero"))
PENDING_PATH = STATE_DIR / "pending-config.json"

DEFAULT_CONFIG = {
    "version": 1,
    "modem": {
        "device": "/dev/serial0", "baud": 38400, "sound_mode": "dial_and_busy",
        "numbers": {
            "2242525": {"enabled": True, "host": "127.0.0.1", "port": 5432},
            "777": {"enabled": False, "host": "127.0.0.1", "port": 6543},
        },
    },
    "ppp": {"local": "172.16.0.1", "peer": "172.16.0.2", "dns": "8.8.4.4"},
    "hub": {"local": "172.16.77.1", "peer": "172.16.77.2",
            "server": "10.77.0.1", "dns": "10.77.0.1"},
    "web": {"bind": "0.0.0.0", "port": 80},
    "audio": {"volume_percent": 70},
    "wifi": {"managed": False, "enabled": False, "ssid": "", "password": "", "interface": "wlan0"},
}


class ConfigError(ValueError):
    pass


def _keys(value, expected, where):
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be an object")
    unknown = set(value) - set(expected)
    if unknown:
        raise ConfigError(f"unknown {where} setting: {sorted(unknown)[0]}")


def _text(value, where, maximum=128, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not value and not allow_empty):
        raise ConfigError(f"invalid {where}")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigError(f"invalid {where}")
    return value


def _validate(config):
    """Return a normalized deep copy or raise ConfigError."""
    _keys(config, DEFAULT_CONFIG, "configuration")
    result = deepcopy(config)
    # Version 1 configurations written before hub support remain usable.
    result.setdefault("hub", deepcopy(DEFAULT_CONFIG["hub"]))
    if result.get("version") != 1:
        raise ConfigError("unsupported configuration version")
    _keys(result["modem"], DEFAULT_CONFIG["modem"], "modem")
    modem = result["modem"]
    if not isinstance(modem["baud"], int) or modem["baud"] not in (300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
        raise ConfigError("invalid modem baud")
    device = _text(modem["device"], "serial device")
    if not device.startswith("/dev/") or ".." in Path(device).parts:
        raise ConfigError("serial device must be an absolute /dev path")
    if modem["sound_mode"] not in ("off", "dial_and_busy"):
        raise ConfigError("invalid sound mode")
    if not isinstance(modem["numbers"], dict) or not modem["numbers"]:
        raise ConfigError("at least one dial number is required")
    if not {"2242525", "777"}.issubset(modem["numbers"]):
        raise ConfigError("phonebook must contain 2242525 and 777")
    if len(modem["numbers"]) > 100:
        raise ConfigError("phonebook is limited to 100 numbers")
    clean_numbers = {}
    for number, endpoint in modem["numbers"].items():
        if not isinstance(number, str) or not number.isdigit() or len(number) > 20:
            raise ConfigError("dial numbers must contain digits only")
        _keys(endpoint, ("enabled", "host", "port"), f"number {number}")
        if not isinstance(endpoint["enabled"], bool):
            raise ConfigError(f"number {number} enabled must be boolean")
        host = _text(endpoint["host"], f"number {number} host", 120)
        if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for c in host):
            raise ConfigError(f"invalid number {number} host")
        port = endpoint["port"]
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ConfigError(f"invalid number {number} port")
        if len(f"{number}={host}:{port}".encode("ascii")) > 127:
            raise ConfigError(f"number {number} endpoint exceeds modem slot")
        clean_numbers[number] = {"enabled": endpoint["enabled"], "host": host, "port": port}
    modem["numbers"] = clean_numbers
    if clean_numbers["777"]["host"] != "127.0.0.1" or clean_numbers["777"]["port"] != 6543:
        raise ConfigError("hub number 777 must use the local hub service (127.0.0.1:6543)")
    _keys(result["ppp"], DEFAULT_CONFIG["ppp"], "ppp")
    for key in ("local", "peer", "dns"):
        try:
            address = ipaddress.ip_address(result["ppp"][key])
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"invalid PPP {key} address") from exc
        if address.version != 4:
            raise ConfigError("PPP addresses must be IPv4")
    if result["ppp"]["local"] == result["ppp"]["peer"]:
        raise ConfigError("PPP local and peer addresses must differ")
    _keys(result["hub"], DEFAULT_CONFIG["hub"], "hub")
    private_ranges = tuple(ipaddress.ip_network(net) for net in
                           ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
    for key in ("local", "peer", "server", "dns"):
        try:
            address = ipaddress.ip_address(result["hub"][key])
        except (ValueError, TypeError) as exc:
            raise ConfigError(f"invalid hub {key} address") from exc
        if address.version != 4 or not any(address in net for net in private_ranges):
            raise ConfigError("hub addresses must be private IPv4 addresses")
    hub = result["hub"]
    if hub["dns"] != hub["server"]:
        raise ConfigError("hub DNS must use the private hub server")
    addresses = [result["ppp"]["local"], result["ppp"]["peer"],
                 hub["local"], hub["peer"], hub["server"]]
    if len(set(addresses)) != len(addresses):
        raise ConfigError("Internet PPP, hub PPP and hub server addresses must be distinct")
    _keys(result["web"], DEFAULT_CONFIG["web"], "web")
    if result["web"]["bind"] not in ("0.0.0.0", "127.0.0.1", result["ppp"]["local"], hub["local"]):
        raise ConfigError("web bind must be local, PPP local, or all interfaces with access checks")
    if not isinstance(result["web"]["port"], int) or not 1 <= result["web"]["port"] <= 65535:
        raise ConfigError("invalid web port")
    _keys(result["audio"], DEFAULT_CONFIG["audio"], "audio")
    volume = result["audio"]["volume_percent"]
    if not isinstance(volume, int) or isinstance(volume, bool) or not 0 <= volume <= 100:
        raise ConfigError("volume must be from 0 through 100 percent")
    _keys(result["wifi"], DEFAULT_CONFIG["wifi"], "wifi")
    wifi = result["wifi"]
    if not isinstance(wifi["managed"], bool) or not isinstance(wifi["enabled"], bool):
        raise ConfigError("Wi-Fi managed and enabled must be boolean")
    _text(wifi["ssid"], "Wi-Fi SSID", 32, allow_empty=not wifi["enabled"])
    _text(wifi["password"], "Wi-Fi password", 63, allow_empty=True)
    _text(wifi["interface"], "Wi-Fi interface", 15)
    if any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in wifi["interface"]):
        raise ConfigError("invalid Wi-Fi interface")
    return result


def validate(config):
    try:
        return _validate(config)
    except KeyError as exc:
        raise ConfigError(f"missing configuration setting: {exc.args[0]}") from exc


def _read(path):
    with path.open(encoding="utf-8") as stream:
        return validate(json.load(stream))


def load(path=CONFIG_PATH):
    path = Path(path)
    return _read(path) if path.exists() else deepcopy(DEFAULT_CONFIG)


def pending_path(config_path=CONFIG_PATH, state_dir=None):
    if state_dir is not None:
        return Path(state_dir) / "pending-config.json"
    if Path(config_path) == CONFIG_PATH:
        return PENDING_PATH
    return Path(config_path).parent / "pending-config.json"


def _atomic_write(path, config, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(validate(config), indent=2, sort_keys=True) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
        stream.write(data)
        temporary = Path(stream.name)
    try:
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stage(config, config_path=CONFIG_PATH, state_dir=None):
    """Validate and save settings for the next service restart or boot."""
    target = pending_path(config_path, state_dir)
    _atomic_write(target, config)
    return target


def activate_pending(config_path=CONFIG_PATH, state_dir=None):
    """Promote staged settings at process startup; return whether changed."""
    config_path = Path(config_path)
    pending = pending_path(config_path, state_dir)
    if not pending.exists():
        return False
    config = _read(pending)
    _atomic_write(config_path, config)
    pending.unlink()
    return True


def redacted(config):
    result = deepcopy(config)
    if result["wifi"]["password"]:
        result["wifi"]["password"] = "********"
    return result
