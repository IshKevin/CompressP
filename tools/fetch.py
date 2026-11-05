#!/usr/bin/env python3
"""Fast, resumable downloader for the File Store. Standard library only.

Downloads one stored file over several connections at once, so a fast link is
used fully, and resumes an interrupted download from where it stopped.

Examples:
  python fetch.py http://192.168.1.50:8080 --list
  python fetch.py http://192.168.1.50:8080 "Backups/photos.zip" --out D:\\Restore
  python fetch.py http://192.168.1.50:8080 "Backups/photos.zip" --connections 12

The password is read from the FILESTORE_PASSWORD environment variable, or asked for.
Run the same command again after an interruption to continue.
"""

import argparse
import getpass
import hashlib
import http.cookiejar
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

PART_BYTES = 64 * 1024 * 1024
BLOCK = 1024 * 1024


def die(message):
    sys.exit("ERROR: " + message)


def make_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def login(opener, base, password):
    body = json.dumps({"password": password}).encode("utf-8")
    req = urllib.request.Request(
        base + "/api/login", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with opener.open(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        die("login failed (" + str(e.code) + "). Check the password.")


def get_json(opener, url):
    with opener.open(url, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def list_files(opener, base):
    return get_json(opener, base + "/api/files")["files"]


def choose_file(files, wanted):
    matches = [f for f in files if f.get("path") == wanted or f.get("id") == wanted]
    if not matches:
        die("no stored file called '" + wanted + "'. Use --list to see names.")
    if len(matches) > 1:
        die("more than one file matches; use the id instead: " + ", ".join(f["id"] for f in matches))
    return matches[0]


def fetch_part(opener, url, start, end, part_path, lock, progress):
    """Download bytes start..end into the .part file at the same offset, retrying on errors."""
    for attempt in range(1, 9):
        try:
            req = urllib.request.Request(url, headers={"Range": "bytes=%d-%d" % (start, end)})
            with opener.open(req, timeout=120) as r:
                if r.status != 206:
                    raise OSError("server did not return a partial response")
                with open(part_path, "r+b") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0:
                        block = r.read(min(BLOCK, remaining))
                        if not block:
                            raise OSError("connection closed early")
                        f.write(block)
                        remaining -= len(block)
                        progress(len(block))
            return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise  # a client error will not get better by retrying
            if attempt == 8:
                raise
        except OSError:
            if attempt == 8:
                raise
        time.sleep(min(30, 2 ** attempt))


def load_state(state_path, file_id, size):
    if os.path.exists(state_path):
        with open(state_path, "r", encoding="utf-8") as f:
            state = json.load(f)
        if state.get("id") == file_id and state.get("size") == size:
            return set(state.get("done", []))
    return set()


def save_state(state_path, file_id, size, done, lock):
    with lock:
        tmp = state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"id": file_id, "size": size, "done": sorted(done)}, f)
        os.replace(tmp, state_path)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8 * BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    ap = argparse.ArgumentParser(description="Fast, resumable File Store downloader.")
    ap.add_argument("base", help="server address, e.g. http://192.168.1.50:8080")
    ap.add_argument("name", nargs="?", help="file path as shown in the list, or its id")
    ap.add_argument("--list", action="store_true", help="list stored files and exit")
    ap.add_argument("--out", default=".", help="folder to save into (default: current folder)")
    ap.add_argument("--connections", type=int, default=8, help="parallel connections (default 8)")
    ap.add_argument("--overwrite", action="store_true", help="replace an existing file")
    ap.add_argument("--no-verify", action="store_true", help="skip the SHA-256 check at the end")
    args = ap.parse_args()

    base = args.base.rstrip("/")
    password = os.environ.get("FILESTORE_PASSWORD") or getpass.getpass("Password: ")
    opener = make_opener()
    login(opener, base, password)
    files = list_files(opener, base)

    if args.list:
        for f in sorted(files, key=lambda x: x.get("path", x["name"]).lower()):
            print("%-60s %12d bytes  id=%s" % (f.get("path", f["name"]), f["size"], f["id"]))
        return
    if not args.name:
        die("give a file name or id, or use --list")

    meta = choose_file(files, args.name)
    size = int(meta["size"])
    file_id = meta["id"]
    out_name = os.path.basename(meta["name"])
    final_path = os.path.join(args.out, out_name)
    part_path = final_path + ".part"
    state_path = part_path + ".state.json"
    url = base + "/api/files/" + urllib.parse.quote(file_id)

    if os.path.exists(final_path) and not args.overwrite:
        die(final_path + " already exists. Use --overwrite to replace it.")
    os.makedirs(args.out, exist_ok=True)

    done = load_state(state_path, file_id, size)
    if not os.path.exists(part_path) or os.path.getsize(part_path) != size:
        with open(part_path, "wb") as f:  # fresh start: reserve the full size
            f.truncate(size)
        done = set()
    total_parts = -(-size // PART_BYTES) if size else 0
    todo = [i for i in range(total_parts) if i not in done]
    print("Downloading %s (%.2f GB) with %d connections; %d of %d parts left"
          % (meta.get("path", out_name), size / 1e9, args.connections, len(todo), total_parts))

    lock = threading.Lock()
    counter = {"bytes": sum(min(PART_BYTES, size - i * PART_BYTES) for i in done)}
    started = time.time()
    last_print = [0.0]

    def progress(n):
        with lock:
            counter["bytes"] += n
            now = time.time()
            if now - last_print[0] >= 2 or counter["bytes"] >= size:
                last_print[0] = now
                elapsed = max(now - started, 1e-6)
                speed = counter["bytes"] / elapsed
                left = (size - counter["bytes"]) / speed if speed else 0
                sys.stdout.write("\r  %5.1f%%  %8.2f MB/s  ETA %s   "
                                 % (100.0 * counter["bytes"] / size, speed / 1e6,
                                    time.strftime("%H:%M:%S", time.gmtime(left))))
                sys.stdout.flush()

    if todo:
        with ThreadPoolExecutor(max_workers=max(1, args.connections)) as pool:
            futures = {}
            for i in todo:
                start = i * PART_BYTES
                end = min(size, start + PART_BYTES) - 1
                futures[pool.submit(fetch_part, opener, url, start, end, part_path, lock, progress)] = i
            try:
                for fut in as_completed(futures):
                    fut.result()  # re-raises a failed part
                    done.add(futures[fut])
                    save_state(state_path, file_id, size, done, lock)
            except BaseException:
                for fut in futures:
                    fut.cancel()
                print("\nStopped. Run the same command again to continue.")
                raise
    print()

    if not args.no_verify:
        expected = meta.get("sha256")
        if expected is None:
            print("Server has not finished its checksum yet; run again later to verify.")
        else:
            print("Checking SHA-256 ...")
            actual = sha256_file(part_path)
            if actual != expected:
                die("checksum mismatch! The file is kept as " + part_path + " for inspection.")
            print("Checksum OK: " + actual)

    os.replace(part_path, final_path)
    if os.path.exists(state_path):
        os.remove(state_path)
    print("Saved " + final_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted. Run the same command again to continue.")
