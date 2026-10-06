from __future__ import annotations

import io
import atexit
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock


SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR))

# app.py exposes a deployment app at import time. Give that instance private
# test storage rather than touching the deployment defaults.
_IMPORT_STORAGE = Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, _IMPORT_STORAGE, True)
os.environ["DBZ_DATA_DIR"] = str(_IMPORT_STORAGE / "data")
os.environ["DBZ_DNS_DIR"] = str(_IMPORT_STORAGE / "dns")
os.environ["DBZ_MEMBERS_HOST"] = "members.retro.net"

from sites.app import create_app  # noqa: E402
from sites.store import (  # noqa: E402
    DomainUnavailable,
    DuplicateUser,
    QuotaExceeded,
    Store,
    UnsafePath,
    ValidationError,
)


def form_token(response) -> str:
    match = re.search(rb'name="csrf_token" value="([^"]+)"', response.data)
    if not match:
        raise AssertionError("form has no CSRF token")
    return match.group(1).decode("ascii")


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.data = root / "data"
        self.dns = root / "dns"
        self.store = Store(self.data, self.dns)

    def tearDown(self):
        self.temporary.cleanup()

    def test_accounts_are_case_insensitive_and_persistent(self):
        self.assertEqual(self.store.register("Alice_1", "long password"), "alice_1")
        self.assertTrue(self.store.authenticate("ALICE_1", "long password"))
        self.assertFalse(self.store.authenticate("alice_1", "wrong password"))
        with self.assertRaises(DuplicateUser):
            self.store.register("ALICE_1", "another password")

        reopened = Store(self.data, self.dns)
        self.assertTrue(reopened.authenticate("alice_1", "long password"))
        self.assertEqual(stat.S_IMODE(reopened.db_path.stat().st_mode), 0o600)

    def test_registration_validation(self):
        for username in ("ab", "space name", "dot.name", "åke", "anonymous"):
            with self.subTest(username=username), self.assertRaises(ValidationError):
                self.store.register(username, "password8")
        with self.assertRaises(ValidationError):
            self.store.register("valid_name", "short")

    def test_domain_claim_writes_alias_and_blocks_reserved_and_related_names(self):
        self.store.register("alice", "password8")
        self.store.register("bob", "password8")
        domain = self.store.claim_domain("alice", "Example.COM")
        self.assertEqual(domain, "example.com")
        self.assertEqual(self.store.resolve_domain("example.com"), "alice")
        self.assertEqual(self.store.resolve_domain("www.example.com"), "alice")
        self.assertEqual(
            self.store.dns_path.read_text("ascii"),
            "10.77.0.1 example.com www.example.com\n",
        )
        self.assertEqual(stat.S_IMODE(self.store.dns_path.stat().st_mode), 0o644)

        for unavailable in ("sub.example.com", "retro.net", "members.retro.net", "home.arpa"):
            with self.subTest(domain=unavailable), self.assertRaises(DomainUnavailable):
                self.store.claim_domain("bob", unavailable)
        with self.assertRaises(ValidationError):
            self.store.validate_domain("www.other.net")
        with self.assertRaises(ValidationError):
            self.store.validate_domain("127.0.0.1")

    def test_safe_paths_reject_escape_hidden_and_symlinks(self):
        self.store.register("alice", "password8")
        self.store.write_file("alice", "pages/hello.txt", b"hello")
        self.assertEqual(self.store.read_file("alice", "pages/hello.txt"), b"hello")
        for path in ("../outside", "/etc/passwd", ".secret", "pages/.secret", r"..\outside"):
            with self.subTest(path=path), self.assertRaises(UnsafePath):
                self.store.allowed_path("alice", path)

        home = self.store.user_home("alice")
        (Path(self.temporary.name) / "outside").write_text("outside")
        os.symlink(Path(self.temporary.name) / "outside", home / "linked")
        with self.assertRaises(UnsafePath):
            self.store.read_file("alice", "linked")

    def test_file_size_account_size_and_count_quotas_cover_replacement(self):
        self.store.register("alice", "password8")
        with mock.patch("sites.store.MAX_FILE_BYTES", 5):
            with self.assertRaises(QuotaExceeded):
                self.store.write_file("alice", "large.bin", b"123456")

        with (
            mock.patch("sites.store.MAX_FILE_BYTES", 20),
            mock.patch("sites.store.MAX_ACCOUNT_BYTES", 6),
            mock.patch("sites.store.MAX_FILES", 1),
        ):
            self.store.write_file("alice", "one.txt", b"1234")
            self.store.write_file("alice", "one.txt", b"123456")
            with self.assertRaises(QuotaExceeded):
                self.store.write_file("alice", "two.txt", b"")
            with self.assertRaises(QuotaExceeded):
                self.store.write_file("alice", "one.txt", b"1234567")

        with mock.patch("sites.store.MAX_FILES", 0):
            with self.assertRaises(QuotaExceeded):
                self.store.write_file("alice", "unused/new.txt", b"")
            self.assertFalse((self.store.user_home("alice") / "unused").exists())

    def test_directory_creation_is_safe_and_bounded(self):
        self.store.register("alice", "password8")
        self.store.mkdir("alice", "images")
        self.assertTrue((self.store.user_home("alice") / "images").is_dir())
        with mock.patch("sites.store.MAX_DIRECTORIES", 1):
            with self.assertRaises(QuotaExceeded):
                self.store.mkdir("alice", "more")
        with self.assertRaises(FileNotFoundError):
            self.store.mkdir("alice", "missing/child")

    def test_public_reader_maps_only_claimed_domain_and_directory_index(self):
        self.store.register("alice", "password8")
        self.store.claim_domain("alice", "alice.net")
        self.store.write_file("alice", "docs/index.html", b"docs")
        path, mime, size = self.store.open_public("www.alice.net", "docs/")
        self.assertEqual(path.name, "index.html")
        self.assertEqual(path.read_bytes(), b"docs")
        self.assertEqual(mime, "text/html")
        self.assertEqual(size, 4)
        with self.assertRaises(FileNotFoundError):
            self.store.open_public("unclaimed.net", "index.html")


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-secret",
                "DBZ_DATA_DIR": str(root / "data"),
                "DBZ_DNS_DIR": str(root / "dns"),
                "DBZ_MEMBERS_HOST": "members.retro.net",
            }
        )
        self.client = self.app.test_client()
        self.host = {"Host": "members.retro.net"}

    def tearDown(self):
        self.temporary.cleanup()

    def signup(self, client, username: str, password: str = "password8"):
        token = form_token(client.get("/signup", headers=self.host))
        return client.post(
            "/signup",
            data={"csrf_token": token, "username": username, "password": password},
            headers=self.host,
            follow_redirects=True,
        )

    def claim(self, client, domain: str):
        page = client.get("/dashboard", headers=self.host)
        return client.post(
            "/claim",
            data={"csrf_token": form_token(page), "domain": domain},
            headers=self.host,
            follow_redirects=True,
        )

    def test_forged_host_and_missing_csrf_are_rejected(self):
        self.assertEqual(self.client.get("/login", headers={"Host": "evil.example"}).status_code, 400)
        response = self.client.post(
            "/signup",
            data={"username": "alice", "password": "password8"},
            headers=self.host,
        )
        self.assertEqual(response.status_code, 400)
        page = self.client.get("/signup", headers=self.host)
        response = self.client.post(
            "/signup",
            data={"csrf_token": "å", "username": "alice", "password": "password8"},
            headers=self.host,
        )
        self.assertEqual(response.status_code, 400)

    def test_signup_claim_edit_upload_delete_and_persistence(self):
        self.assertEqual(self.signup(self.client, "alice").status_code, 200)
        claimed = self.claim(self.client, "alice.com")
        self.assertIn(b"http://alice.com/", claimed.data)

        edit = self.client.get("/edit?path=about.html", headers=self.host)
        saved = self.client.post(
            "/edit",
            data={
                "csrf_token": form_token(edit),
                "path": "about.html",
                "content": "<h1>About Alice</h1>",
            },
            headers=self.host,
            follow_redirects=True,
        )
        self.assertIn(b"Saved about.html", saved.data)

        dashboard = self.client.get("/dashboard", headers=self.host)
        uploaded = self.client.post(
            "/upload",
            data={
                "csrf_token": form_token(dashboard),
                "path": "files/note.txt",
                "file": (io.BytesIO(b"uploaded"), "note.txt"),
            },
            headers=self.host,
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"Uploaded files/note.txt", uploaded.data)

        dashboard = self.client.get("/dashboard", headers=self.host)
        token = form_token(dashboard)
        boundary = b"----dialback-zero-test-boundary"
        body = b"\r\n".join(
            [
                b"--" + boundary,
                b'Content-Disposition: form-data; name="csrf_token"',
                b"",
                token.encode("ascii"),
                b"--" + boundary,
                b'Content-Disposition: form-data; name="path"',
                b"",
                b"",
                b"--" + boundary,
                b'Content-Disposition: form-data; name="file"; filename="C:\\My Documents\\cafe.html"',
                b"Content-Type: text/html",
                b"",
                b"<p>caf\xe9</p>",
                b"--" + boundary + b"--",
                b"",
            ]
        )
        windows_upload = self.client.post(
            "/upload",
            data=body,
            headers=self.host,
            content_type="multipart/form-data; boundary=" + boundary.decode("ascii"),
            follow_redirects=True,
        )
        self.assertIn(b"Uploaded cafe.html", windows_upload.data)
        editor = self.client.get("/edit?path=cafe.html", headers=self.host)
        self.assertIn("café".encode(), editor.data)
        self.assertIn(b'value="windows-1252" selected', editor.data)

        question = self.client.get("/edit?path=question%3F%23.txt", headers=self.host)
        self.client.post(
            "/edit",
            data={
                "csrf_token": form_token(question),
                "path": "question?#.txt",
                "encoding": "utf-8",
                "content": "url",
            },
            headers=self.host,
        )
        dashboard = self.client.get("/dashboard", headers=self.host)
        self.assertIn(b"question%3F%23.txt", dashboard.data)

        public = self.client.get(
            "/public/alice.com/about.html",
            headers={"Host": "alice.com"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(public.status_code, 200)
        self.assertEqual(public.data, b"<h1>About Alice</h1>")
        public.close()
        self.assertEqual(
            self.client.get(
                "/public/alice.com/about.html",
                headers={"Host": "alice.com"},
                environ_overrides={"REMOTE_ADDR": "10.77.0.2"},
            ).status_code,
            404,
        )
        self.assertEqual(self.client.get("/about.html", headers=self.host).status_code, 404)

        dashboard = self.client.get("/dashboard", headers=self.host)
        deleted = self.client.post(
            "/delete",
            data={"csrf_token": form_token(dashboard), "path": "files/note.txt"},
            headers=self.host,
            follow_redirects=True,
        )
        self.assertIn(b"Deleted files/note.txt", deleted.data)

        reopened = Store(self.app.config["DBZ_DATA_DIR"], self.app.config["DBZ_DNS_DIR"])
        self.assertEqual(reopened.read_file("alice", "about.html"), b"<h1>About Alice</h1>")

    def test_account_ownership_directory_and_legacy_reader(self):
        self.signup(self.client, "alice")
        self.claim(self.client, "alice.net")
        page = self.client.get("/edit?path=private.txt", headers=self.host)
        self.client.post(
            "/edit",
            data={"csrf_token": form_token(page), "path": "private.txt", "content": "alice-only"},
            headers=self.host,
        )

        bob = self.app.test_client()
        self.signup(bob, "bob")
        self.claim(bob, "bob.com")
        bob_edit = bob.get("/edit?path=private.txt", headers=self.host)
        self.assertEqual(bob_edit.status_code, 200)
        self.assertNotIn(b"alice-only", bob_edit.data)

        directory = bob.get("/directory", headers=self.host)
        self.assertIn(b"alice.net", directory.data)
        self.assertIn(b"bob.com", directory.data)

        legacy = bob.get(
            "/legacy/alice/private.txt",
            headers={"Host": "10.77.0.1"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        self.assertEqual(legacy.data, b"alice-only")
        legacy.close()

    def test_login_and_logout_are_csrf_protected(self):
        self.signup(self.client, "alice")
        dashboard = self.client.get("/dashboard", headers=self.host)
        response = self.client.post(
            "/logout",
            data={"csrf_token": form_token(dashboard)},
            headers=self.host,
            follow_redirects=True,
        )
        self.assertIn(b"Sign in", response.data)

        login = self.client.get("/login", headers=self.host)
        response = self.client.post(
            "/login",
            data={
                "csrf_token": form_token(login),
                "username": "ALICE",
                "password": "password8",
            },
            headers=self.host,
            follow_redirects=True,
        )
        self.assertIn(b"Signed in as", response.data)

    def test_slow_ftp_lock_returns_fast_503_but_public_site_remains_available(self):
        self.signup(self.client, "alice")
        self.claim(self.client, "alice.net")
        store = self.app.extensions["dbz_store"]
        locked = threading.Event()
        release = threading.Event()

        def hold_ftp_lock():
            with store.user_lock("alice"):
                locked.set()
                release.wait(3)

        thread = threading.Thread(target=hold_ftp_lock)
        thread.start()
        self.assertTrue(locked.wait(1))
        try:
            started = time.monotonic()
            response = self.client.get("/dashboard", headers=self.host)
            self.assertEqual(response.status_code, 503)
            self.assertLess(time.monotonic() - started, 0.5)
            public = self.client.get(
                "/public/alice.net/",
                headers={"Host": "alice.net"},
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(public.status_code, 200)
            public.close()
        finally:
            release.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.client.get("/dashboard", headers=self.host).status_code, 200)

    def test_session_secret_is_persistent_and_private(self):
        root = Path(self.temporary.name) / "secret-test"
        config = {
            "TESTING": True,
            "DBZ_DATA_DIR": str(root / "data"),
            "DBZ_DNS_DIR": str(root / "dns"),
            "DBZ_MEMBERS_HOST": "members.retro.net",
        }
        first = create_app(config)
        second = create_app(config)
        self.assertEqual(first.secret_key, second.secret_key)
        self.assertEqual(stat.S_IMODE((root / "data" / "session.key").stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
