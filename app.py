#!/usr/bin/env python3
"""Single-user file storage server. Standard library only.

Large files are uploaded in chunks. Each chunk is its own request, so a dropped
connection only costs one chunk, and the upload resumes where it stopped.
Downloads support HTTP Range requests, so interrupted downloads can resume too.

Environment variables:
  APP_PASSWORD       (required) password for the web UI, at least 8 characters
  SESSION_SECRET     optional fixed key for login cookies; random per start if unset
  COOKIE_SECURE      1 when served over HTTPS (default 0)
  SESSION_TTL_HOURS  logout after this many idle hours; resets on each request (default 12)
  MAX_FILE_GB        largest allowed single file in GB, 0 = no limit (default 0)
  CHUNK_MB           upload part size in MB (default 8; keep small so each request
                     finishes before the proxy's ~60 s timeout)
  DISK_RESERVE_GB    if above 0, refuse uploads that would leave less than this free (default 0)
  DATA_DIR           where files and state are stored (default ./data)
  PORT               port to listen on inside the container (default 8080)
"""

import errno
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
from urllib.parse import quote, urlsplit

GB = 1024 ** 3
MB = 1024 ** 2
IO_BLOCK = 1024 * 1024

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.environ.get("DATA_DIR", os.path.join(BASE_DIR, "data"))
FILES_DIR = os.path.join(DATA_DIR, "files")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
INDEX_PATH = os.path.join(DATA_DIR, "index.json")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
SECRET_KEY = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
SESSION_TTL = int(os.environ.get("SESSION_TTL_HOURS", "12")) * 3600
MAX_FILE_BYTES = int(os.environ.get("MAX_FILE_GB", "0")) * GB
CHUNK_BYTES = int(os.environ.get("CHUNK_MB", "8")) * MB
DISK_RESERVE_BYTES = int(os.environ.get("DISK_RESERVE_GB", "0")) * GB  # 0 = don't check space
STALE_UPLOAD_SECONDS = 72 * 3600
PORT = int(os.environ.get("PORT", "8080"))

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW = 15 * 60

FILE_ROUTE = re.compile(r"^/api/files/([0-9a-f]{32})$")
UPLOAD_ROUTE = re.compile(r"^/api/uploads/([0-9a-f]{32})$")
CHUNK_ROUTE = re.compile(r"^/api/uploads/([0-9a-f]{32})/chunks/(\d+)$")
COMPLETE_ROUTE = re.compile(r"^/api/uploads/([0-9a-f]{32})/complete$")
ABORT_ROUTE = re.compile(r"^/api/uploads/([0-9a-f]{32})/abort$")
FILE_DELETE_ROUTE = re.compile(r"^/api/files/([0-9a-f]{32})/delete$")

