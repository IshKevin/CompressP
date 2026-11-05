# File Store

A small, password-protected web app for storing files on your server while you
reinstall your PC. Log in, upload your files, and download
them back later.

- No third-party Python packages (standard library only)
- Large files (tested in chunks up to hundreds of GB) upload in parts that resume after a dropped connection
- Downloads resume after an interruption (HTTP Range)
- Any file type can be uploaded. Zip a folder first to keep it as one file.
- Files are stored byte-for-byte as uploaded, with their names and folder paths unchanged
- Every part is checksum-verified, and each stored file gets a SHA-256 you can compare with your PC
- The password comes from an environment variable, never from the code

## Deploy with Coolify

1. Create a new resource from this repository (Docker Compose build pack).
2. In the environment variables, set `APP_PASSWORD` (at least 8 characters).
   Set `DATA_PATH` only if you want a specific folder, such as `/srv/filestore`.
3. Under the service, set a domain (for example `files.example.com`) and port `8080`.
   Coolify then serves it over HTTPS automatically.
4. Set `COOKIE_SECURE=1` in the environment variables.
5. Deploy.

Leave `HOST_PORT` empty in Coolify. Coolify then routes your domain to the app, so no
fixed host port is used and nothing conflicts with other apps on the server.

## Deploy on the server

1. Install Docker and the Docker Compose plugin.
2. Copy this folder to the server.
3. Create the data folder on a disk with plenty of free space, owned by the
   container user (ID 1000):

   ```bash
   sudo mkdir -p /srv/filestore
   sudo chown 1000:1000 /srv/filestore
   ```

4. Create the config file, then set the password and the `DATA_PATH` above:

   ```bash
   cp .env.example .env
   nano .env        # set APP_PASSWORD (at least 8 characters), DATA_PATH and HOST_PORT=8080
   ```

5. Start it:

   ```bash
   docker compose up -d --build
   ```

6. Open `http://<server-ip>:8080`, log in, and upload.

Useful commands:

```bash
docker compose logs -f          # view logs
docker compose restart          # restart after changing .env
docker compose down             # stop (files are kept)
docker compose down -v          # stop AND delete all stored files (careful)
```

## Using it

- **Upload files:** click "Choose files", or drop them on the page.
- **Upload a folder:** click "Choose folder". The folder structure is kept.
- **Connection dropped or page closed?** Log in again, choose the same files or
  folder, and the upload continues from the last sent part. Unfinished uploads
  are listed on the page. Unfinished uploads with no activity for 72 hours are deleted automatically.
- **Keep the page open** and keep the PC awake until the upload finishes.
- **Download:** click "Download". Interrupted downloads resume in most browsers
  (or use a download manager that supports resuming).
- **Delete:** click "Delete". This cannot be undone.

## Checking that files are unchanged

Each stored file shows its SHA-256 in the list. To compare with the original on your PC:

- **Windows (PowerShell):** `Get-FileHash "C:\path\to\file" -Algorithm SHA256`
- **Linux / macOS:** `sha256sum /path/to/file`

Compare the value with the one in the File Store list. They must match exactly.
The per-part checksums are computed in the browser, which only works over HTTPS or
`localhost`. Over plain `http://<ip>` the parts still arrive, but the part check is skipped.

## Fast downloads (use this for big files)

A browser downloads over one connection, which is slow for large files. The
downloader in `tools/fetch.py` uses several connections at once and resumes after a
dropped connection. It needs only Python 3 on your PC (no packages).

```bash
# list the stored files
python tools/fetch.py http://192.168.1.50:8080 --list

# download one file (asks for the password, or set FILESTORE_PASSWORD)
python tools/fetch.py http://192.168.1.50:8080 "Backups/photos.zip" --out D:\Restore --connections 8
```

- If it stops, run the same command again. It continues from the last finished part.
- At the end it checks the SHA-256 against the server's value.
- Use `--connections 1` to compare with a normal single download.

On Windows PowerShell use `python` instead of `python3`.

## Security notes

- **On a private network, plain HTTP is fine.** Use HTTPS only when the server is reachable from the internet. Put a reverse
  proxy in front (Caddy, nginx, or Nginx Proxy Manager) with a certificate, then
  set `COOKIE_SECURE=1` in `.env`. Without HTTPS your password travels in plain text.
- Keep `.env` private. It is in `.gitignore` and `.dockerignore`.
- Failed logins are limited to 5 per IP every 15 minutes.
- Login lasts `SESSION_TTL_HOURS` (default 12). Logging out clears the browser
  cookie.
- Behind a reverse proxy, failed-login limits apply to the proxy's IP, so
  brute-force protection is weaker. Keep the password long.

### If you use a reverse proxy (recommended for HTTPS)

Uploads send 64 MB parts, so the proxy must allow bodies that large and must not
time out on slow uploads. Examples:

- **Caddy** (`Caddyfile`): no changes needed for uploads; Caddy has no body limit by default.
  ```
  files.example.com {
      reverse_proxy localhost:8080
  }
  ```
- **nginx**: add inside the `server` block:
  ```
  client_max_body_size 200m;
  proxy_request_buffering off;
  proxy_read_timeout 600s;
  proxy_send_timeout 600s;
  ```

Without these, uploads fail with 413 errors or stall.

## Configuration

| Variable          | Default | Meaning                                         |
|-------------------|---------|-------------------------------------------------|
| `APP_PASSWORD`    | (none)  | Required. At least 8 characters.                |
| `SESSION_SECRET`  | random  | Optional fixed key for login cookies.           |
| `MAX_FILE_GB`     | 0       | Largest single file in GB. 0 = no limit.        |
| `CHUNK_MB`        | 8       | Size of upload parts. Keep at 8 or lower: the proxy times out requests after about 60 s. |
| `DISK_RESERVE_GB` | 0       | If above 0, refuse uploads that leave less free space. |
| `COOKIE_SECURE`   | 0       | Set to 1 when served over HTTPS.                |
| `SESSION_TTL_HOURS` | 12    | Idle hours before you need to log in again.     |
| `DATA_PATH`       | (volume)| Host folder for stored files (in `.env`).       |
| `HOST_PORT`       | 8080    | Port on the host that maps to the app.          |

## Running without Docker (for testing)

```bash
APP_PASSWORD='a-long-password' DATA_DIR=./data python3 app.py
```

Then open http://localhost:8080.
