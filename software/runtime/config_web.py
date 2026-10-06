#!/usr/bin/env python3
"""Local-only, no-JavaScript Dialback Zero configuration server."""

from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import os
import secrets
from urllib.parse import parse_qs, urlsplit

from dialback_config import CONFIG_PATH, STATE_DIR, ConfigError, load, stage
from network import apply_wifi
from hub_status import status as hub_status, probe as probe_hub
import update_manager

MAX_BODY = 8192
CSRF_TOKEN = secrets.token_urlsafe(32)
UPDATE_WRITE_PHASES = {"downloading", "installing", "verifying"}
UPDATE_PHASE_LABELS = {
    "idle": "Ready", "checking": "Checking for updates", "available": "Update available",
    "downloading": "Downloading update", "installing": "Installing update",
    "verifying": "Verifying update", "succeeded": "Update complete",
    "rolled_back": "Previous version restored", "failed": "Update failed",
}


def client_allowed(address, config):
    try:
        client = ipaddress.ip_address(address)
    except ValueError:
        return False
    return client.is_loopback or str(client) in {config["ppp"]["peer"], config["hub"]["peer"]}


def host_allowed(value, config):
    if value is None:  # Legal for old HTTP/1.0 clients; source checks still apply.
        return True
    if not value or any(c in value for c in "\r\n/@"):
        return False
    try:
        parsed = urlsplit("//" + value)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    if port not in (None, config["web"]["port"]):
        return False
    return host in {"localhost", "127.0.0.1", "[::1]", "::1", config["ppp"]["local"], config["hub"]["local"]}


def update_from_form(current, fields):
    """Return a validated staged config. Empty password means keep current."""
    result = {key: (value.copy() if isinstance(value, dict) else value) for key, value in current.items()}
    result["modem"] = current["modem"].copy()
    result["modem"]["numbers"] = {key: value.copy() for key, value in current["modem"]["numbers"].items()}
    result["wifi"] = current["wifi"].copy()
    result["audio"] = current["audio"].copy()
    result["audio"]["volume_percent"] = int(fields.get("volume", [""])[0])
    result["modem"]["sound_mode"] = fields.get("sound_mode", [""])[0]
    result["modem"]["baud"] = int(fields.get("baud", [""])[0])
    result["wifi"]["managed"] = fields.get("wifi_managed", [""])[0] == "yes"
    result["wifi"]["enabled"] = fields.get("wifi_enabled", [""])[0] == "yes"
    result["wifi"]["ssid"] = fields.get("wifi_ssid", [""])[0]
    password = fields.get("wifi_password", [""])[0]
    if password:
        result["wifi"]["password"] = password
    for number in ("2242525", "777"):
        endpoint = result["modem"]["numbers"][number]
        endpoint["enabled"] = fields.get("number_" + number, [""])[0] == "yes"
        if number == "2242525":
            endpoint["host"] = fields.get("host_" + number, [""])[0]
            endpoint["port"] = int(fields.get("port_" + number, [""])[0])
    from dialback_config import validate
    return validate(result)


class ConfigHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "DialbackZero/1"

    def log_message(self, format_string, *args):
        # Never include request bodies or credentials; BaseHTTPRequestHandler only logs the request line.
        super().log_message(format_string, *args)

    def _config(self):
        return load(self.server.config_path)

    def _authorized(self, config):
        return client_allowed(self.client_address[0], config) and host_allowed(self.headers.get("Host"), config)

    def _updates(self):
        return getattr(self.server, "update_backend", update_manager)

    def _update_status(self):
        try:
            return self._updates().get_status()
        except (update_manager.UpdateError, OSError):
            return {"installed_version": "Unknown", "available_version": None, "phase": "failed",
                    "message": "Update status is unavailable. Try again before changing settings.",
                    "unavailable": True}

    def _updates_body(self, status):
        phase = status["phase"]
        body = ('<HR><H2>Software updates</H2>'
                f'<P>Installed version: {escape(str(status["installed_version"]))}</P>'
                f'<P>Latest version: {escape(str(status["available_version"] or "Not known"))}</P>'
                f'<P>Status: {escape(UPDATE_PHASE_LABELS.get(phase, "Unknown"))}. '
                f'{escape(status["message"])}</P>'
                '<P>Updates preserve your settings and update Dialback Zero software only.</P>')
        if phase not in UPDATE_WRITE_PHASES | {"checking"}:
            body += ('<FORM METHOD="POST" ACTION="/updates/check">'
                     f'<INPUT TYPE="hidden" NAME="csrf" VALUE="{escape(self.server.csrf)}">'
                     '<P><INPUT TYPE="submit" VALUE="Check for updates"></P></FORM>')
        if phase == "available" and status["available_version"]:
            body += ('<P>Installing disconnects the current call. Reconnect afterward and open '
                     'the settings page to see the result.</P>'
                     '<FORM METHOD="POST" ACTION="/updates/install">'
                     f'<INPUT TYPE="hidden" NAME="csrf" VALUE="{escape(self.server.csrf)}">'
                     '<P><INPUT TYPE="submit" VALUE="Install update"></P></FORM>')
        if phase in UPDATE_WRITE_PHASES:
            body += ('<P>Settings and Wi-Fi changes are paused during the update. '
                     'The current call may disconnect. Reconnect afterward to see the result.</P>')
        return body + '<P><A HREF="/updates">Refresh update status</A> | <A HREF="/">Settings</A></P>'

    def _reply(self, status, title, body):
        page = ("<!DOCTYPE HTML PUBLIC \"-//W3C//DTD HTML 3.2 Final//EN\">\n"
                f"<HTML><HEAD><TITLE>{escape(title)}</TITLE></HEAD>"
                f"<BODY><H1>{escape(title)}</H1>{body}</BODY></HTML>\n").encode("ascii", "xmlcharrefreplace")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=us-ascii")
        self.send_header("Content-Length", str(len(page)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(page)

    def do_GET(self):
        config = self._config()
        if not self._authorized(config):
            self._reply(403, "Access denied", "<P>Use localhost or the local PPP link.</P>")
            return
        if self.path not in ("/", "/updates"):
            self._reply(404, "Not found", "")
            return
        update_status = self._update_status()
        if self.path == "/updates":
            self._reply(200, "Dialback Zero updates", self._updates_body(update_status))
            return
        if update_status["phase"] in UPDATE_WRITE_PHASES or update_status.get("unavailable"):
            self._reply(200, "Dialback Zero settings", self._updates_body(update_status))
            return
        modem, wifi = config["modem"], config["wifi"]
        checked = lambda value: " CHECKED" if value else ""
        selected = lambda value: " SELECTED" if value else ""
        baud_options = "".join(f'<OPTION VALUE="{baud}"{selected(baud == modem["baud"])}>{baud}</OPTION>'
                               for baud in (300, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200))
        body = ("<P>Changes are staged. Restart the device to use them.</P>"
                '<FORM METHOD="POST" ACTION="/save">'
                f'<INPUT TYPE="hidden" NAME="csrf" VALUE="{escape(self.server.csrf)}">'
                f'<P>Serial speed <SELECT NAME="baud">{baud_options}</SELECT> bps</P>'
                f'<P>Sound <SELECT NAME="sound_mode"><OPTION VALUE="dial_and_busy"{selected(modem["sound_mode"] == "dial_and_busy")}>Dial and busy</OPTION>'
                f'<OPTION VALUE="off"{selected(modem["sound_mode"] == "off")}>Off</OPTION></SELECT></P>'
                f'<P>Volume (0-100) <INPUT NAME="volume" SIZE="3" MAXLENGTH="3" VALUE="{config["audio"]["volume_percent"]}"></P>'
                f'<P><INPUT TYPE="checkbox" NAME="number_2242525" VALUE="yes"{checked(modem["numbers"]["2242525"]["enabled"])}> Internet 2242525: '
                f'<INPUT NAME="host_2242525" SIZE="24" VALUE="{escape(modem["numbers"]["2242525"]["host"], quote=True)}">:'
                f'<INPUT NAME="port_2242525" SIZE="5" VALUE="{modem["numbers"]["2242525"]["port"]}"></P>'
                f'<P><INPUT TYPE="checkbox" NAME="number_777" VALUE="yes"{checked(modem["numbers"]["777"]["enabled"])}> Private retro network 777</P>'
                f'<P><INPUT TYPE="checkbox" NAME="wifi_managed" VALUE="yes"{checked(wifi["managed"])}> Let Dialback Zero manage its own Wi-Fi profile</P>'
                f'<P><INPUT TYPE="checkbox" NAME="wifi_enabled" VALUE="yes"{checked(wifi["enabled"])}> Enable Wi-Fi</P>'
                f'<P>Wi-Fi name <INPUT NAME="wifi_ssid" MAXLENGTH="32" VALUE="{escape(wifi["ssid"], quote=True)}"></P>'
                '<P>New Wi-Fi password <INPUT TYPE="password" NAME="wifi_password" MAXLENGTH="63" VALUE=""> (leave blank to keep it)</P>'
                '<P><INPUT TYPE="submit" VALUE="Stage settings"></P></FORM>'
                '<HR><FORM METHOD="POST" ACTION="/apply-network">'
                f'<INPUT TYPE="hidden" NAME="csrf" VALUE="{escape(self.server.csrf)}">'
                '<P><INPUT TYPE="submit" VALUE="Apply active Wi-Fi now"> This can end the current connection.</P></FORM>'
                '<HR><H2>Private retro network</H2>'
                f'<P>{escape(hub_status(config))}</P>'
                '<P>A handshake confirms the tunnel peer, not web or DNS availability.</P>'
                '<P>Dial 777, then open <A HREF="http://retro.net/">retro.net</A>.</P>'
                f'<P>Settings over this connection: http://{escape(config["hub"]["local"])}/</P>'
                '<FORM METHOD="POST" ACTION="/test-hub">'
                f'<INPUT TYPE="hidden" NAME="csrf" VALUE="{escape(self.server.csrf)}">'
                '<INPUT TYPE="submit" VALUE="Test private web and DNS"></FORM>')
        self._reply(200, "Dialback Zero settings", body + self._updates_body(update_status))

    def do_POST(self):
        config = self._config()
        if not self._authorized(config):
            self._reply(403, "Access denied", "")
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY or self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/x-www-form-urlencoded":
            self._reply(413, "Invalid request", "<P>The form request was missing or too large.</P>")
            return
        try:
            fields = parse_qs(self.rfile.read(length).decode("ascii"), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=30)
        except (UnicodeDecodeError, ValueError):
            self._reply(400, "Invalid request", "")
            return
        token = fields.get("csrf", [""])
        if len(token) != 1 or not secrets.compare_digest(token[0], self.server.csrf):
            self._reply(403, "Invalid form token", "")
            return
        if self.path in ("/save", "/apply-network"):
            update_status = self._update_status()
            if update_status["phase"] in UPDATE_WRITE_PHASES or update_status.get("unavailable"):
                self._reply(409, "Settings changes paused", self._updates_body(update_status))
                return
        if self.path in ("/updates/check", "/updates/install"):
            if set(fields) != {"csrf"}:
                self._reply(400, "Invalid update request", "<P>Use the update buttons on the settings page.</P>")
                return
            try:
                if self.path == "/updates/check":
                    self._updates().request_check()
                    message = "The update check is running in the background."
                else:
                    status = self._update_status()
                    if status["phase"] != "available" or not status["available_version"]:
                        self._reply(409, "Update not available", self._updates_body(status))
                        return
                    self._updates().request_install()
                    message = ("The update is running in the background. The current call will disconnect. "
                               "Reconnect afterward and open the settings page to see the result.")
            except update_manager.UpdateError as exc:
                self._reply(409, "Update not started", f'<P>{escape(str(exc))}</P>'
                            '<P><A HREF="/updates">Update status</A></P>')
                return
            except OSError:
                self._reply(503, "Update not started", '<P>Could not save the update request. '
                            'Check the device and try again.</P><P><A HREF="/updates">Update status</A></P>')
                return
            self._reply(202, "Update request accepted", f'<P>{message}</P>'
                        '<P><A HREF="/updates">Refresh update status</A></P>')
            return
        if self.path == "/save":
            try:
                updated = update_from_form(config, fields)
                stage(updated, self.server.config_path, self.server.state_dir)
            except (ConfigError, KeyError, ValueError, OSError) as exc:
                self._reply(400, "Settings not saved", f"<P>{escape(str(exc))}</P>")
                return
            self._reply(200, "Settings staged", "<P>Restart the device to apply them.</P><P><A HREF=\"/\">Back</A></P>")
        elif self.path == "/test-hub":
            results = probe_hub(config)
            body = "<UL>" + "".join(
                f"<LI>{escape(name)}: {'OK' if ok else 'Failed'} - {escape(detail)}</LI>"
                for name, ok, detail in results) + '</UL><P><A HREF="/">Back</A></P>'
            self._reply(200, "Private network test", body)
        elif self.path == "/apply-network":
            try:
                apply_wifi(config)
            except Exception:
                self._reply(500, "Wi-Fi apply failed", "<P>See the service log for a non-secret diagnostic.</P>")
                return
            self._reply(200, "Wi-Fi applied", '<P><A HREF="/">Back</A></P>')
        else:
            self._reply(404, "Not found", "")

    def do_PUT(self):
        self._reply(405, "Method not allowed", "")


class ConfigServer(HTTPServer):
    def __init__(self, address, handler=ConfigHandler, config_path=None, state_dir=None, csrf=None,
                 update_backend=None):
        self.config_path = config_path or CONFIG_PATH
        self.state_dir = state_dir or STATE_DIR
        self.csrf = csrf or CSRF_TOKEN
        self.update_backend = update_backend if update_backend is not None else update_manager
        super().__init__(address, handler)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(5)
        return request, address


def main():
    config = load()
    server = ConfigServer((config["web"]["bind"], config["web"]["port"]))
    server.serve_forever()


if __name__ == "__main__":
    main()