STATIC_FILES = {
    "/static/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}

_index_lock = threading.Lock()
_upload_lock = threading.Lock()
_login_lock = threading.Lock()
_failures = {}  # client ip -> [failure_count, window_start]


# ---------- storage helpers ----------

def _read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def load_index():
    if not os.path.exists(INDEX_PATH):
        return {}
    return _read_json(INDEX_PATH)


def save_index(index):
    _write_json_atomic(INDEX_PATH, index)


def upload_state_path(uid):
    return os.path.join(UPLOADS_DIR, uid + ".json")


def part_path(uid):
    return os.path.join(FILES_DIR, uid + ".part")


def load_upload(uid):
    try:
        return _read_json(upload_state_path(uid))
    except FileNotFoundError:
        return None


def save_upload(state):
    _write_json_atomic(upload_state_path(state["upload_id"]), state)


def remove_upload(uid):
    for path in (part_path(uid), upload_state_path(uid)):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def sha256_of(path):
    """Read a file back and return its SHA-256 hex digest (flushed to disk first)."""
    digest = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
        with os.fdopen(os.dup(fd), "rb") as f:
            for block in iter(lambda: f.read(IO_BLOCK * 4), b""):
                digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _hash_stored_file(file_id):
    """Compute the SHA-256 of a stored file and record it in the index."""
    try:
        digest = sha256_of(os.path.join(FILES_DIR, file_id))
    except OSError:
        return  # file was deleted in the meantime
    with _index_lock:
        index = load_index()
        if file_id in index:
            index[file_id]["sha256"] = digest
            save_index(index)


def start_hashing(file_id):
    threading.Thread(target=_hash_stored_file, args=(file_id,), daemon=True).start()


def total_chunks(size, chunk_size):
    return -(-size // chunk_size) if size > 0 else 0


def expected_chunk_len(state, index):
    start = index * state["chunk_size"]
    return min(state["chunk_size"], state["size"] - start)


def upload_view(state):
    return {
        "upload_id": state["upload_id"],
        "path": state["path"],
        "size": state["size"],
        "chunk_size": state["chunk_size"],
        "total_chunks": total_chunks(state["size"], state["chunk_size"]),
        "received": sorted(state["received"]),
    }


def cleanup_stale_uploads():
    """Delete unfinished uploads that have been idle for 72 hours."""
    cutoff = time.time() - STALE_UPLOAD_SECONDS
    with _upload_lock:
        for name in os.listdir(UPLOADS_DIR):
            if not name.endswith(".json"):
                continue
            uid = name[:-5]
            try:
                state = _read_json(os.path.join(UPLOADS_DIR, name))
            except (OSError, ValueError):
                continue
            if state.get("updated", 0) < cutoff:
                remove_upload(uid)


# ---------- auth ----------

def _sign(payload):
    return hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_token():
    expires = str(int(time.time()) + SESSION_TTL)
    return f"{expires}.{_sign(expires)}"


def session_cookie(token):
    value = (
        f"session={token}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Strict"
    )
    return value + ("; Secure" if COOKIE_SECURE else "")


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


# ---------- names ----------

def validate_path(raw):
    """Return the name/relative path exactly as given, or None if it is unsafe.

    Names are never changed. Paths that could be misread (empty parts, "..",
    absolute paths, control characters, backslashes) are rejected instead.
    """
    if not isinstance(raw, str) or not raw or len(raw) > 1024:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw) or "\\" in raw:
        return None
    if raw.startswith("/"):
        return None
    if any(part in ("", ".", "..") for part in raw.split("/")):
        return None
    return raw


def content_disposition(name):
    ascii_name = re.sub(r"[^\x20-\x7e]", "_", name).replace('"', "_")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name)}"


