"""Exercise the real passive FTP protocol against temporary site storage."""

from contextlib import contextmanager
import ftplib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

SITES = Path(__file__).resolve().parents[1] / "sites"
sys.path.insert(0, str(SITES))

try:
    import pyftpdlib
    from store import Store, MAX_ACCOUNT_BYTES, MAX_FILE_BYTES, MAX_FILES, MAX_DIRECTORIES
except ImportError:
    pyftpdlib = None


@unittest.skipUnless(pyftpdlib, "pyftpdlib and Flask/Werkzeug are required")
class FTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = Store(self.base / "data", self.base / "dns")
        self.store.register("alice", "password-alice")
        self.store.register("bobby", "password-bobby")
        # Reserve an arbitrary test range without depending on production ports.
        reservations = [socket.socket() for _ in range(10)]
        for reservation in reservations:
            reservation.bind(("127.0.0.1", 0))
        ports = [reservation.getsockname()[1] for reservation in reservations]
        for reservation in reservations:
            reservation.close()
        self.passive_ports = ports
        self.logs = tempfile.TemporaryFile()
        self.addCleanup(self.logs.close)
        code = """
import json, sys
from ftp import create_server
from store import Store
server = create_server(Store(sys.argv[1], sys.argv[2]), bind='127.0.0.1', port=0,
    passive_address='127.0.0.1', passive_ports=json.loads(sys.argv[3]))
server.handler.auth_failed_timeout = 0.05
print(json.dumps(server.socket.getsockname()), flush=True)
server.serve_forever(timeout=0.05)
"""
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-c", code, str(self.base / "data"), str(self.base / "dns"),
             json.dumps(ports)], cwd=SITES, stdout=subprocess.PIPE, stderr=self.logs, text=True,
        )
        self.addCleanup(self.stop_server)
        line = self.process.stdout.readline()
        if not line:
            self.logs.seek(0)
            self.fail(self.logs.read().decode())
        self.address = json.loads(line)

    def stop_server(self):
        self.process.terminate()
        self.process.wait(timeout=5)
        self.process.stdout.close()

    @contextmanager
    def client(self, username="alice", password=None):
        ftp = ftplib.FTP()
        ftp.connect(*self.address, timeout=5)
        try:
            ftp.login(username, password or "password-" + username.lower())
            yield ftp
        finally:
            ftp.close()

    def read(self, ftp, name):
        content = io.BytesIO()
        ftp.retrbinary("RETR " + name, content.write)
        return content.getvalue()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("Timed out waiting for FTP transfer cleanup")

    def test_shared_credentials_files_and_standard_operations(self):
        self.store.write_file("alice", "web.html", b"written by web editor")
        with self.client("ALICE") as ftp:
            host, port = ftp.makepasv()
            self.assertEqual(host, "127.0.0.1")
            self.assertIn(port, self.passive_ports)
            self.assertEqual(self.read(ftp, "web.html"), b"written by web editor")
            ftp.mkd("images")
            ftp.cwd("images")
            ftp.storbinary("STOR picture.gif", io.BytesIO(b"GIF89a"))
            self.assertEqual(self.store.read_file("alice", "images/picture.gif"), b"GIF89a")
            self.assertEqual(ftp.nlst(), ["picture.gif"])
            listing = []
            ftp.retrlines("LIST", listing.append)
            self.assertTrue(any("picture.gif" in line for line in listing))
            ftp.rename("picture.gif", "renamed.gif")
            self.assertEqual(self.read(ftp, "renamed.gif"), b"GIF89a")
            ftp.delete("renamed.gif")
            ftp.cwd("..")
            ftp.rmd("images")
            self.assertEqual(ftp.nlst(), ["web.html"])
            ftp.storlines("STOR ascii.txt", io.BytesIO(b"one\r\ntwo\r\n"))
            self.assertEqual(self.store.read_file("alice", "ascii.txt"), b"one\ntwo\n")

    def test_auth_active_mode_and_unsupported_writes_are_rejected(self):
        for user, password in (("alice", "wrong"), ("anonymous", "guest@example.org"),
                               ("../alice", "password-alice")):
            with self.subTest(user=user), self.assertRaises(ftplib.error_perm):
                with self.client(user, password):
                    pass
        with self.client() as ftp:
            for command in ("PORT 127,0,0,1,200,0", "EPRT |1|127.0.0.1|50000|",
                            "REST 1", "STOU new.txt", "APPE new.txt",
                            "SITE CHMOD 777 file.txt", "SITE EXEC id"):
                with self.subTest(command=command), self.assertRaises(ftplib.error_perm):
                    ftp.sendcmd(command)
            with self.assertRaises(ftplib.error_perm):
                ftp.storbinary("STOR .secret", io.BytesIO(b"hidden"))

    def test_user_isolation_hidden_files_and_symlinks(self):
        home = self.store.user_home("alice")
        self.store.write_file("bobby", "private.txt", b"bob secret")
        (home / ".secret").write_text("secret")
        (home / "link").symlink_to(self.store.user_home("bobby"), target_is_directory=True)
        (home / "filelink.txt").symlink_to(self.store.user_home("bobby") / "private.txt")
        with self.client() as ftp:
            self.assertEqual(ftp.nlst(), [])
            for path in (".secret", "link/private.txt", "filelink.txt", "../bobby/private.txt",
                         "/bobby/private.txt", "../../accounts.sqlite"):
                with self.subTest(path=path), self.assertRaises(ftplib.error_perm):
                    self.read(ftp, path)
            for path in ("link", ".hidden"):
                with self.subTest(path=path), self.assertRaises(ftplib.error_perm):
                    ftp.cwd(path)
            with self.assertRaises(ftplib.error_perm):
                ftp.storbinary("STOR link/new.txt", io.BytesIO(b"bad"))
            self.assertFalse((self.store.user_home("bobby") / "new.txt").exists())

    def test_file_limit_aborts_before_eof_and_keeps_previous_file(self):
        self.store.write_file("alice", "old.txt", b"old published file")
        with self.client() as ftp:
            ftp.voidcmd("TYPE I")
            data = ftp.transfercmd("STOR old.txt")
            self.addCleanup(data.close)
            data.sendall(b"x" * MAX_FILE_BYTES)
            data.sendall(b"oversized")
            # The data socket remains open here: failure must occur during STOR,
            # before EOF, rather than accepting unbounded temporary data.
            with self.assertRaises(ftplib.error_temp):
                ftp.voidresp()
            self.assertEqual((self.store.user_home("alice") / "old.txt").read_bytes(),
                             b"old published file")
            self.assertEqual(list(self.store.user_home("alice").glob(".upload-*")), [])

    def test_account_quota_and_replacement_credit(self):
        home = self.store.user_home("alice")
        for number in range(MAX_ACCOUNT_BYTES // MAX_FILE_BYTES):
            self.store.write_file("alice", f"{number}.bin", b"x" * MAX_FILE_BYTES)
        with self.client() as ftp:
            with self.assertRaises(ftplib.error_temp):
                ftp.storbinary("STOR extra.bin", io.BytesIO(b"x"))
            self.assertFalse((home / "extra.bin").exists())
            ftp.storbinary("STOR 0.bin", io.BytesIO(b"replacement"))
            self.assertEqual(self.read(ftp, "0.bin"), b"replacement")
            ftp.storbinary("STOR extra.bin", io.BytesIO(b"now there is space"))

    def test_file_count_quota(self):
        home = self.store.user_home("alice")
        for number in range(MAX_FILES):
            (home / f"{number}.txt").write_bytes(b"")
        with self.client() as ftp:
            with self.assertRaises(ftplib.error_perm):
                ftp.storbinary("STOR one-too-many.txt", io.BytesIO(b""))
            ftp.storbinary("STOR 0.txt", io.BytesIO(b"replacement"))
            self.assertEqual(self.read(ftp, "0.txt"), b"replacement")

    def test_same_user_sessions_serialize_and_disconnect_discards_partial_upload(self):
        home = self.store.user_home("alice")
        self.store.write_file("alice", "old.txt", b"previous")
        with self.client() as first, self.client() as second, self.client("bobby") as other:
            first.voidcmd("TYPE I")
            data = first.transfercmd("STOR old.txt")
            self.addCleanup(data.close)
            data.sendall(b"partial")
            self.wait_for(lambda: bool(list(home.glob(".upload-*"))))
            self.assertEqual(self.read(second, "old.txt"), b"previous")
            for operation in (lambda: second.storbinary("STOR second.txt", io.BytesIO(b"x")),
                              lambda: second.delete("old.txt"), lambda: second.mkd("newdir")):
                with self.assertRaises(ftplib.error_perm):
                    operation()
            other.storbinary("STOR independent.txt", io.BytesIO(b"works"))
            first.close()
            self.wait_for(lambda: not list(home.glob(".upload-*")))
            self.assertEqual((home / "old.txt").read_bytes(), b"previous")
            second.storbinary("STOR after.txt", io.BytesIO(b"released"))

    def test_web_process_lock_rejects_ftp_mutation_without_blocking(self):
        with self.client() as ftp:
            with self.store.user_lock("alice"):
                start = time.monotonic()
                with self.assertRaises(ftplib.error_perm):
                    ftp.storbinary("STOR locked.txt", io.BytesIO(b"x"))
                self.assertLess(time.monotonic() - start, 2)
            ftp.storbinary("STOR unlocked.txt", io.BytesIO(b"ok"))
            self.assertEqual(self.read(ftp, "unlocked.txt"), b"ok")

    def test_directory_limit_cannot_be_bypassed_with_mkd(self):
        home = self.store.user_home("alice")
        for number in range(MAX_DIRECTORIES):
            (home / f"folder-{number}").mkdir()
        with self.client() as ftp:
            with self.assertRaises(ftplib.error_perm):
                ftp.mkd("too-many")
            self.assertFalse((home / "too-many").exists())
            ftp.rmd("folder-0")
            ftp.mkd("allowed-again")

    def test_passive_range_exhaustion_does_not_use_an_unfiltered_port(self):
        reservations = []
        try:
            for port in self.passive_ports:
                reservation = socket.socket()
                reservations.append(reservation)
                reservation.bind(("127.0.0.1", port))
                reservation.listen()
            with self.client() as ftp:
                with self.assertRaises(ftplib.error_temp):
                    ftp.makepasv()
                for reservation in reservations:
                    reservation.close()
                self.assertEqual(ftp.nlst(), [])
        finally:
            for reservation in reservations:
                reservation.close()

    def test_pending_upload_commands_and_abort_release_the_account_lock(self):
        home = self.store.user_home("alice")
        with self.client() as ftp:
            ftp.makepasv()  # Reserve the data listener, but do not connect to it.
            self.assertTrue(ftp.sendcmd("STOR pending.txt").startswith("150"))
            for command in ("USER bobby", "REIN", "RETR other.txt", "LIST", "STOR second.txt"):
                with self.subTest(command=command), self.assertRaises(ftplib.error_temp):
                    ftp.sendcmd(command)
            self.assertTrue(ftp.sendcmd("ABOR").startswith("225"))
            self.assertEqual(list(home.glob(".upload-*")), [])
            self.assertFalse((home / "pending.txt").exists())
            ftp.storbinary("STOR after.txt", io.BytesIO(b"lock released"))
            self.assertEqual(self.read(ftp, "after.txt"), b"lock released")


if __name__ == "__main__":
    unittest.main()
