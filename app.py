#!/usr/bin/env python3
"""Small single-user file storage server.

Standard library only (no third-party packages), so the Docker image stays small.

Configuration is read from environment variables:
  APP_PASSWORD      (required) password for the web UI, at least 8 characters
  SESSION_SECRET    optional key for signing login cookies; random per start if unset
  MAX_UPLOAD_MB     maximum size of one uploaded file in MB (default 5120)
  COOKIE_SECURE     set to 1 when served over HTTPS (default 0)
  SESSION_TTL_HOURS how long a login lasts (default 12)
  DATA_DIR          where files and the index are stored (default ./data)
  PORT              port to listen on inside the container (default 8080)
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
FILES_DIR = os.path.join(DATA_DIR, "files")
INDEX_PATH = os.path.join(DATA_DIR, "index.json")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "5120")) * 1024 * 1024
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
SESSION_TTL = int(os.environ.get("SESSION_TTL_HOURS", "12")) * 3600
PORT = int(os.environ.get("PORT", "8080"))

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 15 * 60
CHUNK = 1024 * 1024
ID_RE = re.compile(r"^[0-9a-f]{32}$")
STATIC_FILES = {
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}

_index_lock = threading.Lock()
_login_lock = threading.Lock()
_failures = {}  # client ip -> [failure_count, window_start]


# ---------- file index ----------

def load_index():
    if not os.path.exists(INDEX_PATH):
        return {}
    with open(INDEX_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_index(index):
    tmp = INDEX_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2)
    os.replace(tmp, INDEX_PATH)


# ---------- auth ----------

def _sign(payload):
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_token():
    expires = str(int(time.time()) + SESSION_TTL)
    return f"{expires}.{_sign(expires)}"


def valid_session_token(token):
    if not token or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


def password_ok(candidate):
    return hmac.compare_digest(candidate.encode("utf-8"), APP_PASSWORD.encode("utf-8"))


def login_blocked(ip):
    with _login_lock:
        entry = _failures.get(ip)
        if not entry:
            return False
        count, start = entry
        if time.time() - start > LOGIN_WINDOW:
            del _failures[ip]
            return False
        return count >= LOGIN_MAX_FAILURES


def record_login_failure(ip):
    with _login_lock:
        now = time.time()
        entry = _failures.get(ip)
        if not entry or now - entry[1] > LOGIN_WINDOW:
            _failures[ip] = [1, now]
        else:
            entry[0] += 1


def clear_login_failures(ip):
    with _login_lock:
        _failures.pop(ip, None)


# ---------- helpers ----------

def clean_filename(raw):
    name = os.path.basename(raw.replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    return name[:200] or "file"


def content_disposition(name):
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name).replace('"', "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


class Handler(BaseHTTPRequestHandler):
    server_version = "FileStore"
    sys_version = ""
    timeout = 300  # drop stalled connections after 5 minutes of silence

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- response helpers --

    def _base_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
        )

    def _json(self, status, obj, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._base_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away; nothing to do

    def _error(self, status, message):
        # The request body may be unread, so don't reuse this connection.
        self.close_connection = True
        self._json(status, {"error": message})

    def _send_file(self, path, content_type, download_name=None):
        size = os.path.getsize(path)
        self.send_response(200)
        self._base_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        if download_name:
            self.send_header("Content-Disposition", content_disposition(download_name))
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile, CHUNK)

    # -- request checks --

    def _is_authed(self):
        raw = self.headers.get("Cookie", "")
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return False
        morsel = jar.get("session")
        return bool(morsel and valid_session_token(morsel.value))

    def _same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlsplit(origin).netloc == self.headers.get("Host", "")

    def _require_auth(self):
        if not self._is_authed():
            self._error(401, "Not logged in")
            return False
        if not self._same_origin():
            self._error(403, "Cross-site request blocked")
            return False
        return True

    def _file_id_from_path(self, path):
        if not path.startswith("/api/files/"):
            return None
        file_id = path[len("/api/files/"):]
        return file_id if ID_RE.match(file_id) else None

    # -- routes --

    def do_GET(self):
        path = urlsplit(self.path).path

        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            return self._static(name, ctype)
        if path == "/healthz":
            return self._json(200, {"ok": True})
        if path == "/api/session":
            return self._json(200, {"authenticated": self._is_authed()})

        if path == "/api/files":
            if not self._require_auth():
                return
            with _index_lock:
                items = sorted(load_index().values(), key=lambda m: m["uploaded_at"], reverse=True)
            return self._json(200, {"files": items})

        file_id = self._file_id_from_path(path)
        if file_id:
            if not self._require_auth():
                return
            with _index_lock:
                meta = load_index().get(file_id)
            disk_path = os.path.join(FILES_DIR, file_id)
            if not meta or not os.path.isfile(disk_path):
                return self._error(404, "File not found")
            return self._send_file(disk_path, "application/octet-stream", meta["name"])

        self._error(404, "Not found")

    def _static(self, filename, content_type):
        full = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(full):
            return self._error(404, "Not found")
        self._send_file(full, content_type)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/login":
            return self._login()
        if path == "/api/logout":
            if not self._same_origin():
                return self._error(403, "Cross-site request blocked")
            return self._json(
                200, {"ok": True},
                [("Set-Cookie", "session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict")],
            )
        self._error(404, "Not found")

    def _login(self):
        ip = self.client_address[0]
        if not self._same_origin():
            return self._error(403, "Cross-site request blocked")
        if login_blocked(ip):
            return self._error(429, "Too many failed attempts. Try again in 15 minutes.")

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._error(400, "Bad request")
        if length <= 0 or length > 4096:
            return self._error(400, "Bad request")
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            candidate = str(data.get("password", ""))
        except (ValueError, AttributeError):
            return self._error(400, "Bad request")

        if not password_ok(candidate):
            record_login_failure(ip)
            time.sleep(1)  # slow down password guessing
            return self._error(401, "Wrong password")

        clear_login_failures(ip)
        cookie = (
            f"session={make_session_token()}; Path=/; Max-Age={SESSION_TTL}; "
            f"HttpOnly; SameSite=Strict" + ("; Secure" if COOKIE_SECURE else "")
        )
        self._json(200, {"ok": True}, [("Set-Cookie", cookie)])

    def do_PUT(self):
        path = urlsplit(self.path).path
        if path != "/api/files":
            return self._error(404, "Not found")
        if not self._require_auth():
            return

        if "Transfer-Encoding" in self.headers:
            return self._error(411, "Content-Length is required")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error(411, "Content-Length is required")
        if length < 0:
            return self._error(400, "Bad request")
        if length > MAX_UPLOAD_BYTES:
            return self._error(413, "File is larger than the upload limit")

        name = clean_filename(unquote(self.headers.get("X-Filename", "")))

        file_id = uuid.uuid4().hex
        part_path = os.path.join(FILES_DIR, file_id + ".part")
        final_path = os.path.join(FILES_DIR, file_id)
        digest = hashlib.sha256()
        remaining = length
        try:
            with open(part_path, "wb") as out:
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
        except OSError:
            remaining = -1

        if remaining != 0:
            if os.path.exists(part_path):
                os.remove(part_path)
            return self._error(400, "Upload was incomplete")

        os.replace(part_path, final_path)
        meta = {
            "id": file_id,
            "name": name,
            "size": length,
            "sha256": digest.hexdigest(),
            "uploaded_at": int(time.time()),
        }
        with _index_lock:
            index = load_index()
            index[file_id] = meta
            save_index(index)
        self._json(201, meta)

    def do_DELETE(self):
        if not self._require_auth():
            return
        file_id = self._file_id_from_path(urlsplit(self.path).path)
        if not file_id:
            return self._error(404, "Not found")
        with _index_lock:
            index = load_index()
            if file_id not in index:
                return self._error(404, "File not found")
            del index[file_id]
            save_index(index)
        disk_path = os.path.join(FILES_DIR, file_id)
        if os.path.exists(disk_path):
            os.remove(disk_path)
        self._json(200, {"ok": True})


def main():
    if not APP_PASSWORD:
        sys.exit("ERROR: APP_PASSWORD is not set. Set it in the environment or .env file.")
    if len(APP_PASSWORD) < 8:
        sys.exit("ERROR: APP_PASSWORD must be at least 8 characters.")

    os.makedirs(FILES_DIR, exist_ok=True)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"File store listening on port {PORT}, data in {DATA_DIR}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
