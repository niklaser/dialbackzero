"""Exercise real member publishing and FTP through the disposable WireGuard peer."""
import ftplib
from html.parser import HTMLParser
import http.client
from http.cookies import SimpleCookie
import io
import socket
import sys
import time
import unittest
from urllib.parse import urlencode

from smoke import SERVER, dns_query, http as raw_http

MEMBERS = "members.retro.net"
PASSWORD = "Integration-only-passphrase-47"


class Tokens(HTMLParser):
    token = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "input" and attrs.get("name") == "csrf_token":
            self.token = attrs.get("value")


class Browser:
    def __init__(self):
        self.cookie = ""

    def request(self, method, path, host=MEMBERS, data=None, content_type=None):
        connection = http.client.HTTPConnection(SERVER, 80, timeout=10)
        connection._http_vsn, connection._http_vsn_str = 10, "HTTP/1.0"
        headers = {"Host": host, "Connection": "close"}
        if host == MEMBERS and self.cookie:
            headers["Cookie"] = self.cookie
        if content_type:
            headers["Content-Type"] = content_type
        connection.request(method, path, body=data, headers=headers)
        response = connection.getresponse()
        body = response.read()
        result = response.status, dict(response.getheaders()), body
        if host == MEMBERS and response.getheader("Set-Cookie"):
            cookie = SimpleCookie()
            cookie.load(response.getheader("Set-Cookie"))
            self.cookie = "; ".join(key + "=" + value.value for key, value in cookie.items())
        connection.close()
        return result

    def token(self, path="/dashboard"):
        status, _, body = self.request("GET", path)
        assert status == 200, (path, status)
        parser = Tokens()
        parser.feed(body.decode("utf-8"))
        assert parser.token, path
        return parser.token

    def form(self, path, fields, token_path="/dashboard"):
        values = {"csrf_token": self.token(token_path), **fields}
        return self.request("POST", path, data=urlencode(values).encode(),
                            content_type="application/x-www-form-urlencoded")

    def signup(self, username):
        status, _, _ = self.form("/signup", {"username": username, "password": PASSWORD}, "/signup")
        assert status == 302, status


