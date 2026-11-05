"""Server tests for the File Store. Run with: python3 tests/test_server.py

Starts app.py on a free local port with a temporary data folder and checks:
uploads in parts, resume, checksums, range downloads, file names, and deletes.
"""

import hashlib
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
PASSWORD = "correct-horse-1"
MB = 1024 * 1024
results = []


def check(name, cond, extra=""):
    results.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  [%s]" % (extra,)))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def call(self, method, path, body=None, headers=None, raw=False):
        req = urllib.request.Request(self.base + path, method=method, data=body, headers=headers or {})
        try:
            with self.opener.open(req, timeout=60) as r:
                data = r.read()
                return r.status, (data if raw else _json(data)), r.headers
        except urllib.error.HTTPError as e:
            data = e.read()
            return e.code, (data if raw else _json(data)), e.headers

    def json_post(self, path, obj):
        return self.call("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"})


def _json(data):
    try:
        return json.loads(data)
    except ValueError:
        return data


def sha(data):
    return hashlib.sha256(data).hexdigest()


def upload(c, path, data, part_size):
    """Create, send every part with a checksum, and complete. Returns the completion body."""
    st, up, _ = c.json_post("/api/uploads", {"path": path, "size": len(data)})
    assert st == 201, (st, up)
    uid = up["upload_id"]
    total = -(-len(data) // part_size) if data else 0
    for i in range(total):
        part = data[i * part_size:(i + 1) * part_size]
        st, _, _ = c.call("POST", "/api/uploads/%s/chunks/%d" % (uid, i), part,
                          {"Content-Type": "application/octet-stream", "X-Chunk-SHA256": sha(part)})
        assert st == 200, (st, i)
    st, meta, _ = c.call("POST", "/api/uploads/%s/complete" % uid)
    assert st == 201, (st, meta)
    return meta


def wait_for_checksum(c, file_id):
    for _ in range(600):
        st, lst, _ = c.call("GET", "/api/files")
        for f in lst["files"]:
            if f["id"] == file_id and f.get("sha256"):
                return f
        time.sleep(0.2)
    raise SystemExit("checksum never appeared")


def run_suite(base, data_dir):
    c = Client(base)
    st, _, _ = c.call("GET", "/api/files")
    check("anonymous request refused", st == 401, st)
    st, _, _ = c.json_post("/api/login", {"password": "wrong-password"})
    check("wrong password refused", st == 401, st)
    st, _, h = c.json_post("/api/login", {"password": PASSWORD})
    check("login accepted", st == 200, st)

    # --- exact content, names, and checksum ---
    part = 1 * MB
    payload = os.urandom(3 * MB + 123457)
    path = "Work Files/2024 (final) v2 .draft/résumé 日本 & co.pdf"
    st, up, _ = c.json_post("/api/uploads", {"path": path, "size": len(payload)})
    check("create upload with spaces, unicode and dots", st == 201, (st, up))
    uid = up["upload_id"]
    total = -(-len(payload) // part)
    for i in range(total):
        chunk = payload[i * part:(i + 1) * part]
        c.call("POST", "/api/uploads/%s/chunks/%d" % (uid, i), chunk,
               {"Content-Type": "application/octet-stream", "X-Chunk-SHA256": sha(chunk)})
    st, meta, _ = c.call("POST", "/api/uploads/%s/complete" % uid)
    check("complete returns right away with checksum pending", st == 201 and meta.get("sha256") is None, meta)
    check("path kept exactly", meta.get("path") == path, meta.get("path"))
    meta = wait_for_checksum(c, meta["id"])
    check("server checksum matches original", meta["sha256"] == sha(payload), meta.get("sha256"))
    st, data, h = c.call("GET", "/api/files/" + meta["id"], raw=True)
    check("download is byte-identical", st == 200 and data == payload)
    check("download keeps unicode name", "r%C3%A9sum%C3%A9" in (h.get("Content-Disposition") or ""))
    st, part_bytes, h = c.call("GET", "/api/files/" + meta["id"],
                               headers={"Range": "bytes=5-4999"}, raw=True)
    check("range download is exact", st == 206 and part_bytes == payload[5:5000], st)
    st, tail, _ = c.call("GET", "/api/files/" + meta["id"], headers={"Range": "bytes=-25"}, raw=True)
    check("suffix range is exact", st == 206 and tail == payload[-25:], st)
    st, _, _ = c.call("GET", "/api/files/" + meta["id"], headers={"Range": "bytes=%d-" % (len(payload) + 9)}, raw=True)
    check("range past the end gives 416", st == 416, st)

    # --- resume after an interruption ---
    data2 = os.urandom(4 * MB)
    st, up2, _ = c.json_post("/api/uploads", {"path": "resume/test.zip", "size": len(data2)})
    uid2 = up2["upload_id"]
    for i in range(2):
        chunk = data2[i * part:(i + 1) * part]
        c.call("POST", "/api/uploads/%s/chunks/%d" % (uid2, i), chunk,
               {"Content-Type": "application/octet-stream", "X-Chunk-SHA256": sha(chunk)})
    st, body, _ = c.call("POST", "/api/uploads/%s/complete" % uid2)
    check("complete with missing parts refused (409)", st == 409 and body.get("missing") == [2, 3], (st, body))
    st, status, _ = c.call("GET", "/api/uploads/" + uid2)
    check("status shows the parts already received", status.get("received") == [0, 1], status)
    st, lst, _ = c.call("GET", "/api/uploads")
    check("unfinished list shows the upload", any(u["upload_id"] == uid2 for u in lst["uploads"]))
    for i in range(2, 4):
        chunk = data2[i * part:(i + 1) * part]
        c.call("POST", "/api/uploads/%s/chunks/%d" % (uid2, i), chunk,
               {"Content-Type": "application/octet-stream", "X-Chunk-SHA256": sha(chunk)})
    st, meta2, _ = c.call("POST", "/api/uploads/%s/complete" % uid2)
    check("resumed upload completes", st == 201, (st, meta2))
    check("completing twice is safe", c.call("POST", "/api/uploads/%s/complete" % uid2)[0] == 201)
    meta2 = wait_for_checksum(c, meta2["id"])
    check("resumed file is exact", c.call("GET", "/api/files/" + meta2["id"], raw=True)[1] == data2)

    # --- damaged part is refused and not recorded ---
    st, up3, _ = c.json_post("/api/uploads", {"path": "damaged.zip", "size": part})
    bad = os.urandom(part)
    st, _, _ = c.call("POST", "/api/uploads/%s/chunks/0" % up3["upload_id"], bad,
                      {"Content-Type": "application/octet-stream", "X-Chunk-SHA256": sha(b"other")})
    check("wrong checksum refused (422)", st == 422, st)
    st, status, _ = c.call("GET", "/api/uploads/" + up3["upload_id"])
    check("damaged part not recorded", status.get("received") == [], status)
    c.call("POST", "/api/uploads/%s/chunks/0" % up3["upload_id"], bad,
           {"Content-Type": "application/octet-stream", "X-Chunk-SHA256": sha(bad)})
    st, meta3, _ = c.call("POST", "/api/uploads/%s/complete" % up3["upload_id"])
    check("resent part completes exactly", st == 201)

    # --- unsafe paths refused, safe ones kept ---
    for bad_path in ["../x.zip", "a/../../etc/x", "/abs/x", "a//b", "a/./b", "dir\\file.txt", "ctrl\x01.txt", "", "a/"]:
        st, _, _ = c.json_post("/api/uploads", {"path": bad_path, "size": 1})
        check("unsafe path refused: %r" % (bad_path,), st == 400, st)
    st, up4, _ = c.json_post("/api/uploads", {"path": " spaced name . .zip", "size": 0})
    st, m4, _ = c.call("POST", "/api/uploads/%s/complete" % up4["upload_id"])
    check("leading and trailing spaces kept", m4.get("name") == " spaced name . .zip", m4)

    # --- empty file ---
    st, up5, _ = c.json_post("/api/uploads", {"path": "empty.txt", "size": 0})
    st, m5, _ = c.call("POST", "/api/uploads/%s/complete" % up5["upload_id"])
    m5 = wait_for_checksum(c, m5["id"])
    check("empty file checksum is correct", m5["sha256"] == sha(b""))

    # --- abort, delete, and cleanup ---
    st, up6, _ = c.json_post("/api/uploads", {"path": "abort.bin", "size": 3})
    st, _, _ = c.call("POST", "/api/uploads/%s/abort" % up6["upload_id"])
    check("abort removes the upload", st == 200)
    st, _, _ = c.call("GET", "/api/uploads/" + up6["upload_id"])
    check("aborted upload is gone", st == 404, st)
    st, _, _ = c.call("POST", "/api/files/%s/delete" % meta["id"])
    st2, _, _ = c.call("GET", "/api/files/" + meta["id"], raw=True)
    check("delete removes the file", st == 200 and st2 == 404, (st, st2))

    st, _, h = c.call("GET", "/api/files")
    check("session is renewed on use", any("session=" in v for v in (h.get_all("Set-Cookie") or [])))
    st, _, h = c.call("POST", "/api/logout")
    check("logout clears the cookie", st == 200 and any("Max-Age=0" in v for v in (h.get_all("Set-Cookie") or [])))


def start_server(data_dir, **extra):
    port = free_port()
    env = dict(os.environ, APP_PASSWORD=PASSWORD, DATA_DIR=data_dir, PORT=str(port), CHUNK_MB="1",
               DISK_RESERVE_GB="0")
    env.update(extra)
    proc = subprocess.Popen([sys.executable, APP], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/healthz", timeout=1)
            return proc, base
        except Exception:
            time.sleep(0.1)
    proc.kill()
    raise SystemExit("server did not start")


def main():
    tmp = tempfile.mkdtemp(prefix="filestore-test-")
    proc, base = start_server(os.path.join(tmp, "data"))
    try:
        run_suite(base, os.path.join(tmp, "data"))
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    proc, base = start_server(os.path.join(tmp, "data-zip-limit"), MAX_FILE_GB="1")
    try:
        c = Client(base)
        c.json_post("/api/login", {"password": PASSWORD})
        st, _, _ = c.json_post("/api/uploads", {"path": "huge.bin", "size": 2 * 1024 ** 3})
        check("file over MAX_FILE_GB refused (413)", st == 413, st)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    shutil.rmtree(tmp, ignore_errors=True)
    passed = sum(1 for _, ok in results if ok)
    print("\n%d/%d checks passed" % (passed, len(results)))
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
