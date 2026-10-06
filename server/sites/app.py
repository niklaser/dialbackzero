"""Portal and internal site-file reader."""

from __future__ import annotations

import contextlib
import fcntl
import hmac
import ntpath
import os
from pathlib import Path
import re
import secrets
import tempfile
from urllib.parse import quote

from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

try:
    from .store import MAX_FILE_BYTES, Store, StoreError, UnsafePath, UserBusy
except ImportError:
    from store import MAX_FILE_BYTES, Store, StoreError, UnsafePath, UserBusy


def _persistent_secret(data_dir: Path) -> bytes:
    data_dir.mkdir(parents=True, exist_ok=True)
    secret_path = data_dir / "session.key"
    lock_path = data_dir / ".session-key.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if not secret_path.exists():
            fd, temporary_name = tempfile.mkstemp(prefix=".session-key-", dir=data_dir)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(secrets.token_bytes(32))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary_name, secret_path)
            except Exception:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary_name)
                raise
        if secret_path.is_symlink():
            raise RuntimeError("session.key must not be a symlink")
        os.chmod(secret_path, 0o600)
        secret = secret_path.read_bytes()
        if len(secret) < 32:
            raise RuntimeError("session.key is invalid")
        return secret
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def create_app(config: dict | None = None) -> Flask:
    data_dir = Path(os.environ.get("DBZ_DATA_DIR", "/data"))
    dns_dir = Path(os.environ.get("DBZ_DNS_DIR", "/dns"))
    members_host = os.environ.get("DBZ_MEMBERS_HOST", "members.retro.net").lower()

    app = Flask(__name__, template_folder="templates")
    app.config.update(
        DBZ_DATA_DIR=str(data_dir),
        DBZ_DNS_DIR=str(dns_dir),
        DBZ_MEMBERS_HOST=members_host,
        MAX_CONTENT_LENGTH=MAX_FILE_BYTES + 128 * 1024,
        SESSION_COOKIE_NAME="dbz_members",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=False,
    )
    if config:
        app.config.update(config)
    if not app.config.get("SECRET_KEY"):
        app.config["SECRET_KEY"] = _persistent_secret(Path(app.config["DBZ_DATA_DIR"]))
    app.extensions["dbz_store"] = Store(app.config["DBZ_DATA_DIR"], app.config["DBZ_DNS_DIR"])
    app.extensions["dbz_store"].reconcile_dns()

    def store() -> Store:
        return app.extensions["dbz_store"]

    def loopback_request() -> bool:
        return request.remote_addr in {"127.0.0.1", "::1"}

    def csrf_token() -> str:
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["public_path"] = lambda path: quote(path, safe="/")

    internal_endpoints = {"healthz", "public_file", "legacy_file"}
    site_mutation_endpoints = {"dashboard", "claim", "edit", "upload", "delete"}

    @app.before_request
    def enforce_request_boundary():
        if request.endpoint in internal_endpoints:
            if not loopback_request():
                abort(404)
            return None
        if request.host.lower() != app.config["DBZ_MEMBERS_HOST"].lower():
            return "Bad Host\n", 400, {"Content-Type": "text/plain; charset=us-ascii"}
        if request.method == "POST":
            expected = session.get("csrf_token", "")
            supplied = request.form.get("csrf_token", "")
            try:
                token_valid = bool(expected) and hmac.compare_digest(
                    expected.encode("ascii"), supplied.encode("ascii")
                )
            except (AttributeError, UnicodeEncodeError):
                token_valid = False
            if not token_valid:
                return "Invalid form token. Go back and try again.\n", 400, {
                    "Content-Type": "text/plain; charset=us-ascii"
                }
        if request.endpoint in site_mutation_endpoints and isinstance(session.get("username"), str):
            lock = store().user_lock(session["username"], blocking=False)
            try:
                lock.__enter__()
            except UserBusy:
                return "Site files are busy with an FTP transfer. Try again shortly.\n", 503, {
                    "Content-Type": "text/plain; charset=us-ascii",
                    "Retry-After": "3",
                }
            g.dbz_site_lock = lock
        return None

    @app.teardown_request
    def release_site_lock(_error):
        lock = g.pop("dbz_site_lock", None)
        if lock is not None:
            lock.__exit__(None, None, None)

    def current_user() -> str | None:
        value = session.get("username")
        return value if isinstance(value, str) else None

    def require_user() -> str:
        username = current_user()
        if not username:
            abort(401)
        return username

    @app.get("/healthz")
    def healthz():
        return "ok\n", 200, {"Content-Type": "text/plain; charset=us-ascii"}

    @app.get("/")
    def home():
        return redirect(url_for("dashboard" if current_user() else "login"))

    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            try:
                canonical = store().register(username, password)
            except StoreError as error:
                flash(str(error))
            else:
                session.clear()
                session["username"] = canonical
                csrf_token()
                return redirect(url_for("dashboard"))
        return render_template("signup.html", title="Create account")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if store().authenticate(username, password):
                session.clear()
                session["username"] = username.lower()
                csrf_token()
                return redirect(url_for("dashboard"))
            flash("Username or password was not accepted.")
        return render_template("login.html", title="Sign in")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/dashboard")
    def dashboard():
        username = require_user()
        domain = store().user_domain(username)
        files = store().list_files(username) if domain else []
        file_count, used = store().usage(username)
        return render_template(
            "dashboard.html",
            title="Your site",
            username=username,
            domain=domain,
            files=files,
            file_count=file_count,
            used=used,
        )

    @app.post("/claim")
    def claim():
        username = require_user()
        try:
            domain = store().claim_domain(username, request.form.get("domain", ""))
        except StoreError as error:
            flash(str(error))
        else:
            flash(f"Your site is ready at http://{domain}/")
        return redirect(url_for("dashboard"))

    @app.route("/edit", methods=["GET", "POST"])
    def edit():
        username = require_user()
        if not store().user_domain(username):
            flash("Claim a domain before adding files.")
            return redirect(url_for("dashboard"))
        relative = (
            request.form.get("path", "")
            if request.method == "POST"
            else request.args.get("path", "")
        )
        content = ""
        encoding = "utf-8"
        if request.method == "POST":
            content = request.form.get("content", "")
            encoding = request.form.get("encoding", "utf-8")
            if encoding not in {"utf-8", "windows-1252"}:
                return "Invalid text encoding.\n", 400, {
                    "Content-Type": "text/plain; charset=us-ascii"
                }
            try:
                encoded = content.encode(encoding)
            except UnicodeEncodeError:
                flash("This text contains a character unavailable in the selected encoding.")
            else:
                try:
                    store().write_file(username, relative, encoded)
                except StoreError as error:
                    flash(str(error))
                else:
                    flash(f"Saved {relative}.")
                    return redirect(url_for("dashboard"))
        elif relative:
            try:
                raw = store().read_file(username, relative)
                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        content = raw.decode("windows-1252")
                    except UnicodeDecodeError:
                        flash("This file is binary and cannot be opened in the text editor.")
                        return redirect(url_for("dashboard"))
                    else:
                        encoding = "windows-1252"
            except FileNotFoundError:
                content = ""
            except StoreError as error:
                flash(str(error))
                return redirect(url_for("dashboard"))
        return render_template(
            "edit.html", title="Edit file", path=relative, content=content, encoding=encoding
        )

    @app.post("/upload")
    def upload():
        username = require_user()
        if not store().user_domain(username):
            flash("Claim a domain before adding files.")
            return redirect(url_for("dashboard"))
        uploaded = request.files.get("file")
        relative = request.form.get("path", "").strip()
        if uploaded is None or not uploaded.filename:
            flash("Choose a file to upload.")
            return redirect(url_for("dashboard"))
        if not relative:
            # Werkzeug decodes quoted header backslashes, so its filename can
            # lose the separators sent by Windows 9x browsers. Recover the
            # basename from the retained raw header when it contains a path.
            disposition = uploaded.headers.get("Content-Disposition", "")
            raw_name = re.search(r'filename="([^"]*)"', disposition)
            relative = ntpath.basename(raw_name.group(1) if raw_name else uploaded.filename)
        try:
            store().write_stream(username, relative, uploaded.stream)
        except StoreError as error:
            flash(str(error))
        else:
            flash(f"Uploaded {relative}.")
        return redirect(url_for("dashboard"))

    @app.post("/delete")
    def delete():
        username = require_user()
        relative = request.form.get("path", "")
        try:
            store().delete_file(username, relative)
        except (StoreError, FileNotFoundError) as error:
            flash(str(error) or "File not found.")
        else:
            flash(f"Deleted {relative}.")
        return redirect(url_for("dashboard"))

    @app.get("/directory")
    def directory():
        return render_template("directory.html", title="Site directory", sites=store().list_sites())

    @app.get("/public/<domain>/", defaults={"relative": ""})
    @app.get("/public/<domain>/<path:relative>")
    def public_file(domain: str, relative: str):
        try:
            path, mime, _size = store().open_public(domain, relative)
        except (FileNotFoundError, StoreError, UnsafePath):
            abort(404)
        return send_file(path, mimetype=mime, conditional=True, max_age=0)

    @app.get("/legacy/<username>/", defaults={"relative": ""})
    @app.get("/legacy/<username>/<path:relative>")
    def legacy_file(username: str, relative: str):
        try:
            domain = store().user_domain(username)
            if not domain:
                abort(404)
            path, mime, _size = store().open_public(domain, relative)
        except (FileNotFoundError, StoreError, UnsafePath):
            abort(404)
        return send_file(path, mimetype=mime, conditional=True, max_age=0)

    @app.errorhandler(401)
    def unauthorized(_error):
        return redirect(url_for("login"))

    @app.errorhandler(413)
    def request_too_large(_error):
        return "Upload is too large. The file limit is 2 MiB.\n", 413, {
            "Content-Type": "text/plain; charset=us-ascii"
        }

    return app


app = create_app()