class Publishing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.alice, cls.bob = Browser(), Browser()
        cls.alice.signup("alice")
        cls.bob.signup("bob")
        assert cls.alice.form("/claim", {"domain": "alice.com"})[0] == 302
        assert cls.bob.form("/claim", {"domain": "bob.net"})[0] == 302
        for _ in range(30):
            if dns_query("alice.com")[0][3] == 1:
                break
            time.sleep(.1)
        else:
            raise AssertionError("Claimed domain never reached DNS")

    def test_domain_creation_and_directory(self):
        for domain in ("alice.com", "www.alice.com", "bob.net", "www.bob.net"):
            for tcp in (False, True):
                fields, packet = dns_query(domain, tcp=tcp)
                self.assertEqual(fields[1] & 15, 0)
                self.assertEqual(fields[3], 1)
                self.assertIn(socket.inet_aton(SERVER), packet)
            status, _, body = self.alice.request("GET", "/", domain)
            self.assertEqual(status, 200)
            self.assertIn(b"<html", body.lower())
        status, _, body = Browser().request("GET", "/directory")
        self.assertEqual(status, 200)
        self.assertIn(b"http://alice.com/", body)
        self.assertIn(b"http://bob.net/", body)
        self.assertEqual(dns_query("unclaimed.com")[0][1] & 15, 3)
        self.assertEqual(dns_query("not-registered.alice.com")[0][1] & 15, 3)
        self.assertEqual(self.alice.request("GET", "/", "unclaimed.com")[0], 404)

    def test_editor_upload_and_owner_isolation(self):
        content = "<html><body>Published by Alice</body></html>"
        self.assertEqual(self.alice.form("/edit", {"path": "page.html", "content": content})[0], 302)
        self.assertEqual(self.alice.request("GET", "/page.html", "alice.com")[2], content.encode())
        self.assertEqual(self.bob.request("GET", "/page.html", "bob.net")[0], 404)
        self.bob.form("/edit", {"path": "../alice/page.html", "content": "not Alice"})
        self.assertEqual(self.alice.request("GET", "/page.html", "alice.com")[2], content.encode())
        boundary = "DialbackBoundary7372"
        body = ("--" + boundary + '\r\nContent-Disposition: form-data; name="csrf_token"\r\n\r\n'
                + self.alice.token() + "\r\n--" + boundary
                + '\r\nContent-Disposition: form-data; name="file"; filename="C:\\PICTURES\\tiny.gif"'
                + '\r\nContent-Type: image/gif\r\n\r\n').encode() + b"GIF89a\x01\0\x01\0"
        body += ("\r\n--" + boundary + "--\r\n").encode()
        self.assertEqual(self.alice.request("POST", "/upload", data=body,
                         content_type="multipart/form-data; boundary=" + boundary)[0], 302)
        self.assertEqual(self.alice.request("GET", "/tiny.gif", "alice.com")[2], b"GIF89a\x01\0\x01\0")

    def test_forms_require_csrf_and_internal_routes_stay_internal(self):
        status, _, _ = self.alice.request("POST", "/delete", data=b"path=index.html",
                                         content_type="application/x-www-form-urlencoded")
        self.assertEqual(status, 400)
        for path in ("/public/alice.com/", "/legacy/alice/", "/healthz"):
            self.assertEqual(self.alice.request("GET", path)[0], 404)
        for path in ("/.upload-stolen", "/../accounts.sqlite", "/%2e%2e/accounts.sqlite", "/.session-key.lock"):
            self.assertIn(self.alice.request("GET", path, "alice.com")[0], (400, 403, 404))
        self.assertEqual(self.alice.request("GET", "/sites/alice/", "retro.net")[0], 404)
        self.assertEqual(self.alice.request("GET", "/sites/alice/", SERVER)[0], 200)
        self.assertEqual(raw_http("/sites/alice/", host=None)[0], 200)
        with self.assertRaises(OSError):
            socket.create_connection((SERVER, 8080), timeout=.4)

    def test_passive_ftp_shares_browser_files_and_isolates_accounts(self):
        ftp = ftplib.FTP()
        ftp.connect(SERVER, 21, timeout=5)
        try:
            ftp.login("alice", PASSWORD)
            host, port = ftp.makepasv()
            self.assertEqual(host, SERVER)
            self.assertIn(port, range(30000, 30010))
            payload = b"FTP upload from an old computer.\r\n"
            ftp.storbinary("STOR ftp.txt", io.BytesIO(payload))
            self.assertIn("ftp.txt", ftp.nlst())
            received = io.BytesIO()
            ftp.retrbinary("RETR ftp.txt", received.write)
            self.assertEqual(received.getvalue(), payload)
            self.assertEqual(self.alice.request("GET", "/ftp.txt", "alice.com")[2], payload)
            self.assertEqual(self.bob.request("GET", "/ftp.txt", "bob.net")[0], 404)
            with self.assertRaises(ftplib.error_perm):
                ftp.sendcmd("PORT 10,77,0,2,50,50")
            with self.assertRaises(ftplib.error_perm):
                ftp.retrbinary("RETR ../bob/index.html", lambda data: None)
            ftp.rename("ftp.txt", "ftp-renamed.txt")
            self.assertEqual(self.alice.request("GET", "/ftp-renamed.txt", "alice.com")[2], payload)
        finally:
            ftp.close()


def verify_existing():
    browser = Browser()
    assert browser.form("/login", {"username": "alice", "password": PASSWORD}, "/login")[0] == 302
    assert browser.request("GET", "/page.html", "alice.com")[2] == b"<html><body>Published by Alice</body></html>"
    assert b"FTP upload" in browser.request("GET", "/ftp-renamed.txt", "alice.com")[2]
    assert dns_query("alice.com")[0][3] == 1
    print("Accounts, domains, browser files and FTP files survived service recreation.")


if __name__ == "__main__":
    if "--verify-existing" in sys.argv:
        verify_existing()
    else:
        unittest.main(verbosity=2)