class Handler(BaseHTTPRequestHandler):
    server_version = "FileStore"
    sys_version = ""
    timeout = 300  # drop connections that stay silent for 5 minutes
    _renewed_token = None

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # ----- responses -----

    def _base_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
        )
        if self._renewed_token:
            self.send_header("Set-Cookie", session_cookie(self._renewed_token))

    def _json(self, status, obj, extra_headers=None):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self._base_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client went away

    def _error(self, status, message):
        # The request body may be unread, so the connection can't be reused.
        self.close_connection = True
        self._json(status, {"error": message})

    def _send_stored_file(self, path, download_name):
        """Send a file, honouring a single HTTP Range header for resumable downloads."""
        size = os.path.getsize(path)
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header and size > 0:
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not m or (not m.group(1) and not m.group(2)):
                return self._range_not_satisfiable(size)
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start >= size or start > end:
                return self._range_not_satisfiable(size)
            status = 206

        length = end - start + 1 if size > 0 else 0
        try:
            self.send_response(status)
            self._base_headers()
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if download_name:
                self.send_header("Content-Disposition", content_disposition(download_name))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    buf = f.read(min(IO_BLOCK, remaining))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    remaining -= len(buf)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _range_not_satisfiable(self, size):
        self.close_connection = True
        self._json(416, {"error": "Invalid range"}, [("Content-Range", f"bytes */{size}")])

    def _read_body_json(self, limit=4096):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > limit:
            return None
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    # ----- auth checks -----

    def _is_authed(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
        except Exception:
            return False
        morsel = jar.get("session")
        if not morsel or not valid_session_token(morsel.value):
            return False
        # Sliding expiry: every request renews the session, so a long upload
        # doesn't log you out halfway through.
        self._renewed_token = make_session_token()
        return True

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

    # ----- routes -----

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
                items = list(load_index().values())
            items.sort(key=lambda m: m.get("path", m["name"]).lower())
            return self._json(200, {"files": items})

        if path == "/api/uploads":
            if not self._require_auth():
                return
            uploads = []
            for name in os.listdir(UPLOADS_DIR):
                if not name.endswith(".json"):
                    continue
                try:
                    state = _read_json(os.path.join(UPLOADS_DIR, name))
                except (OSError, ValueError):
                    continue
                if not state.get("completed"):
                    uploads.append(upload_view(state))
            return self._json(200, {"uploads": uploads})

        m = UPLOAD_ROUTE.match(path)
        if m:
            if not self._require_auth():
                return
            state = load_upload(m.group(1))
            if state is None or state.get("completed"):
                return self._error(404, "Upload not found")
            return self._json(200, upload_view(state))

        m = FILE_ROUTE.match(path)
        if m:
            if not self._require_auth():
                return
            with _index_lock:
                meta = load_index().get(m.group(1))
            disk_path = os.path.join(FILES_DIR, m.group(1))
            if not meta or not os.path.isfile(disk_path):
                return self._error(404, "File not found")
            return self._send_stored_file(disk_path, meta["name"])

        self._error(404, "Not found")

    def _static(self, filename, content_type):
        full = os.path.join(STATIC_DIR, filename)
        if not os.path.isfile(full):
            return self._error(404, "Not found")
        with open(full, "rb") as f:
            body = f.read()
        try:
            self.send_response(200)
            self._base_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
        if not self._require_auth():
            return
        if path == "/api/uploads":
            return self._create_upload()
        m = COMPLETE_ROUTE.match(path)
        if m:
            return self._complete_upload(m.group(1))
        # POST versions of the part/abort/delete calls. Some proxies don't pass
        # PUT and DELETE through, so the browser uses these instead.
        m = CHUNK_ROUTE.match(path)
        if m:
            return self._put_chunk(m.group(1), int(m.group(2)))
        m = ABORT_ROUTE.match(path)
        if m:
            return self._abort_upload(m.group(1))
        m = FILE_DELETE_ROUTE.match(path)
        if m:
            return self._delete_file(m.group(1))
        self._error(404, "Not found")

    def do_PUT(self):
        path = urlsplit(self.path).path
        m = CHUNK_ROUTE.match(path)
        if not m:
            return self._error(404, "Not found")
        if not self._require_auth():
            return
        self._put_chunk(m.group(1), int(m.group(2)))

    def do_DELETE(self):
        path = urlsplit(self.path).path
        if not self._require_auth():
            return

        m = UPLOAD_ROUTE.match(path)
        if m:
            return self._abort_upload(m.group(1))

        m = FILE_ROUTE.match(path)
        if m:
            return self._delete_file(m.group(1))

        self._error(404, "Not found")

    def _abort_upload(self, uid):
        with _upload_lock:
            state = load_upload(uid)
            if state is None or state.get("completed"):
                return self._error(404, "Upload not found")
            remove_upload(uid)
        self._json(200, {"ok": True})

    def _delete_file(self, file_id):
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

    # ----- login -----

    def _login(self):
        ip = self.client_address[0]
        if not self._same_origin():
            return self._error(403, "Cross-site request blocked")
        if login_blocked(ip):
            return self._error(429, "Too many failed attempts. Try again in 15 minutes.")

        data = self._read_body_json()
        if data is None:
            return self._error(400, "Bad request")
        candidate = str(data.get("password", ""))

        if not password_ok(candidate):
            record_login_failure(ip)
            time.sleep(1)  # slow down password guessing
            return self._error(401, "Wrong password")

        clear_login_failures(ip)
        self._json(200, {"ok": True}, [("Set-Cookie", session_cookie(make_session_token()))])

    # ----- chunked uploads -----

    def _create_upload(self):
        body = self._read_body_json()
        if body is None:
            return self._error(400, "Bad request")
        try:
            size = int(body.get("size"))
        except (TypeError, ValueError):
            return self._error(400, "Size is required")
        if size < 0:
            return self._error(400, "Bad size")
        if MAX_FILE_BYTES and size > MAX_FILE_BYTES:
            return self._error(413, "File is larger than the allowed limit")

        path = validate_path(body.get("path") or body.get("name"))
        if path is None:
            return self._error(400, "Invalid file name or path")

        if DISK_RESERVE_BYTES and size + DISK_RESERVE_BYTES > shutil.disk_usage(DATA_DIR).free:
            return self._error(507, "Not enough free disk space on the server for this file")

        cleanup_stale_uploads()
        uid = uuid.uuid4().hex
        now = int(time.time())
        state = {
            "upload_id": uid,
            "path": path,
            "name": path.split("/")[-1],
            "size": size,
            "chunk_size": CHUNK_BYTES,
            "received": [],
            "created": now,
            "updated": now,
        }
        try:
            with _upload_lock:
                with open(part_path(uid), "wb") as f:
                    f.truncate(size)  # reserve the full size up front
                save_upload(state)
        except OSError as e:
            remove_upload(uid)
            status = 507 if e.errno == errno.ENOSPC else 500
            return self._error(status, "Could not start the upload")
        self._json(201, upload_view(state))

    def _put_chunk(self, uid, index):
        state = load_upload(uid)
        if state is None or state.get("completed"):
            return self._error(404, "Upload not found")

        if not (0 <= index < total_chunks(state["size"], state["chunk_size"])):
            return self._error(400, "Chunk number out of range")
        expected = expected_chunk_len(state, index)

        if "Transfer-Encoding" in self.headers:
            return self._error(411, "Content-Length is required")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self._error(411, "Content-Length is required")
        if length != expected:
            return self._error(400, f"Chunk must be exactly {expected} bytes")

        try:
            fd = os.open(part_path(uid), os.O_WRONLY)
        except FileNotFoundError:
            return self._error(404, "Upload not found")

        claimed = self.headers.get("X-Chunk-SHA256", "").strip().lower()
        digest = hashlib.sha256()
        try:
            pos = index * state["chunk_size"]
            remaining = expected
            while remaining > 0:
                buf = self.rfile.read(min(IO_BLOCK, remaining))
                if not buf:
                    return self._error(400, "Upload was interrupted")
                digest.update(buf)
                view = memoryview(buf)
                written_total = 0
                while len(view):
                    written = os.pwrite(fd, view, pos + written_total)
                    view = view[written:]
                    written_total += written
                pos += len(buf)
                remaining -= len(buf)
        except OSError as e:
            status = 507 if e.errno == errno.ENOSPC else 500
            return self._error(status, "Could not save this part")
        finally:
            os.close(fd)

        # The part is only recorded if its bytes match the checksum the browser computed.
        # A mismatch is not recorded, so the browser sends the part again.
        if claimed and claimed != digest.hexdigest():
            return self._error(422, "Part arrived damaged; sending it again")

        with _upload_lock:
            state = load_upload(uid)
            if state is None:
                return self._error(404, "Upload was cancelled")
            if index not in state["received"]:
                state["received"].append(index)
            state["updated"] = int(time.time())
            save_upload(state)
        self._json(200, {"ok": True, "received": len(state["received"])})

    def _complete_upload(self, uid):
        # Step 1: make sure every part has arrived.
        with _upload_lock:
            state = load_upload(uid)
            if state is None:
                return self._error(404, "Upload not found")
            if state.get("completed"):  # retry after a lost response
                return self._json(201, state["completed"])
            total = total_chunks(state["size"], state["chunk_size"])
            have = set(state["received"])
            missing = [i for i in range(total) if i not in have]
            if missing:
                return self._json(
                    409,
                    {"error": f"{len(missing)} part(s) still missing", "missing": missing[:100]},
                )

        # Step 2: check the size and move the file into place. The checksum is
        # computed in the background, so this request returns at once even for
        # very large files (a proxy may cut off a request that runs for minutes).
        part = part_path(uid)
        with _upload_lock:
            state = load_upload(uid)
            if state is None:
                return self._error(404, "Upload not found")
            if state.get("completed"):
                return self._json(201, state["completed"])
            try:
                if os.path.getsize(part) != state["size"]:
                    return self._error(500, "Stored size does not match the upload")
                file_id = uuid.uuid4().hex
                os.replace(part, os.path.join(FILES_DIR, file_id))
            except FileNotFoundError:
                return self._error(404, "Upload not found")
            except OSError:
                return self._error(500, "Could not finish the upload")

            meta = {
                "id": file_id,
                "name": state["name"],
                "path": state["path"],
                "size": state["size"],
                "sha256": None,  # filled in by the background hashing thread
                "uploaded_at": int(time.time()),
            }
            with _index_lock:
                index = load_index()
                index[file_id] = meta
                save_index(index)

            state["completed"] = meta
            state["updated"] = int(time.time())
            save_upload(state)
        start_hashing(file_id)
        self._json(201, meta)


def main():
    if not APP_PASSWORD:
        sys.exit("ERROR: APP_PASSWORD is not set. Set it in the environment or .env file.")
    if len(APP_PASSWORD) < 8:
        sys.exit("ERROR: APP_PASSWORD must be at least 8 characters.")
    if CHUNK_BYTES <= 0:
        sys.exit("ERROR: CHUNK_MB must be at least 1.")

    os.makedirs(FILES_DIR, exist_ok=True)
    os.makedirs(UPLOADS_DIR, exist_ok=True)
    cleanup_stale_uploads()
    # Files finished just before a restart may still lack a checksum.
    for file_id, meta in load_index().items():
        if meta.get("sha256") is None and os.path.isfile(os.path.join(FILES_DIR, file_id)):
            start_hashing(file_id)

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    free_gb = shutil.disk_usage(DATA_DIR).free / GB
    print(
        f"File store on port {PORT}, data in {DATA_DIR} ({free_gb:.0f} GB free), "
        f"chunk size {CHUNK_BYTES // MB} MB",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
