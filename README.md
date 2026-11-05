# File Store

A small, password-protected web app for storing files on your server while you
reinstall your PC. Log in, upload your files (zip folders first), and download
them back later.

- No third-party Python packages (standard library only)
- Files are kept in a Docker volume (`filestore-data`)
- The password comes from an environment variable, never from the code
- Single-file uploads of any type, with SHA-256 checksums shown in the list

## Deploy on the server

1. Install Docker and the Docker Compose plugin.
2. Copy this folder to the server.
3. Create the config file and set a strong password:

   ```bash
   cp .env.example .env
   nano .env        # set APP_PASSWORD (at least 8 characters)
   ```

4. Start it:

   ```bash
   docker compose up -d --build
   ```

5. Open `http://<server-ip>:8080`, log in, and upload.

Useful commands:

```bash
docker compose logs -f          # view logs
docker compose restart          # restart after changing .env
docker compose down             # stop (files are kept)
docker compose down -v          # stop AND delete all stored files (careful)
```

## Using it

- **Upload:** drag files onto the page or click "Choose files". For a folder,
  zip it on your PC first (right-click > Send to > Compressed folder on Windows).
- **Download:** click "Download" next to a file. Check the SHA-256 if you want to
  confirm the file is intact.
- **Delete:** click "Delete". This cannot be undone.

## Security notes

- **Use HTTPS when the server is reachable from the internet.** Put a reverse
  proxy in front (Caddy, nginx, or Nginx Proxy Manager) with a certificate, then
  set `COOKIE_SECURE=1` in `.env`. Without HTTPS your password travels in plain text.
- Keep `.env` private. It is in `.gitignore` and `.dockerignore`.
- Failed logins are limited to 5 per IP every 15 minutes.
- Login lasts `SESSION_TTL_HOURS` (default 12). Logging out clears the browser
  cookie.
- Behind a reverse proxy, failed-login limits apply to the proxy's IP, so
  brute-force protection is weaker. Keep the password long.

## Configuration

| Variable          | Default | Meaning                                         |
|-------------------|---------|-------------------------------------------------|
| `APP_PASSWORD`    | (none)  | Required. At least 8 characters.                |
| `SESSION_SECRET`  | random  | Optional fixed key for login cookies.           |
| `MAX_UPLOAD_MB`   | 5120    | Largest single file, in MB.                     |
| `COOKIE_SECURE`   | 0       | Set to 1 when served over HTTPS.                |
| `SESSION_TTL_HOURS` | 12    | Hours before you need to log in again.          |
| `HOST_PORT`       | 8080    | Port on the host that maps to the app.          |

## Running without Docker (for testing)

```bash
APP_PASSWORD='a-long-password' DATA_DIR=./data python3 app.py
```

Then open http://localhost:8080.
