# HTMLHost

HTMLHost is a self-hosted HTML file hosting and sharing service built with Flask. It lets authenticated users upload `.html` and `.htm` files, organize them into nested folders, and publish time-limited share links for public access.

The project is designed to be simple to deploy and operate: a Flask app, SQLite by default, file-based storage for uploads, and an admin panel for user management.

## Features

- Upload and manage `.html` / `.htm` files
- Organize files in nested folders, with rename/move sync across disk and database
- Edit uploaded files in the browser in place, keeping existing share links valid
- Create new files from the dashboard and write them in the editor
- Keep the last 10 versions of an edited file, preview any of them, and roll back to one
- Preview the unsaved draft in a sandboxed frame before saving it
- Generate public share links with optional expiration
- Preview shared HTML in a sandboxed iframe
- Publish multi-file projects (HTML plus CSS, JS, images) served over real URLs
- JSON API at `/api/v1` for scripts and CI, with personal access tokens
- MCP server so agents can publish and manage links — see [MCP Integration](#mcp-integration)
- Microsoft Entra ID (Azure AD) SSO login via OAuth 2.0
- Admin panel for managing users and browsing user files
- Automatic database schema migration on startup
- Release versioning: the running version is shown in the UI, and every merge publishes a versioned container image
- Chinese and English interface, with Chinese as the default language
- Lightweight deployment with Flask + Gunicorn + Nginx

## Tech Stack

- Python 3
- Flask
- Flask-Login
- Flask-SQLAlchemy
- Authlib (Microsoft SSO OAuth)
- SQLite by default
- Tailwind CSS and Alpine.js via CDN

## Quick Start

### 1. Clone the repository

```bash
git clone <your-repo-url>
cd htmlhost
```

### 2. Start the app

The helper script creates a virtual environment and installs dependencies automatically when `venv` does not exist.

```bash
./run.sh
```

The development server runs on:

```text
http://127.0.0.1:5001
```

You can also install dependencies manually and run the app directly:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python app.py
```

### 3. First-time setup

On first launch, the app initializes the database automatically. The first registered user completes the initial setup flow and becomes the administrator.

### 4. (Optional) Enable Microsoft SSO

Set the following environment variables to enable Microsoft Entra ID (Azure AD) single sign-on:

| Variable | Description |
| --- | --- |
| `MICROSOFT_CLIENT_ID` | Azure AD application client ID. **When empty (the default), SSO is disabled.** |
| `MICROSOFT_CLIENT_SECRET` | Azure AD application client secret |
| `MICROSOFT_TENANT_ID` | Azure AD tenant ID. Use `"common"` to allow any Microsoft account (multi-tenant) |

**Bare metal:**

```bash
export MICROSOFT_CLIENT_ID="your-client-id"
export MICROSOFT_CLIENT_SECRET="your-client-secret"
export MICROSOFT_TENANT_ID="your-tenant-id"
```

**Docker:**

```bash
docker run -d \
  --name htmlhost \
  -p 5001:5001 \
  -v htmlhost-data:/opt/htmlhost/data \
  -e SECRET_KEY="your-secret-key" \
  -e MICROSOFT_CLIENT_ID="your-client-id" \
  -e MICROSOFT_CLIENT_SECRET="your-client-secret" \
  -e MICROSOFT_TENANT_ID="your-tenant-id" \
  htmlhost
```

**Docker Compose:** Uncomment and fill in the SSO variables in `docker-compose.yml`:

```yaml
environment:
  - SECRET_KEY=your-secret-key
  - MICROSOFT_CLIENT_ID=your-client-id
  - MICROSOFT_CLIENT_SECRET=your-client-secret
  - MICROSOFT_TENANT_ID=your-tenant-id
```

When SSO is enabled, a "Sign in with Microsoft" button appears on the login page. SSO users are created automatically on first login with `is_admin=False`. SSO users have no local password and must sign in through Microsoft. If a local account already uses the SSO user's email, that SSO login is rejected.

## Production Run

### Option A: Docker (Recommended)

Build and run with Docker:

```bash
docker build -t htmlhost .

docker run -d \
  --name htmlhost \
  -p 5001:5001 \
  -v htmlhost-data:/opt/htmlhost/data \
  -e SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  htmlhost
```

Or use Docker Compose:

```bash
# SECRET_KEY is read from the shell or an .env file next to docker-compose.yml;
# compose refuses to start without it, and so does the app itself.
SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" docker compose up -d
```

The application will be available at `http://localhost:5001`.

**Data persistence:** The `/opt/htmlhost/data` directory inside the container holds both the SQLite database (`data.db`) and uploaded files (`uploads/`). Mount it as a volume to persist data across container rebuilds.

The *host* side of that mount is yours to choose; the container side is not:

```bash
# Named volume (what docker-compose.yml uses) - Docker picks the location.
-v htmlhost-data:/opt/htmlhost/data

# Bind mount - you pick the location, and can see and back it up directly.
-v /opt/apps/htmlhost/data:/opt/htmlhost/data
```

`/opt/htmlhost/data` is fixed: it is baked into the image's `DATABASE_URL` and `UPLOAD_FOLDER`, and `entrypoint.sh` chowns it before dropping privileges. Only the part before the `:` varies — and `DATA_DIR` is read by nothing, so changing it has no effect.

Pick a bind mount when the data should live at a path you control (a per-app directory under `/opt/apps`, an existing backup target, a network mount) that you can `tar` or `rsync` without going through Docker; pick a named volume when you would rather Docker own the location and lifecycle. The application behaves identically either way.

**When switching an existing deployment from a named volume to a bind mount, copy the data across first.** The new mount starts empty, and the app will create a fresh `data.db` in it and send you through `/setup` again rather than reporting the old database missing.

**Environment variables for Docker:**

| Variable | Docker Default | Description |
| --- | --- | --- |
| `SECRET_KEY` | *(required)* | Flask secret key — the app refuses to start without a real value |
| `DATABASE_URL` | `sqlite:////opt/htmlhost/data/data.db` | SQLAlchemy database URL |
| `UPLOAD_FOLDER` | `/opt/htmlhost/data/uploads` | Directory for uploaded files |
| `MICROSOFT_CLIENT_ID` | `""` | Azure AD client ID (SSO disabled when empty) |
| `MICROSOFT_CLIENT_SECRET` | `""` | Azure AD client secret |
| `MICROSOFT_TENANT_ID` | `""` | Azure AD tenant ID (`"common"` for multi-tenant) |

**Enable Microsoft SSO:** See the [Optional) Enable Microsoft SSO](#4-optional-enable-microsoft-sso) section above for Docker and Docker Compose examples.

**Run behind a reverse proxy:** Use the `nginx.conf` in this repository as a reference. Point the upstream to `http://localhost:5001` (or the appropriate host/port if customized).

### Option B: Bare Metal

Run with Gunicorn:

```bash
./run.sh prod
```

This starts the application on port `5000`.

The repository also includes:

- `htmlhost.service` for systemd deployment
- `nginx.conf` as an example reverse proxy configuration

## Configuration

Configuration is provided through environment variables.

| Variable | Description | Default |
| --- | --- | --- |
| `SECRET_KEY` | Flask secret key | *(refuses to start on the dev default or the compose placeholder)* |
| `SESSION_COOKIE_SECURE` | Mark the session cookie `Secure` | `true` in the container and `./run.sh prod`; `false` otherwise |
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite:///data.db` |
| `UPLOAD_FOLDER` | Directory for uploaded files | `uploads/` (relative to project root) |
| `MICROSOFT_CLIENT_ID` | Azure AD client ID (SSO disabled when empty) | `""` (SSO disabled) |
| `MICROSOFT_CLIENT_SECRET` | Azure AD client secret | `""` |
| `MICROSOFT_TENANT_ID` | Azure AD tenant ID (`"common"` for multi-tenant) | `""` |

Other built-in defaults:

- Maximum upload size: 50 MB
- Default language: `zh`

Example:

```bash
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export DATABASE_URL="sqlite:///data.db"
./run.sh prod
```

## MCP Integration

`htmlhost-mcp` is a separate MCP server that lets an agent publish HTML to HTMLHost and manage the
links it creates. It is a stateless proxy in front of `/api/v1`: it holds no user data, parses no
credentials, and forwards each caller's own auth headers to HTMLHost, which remains the single place
that decides who the caller is.

It supports two transports:

| Transport | For | How it authenticates |
| --- | --- | --- |
| `streamable-http` | one shared endpoint for a whole open-webui instance | a shared secret plus the acting user's email, injected by open-webui per request |
| `stdio` | Claude Code and other single-user agents | a personal access token of your own |

The server lives in [`mcp-server/`](mcp-server/) — see its
[README](mcp-server/README.md) for the full configuration reference and troubleshooting.

### Setting it up for open-webui

Requires **open-webui 0.11.4 or newer**.

**1. Configure HTMLHost.** Sign in as an administrator and open **`/admin/mcp`**:

- Set **Public base URL** to the address users reach HTMLHost at (`https://html.example.com`).
  Share links are built from it, so without it the agent returns links that only work from inside
  the server.
- Generate a **shared secret**. It is shown once — copy it now.

**2. Deploy the MCP server.** Pull the image published by CI (see `.github/workflows/mcp-build.yml`):

```bash
docker run -d --name htmlhost-mcp \
  -p 127.0.0.1:8000:8000 \
  -e HTMLHOST_URL=https://html.example.com \
  -e MCP_ALLOWED_HOSTS=html.example.com \
  --restart unless-stopped \
  ghcr.io/ykpaoschool/html-host-mcp:latest
```

**3. Put it behind your reverse proxy** on the same domain as HTMLHost, at `/mcp`. Streamable HTTP
is a long-lived streaming transport, so the proxy needs explicit settings — and nginx's 1 MB default
body limit will block any publish larger than about 0.9 MB:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8000;   # no trailing slash: /mcp must survive
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;         # must match MCP_ALLOWED_HOSTS
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 300s;
    client_max_body_size 10m;
}
```

Nginx Proxy Manager has no fields for these: add `/mcp` under **Custom Locations** and paste the
block above into that location's **Advanced** tab, **minus the `proxy_set_header Host` line** — NPM
writes that header (and the `X-Forwarded-*` ones) into every custom location itself, and nginx
appends a repeated header rather than overriding it, so a second copy sends two `Host` headers and
every request is answered `400 Invalid HTTP request received.` See
[mcp-server/README.md](mcp-server/README.md#nginx-proxy-manager) for the click-by-click version.

**4. Register it in open-webui** under **Settings → Admin Settings → Integrations → External Tool
Servers → Add**:

| Field | Value |
| --- | --- |
| Type | MCP (Streamable HTTP) |
| URL | `https://html.example.com/mcp` |
| Auth | None |
| Headers | `{"X-HtmlHost-Key": "<shared secret>", "X-HtmlHost-User": "{{USER_EMAIL}}"}` |

`{{USER_EMAIL}}` is substituted by open-webui server-side, so each user's work is filed under their
own HTMLHost account with no per-user setup. A user with no HTMLHost account is refused with an
error carrying the sign-in link — under SSO, signing in once creates the account. Accounts are never
created implicitly. Administrators can disable any user's API access from the admin user list.

### Using it from Claude Code instead

No shared endpoint is needed; the client starts the process and talks over stdio:

```bash
pip install ./mcp-server     # or: uvx --from ./mcp-server htmlhost-mcp

export HTMLHOST_URL=https://html.example.com
export HTMLHOST_PAT=hh_xxxxxxxx      # Settings → API tokens
claude mcp add htmlhost \
  --env HTMLHOST_URL=https://html.example.com \
  --env HTMLHOST_PAT=hh_xxxxxxxx \
  -- htmlhost-mcp
```

### Limits and scope

- One tool call carries up to **3 MiB** of content. Larger documents must be published through the
  web UI, which accepts up to 10 MB per file; the error says so.
- Agents can publish, update and unshare, but **cannot delete** content. Only share links can be
  revoked, and the content behind them survives.
- Cross-user access always answers "not found", never "forbidden", so an agent cannot probe for
  content that exists.

## Versioning

The `VERSION` file in the repository root is the single hand-maintained source of truth. It holds the semantic base version, bumped by hand in the pull request that warrants it — a **minor bump for a new feature** (`0.2.0` -> `0.3.0`), a **patch bump for a fix or touch-up** (`0.2.0` -> `0.2.1`), never automatically:

```text
0.2.0
```

Every merge to `main` derives a unique, immutable full version on top of it (see `.github/workflows/docker-build.yml`):

```text
X.Y.Z-build.42.sha.abc1234
  │     │        └── short commit SHA
  │     └────────── GitHub Actions run number (monotonic)
  └──────────────── base version from VERSION
```

That exact string is baked into the image as `APP_VERSION` **and** used as the registry tag, so the version printed in the UI is the tag you pull:

```bash
docker pull ghcr.io/ykpaoschool/html-host:X.Y.Z-build.42.sha.abc1234
```

Tags published per merge:

| Tag | Meaning |
| --- | --- |
| `latest` | most recent merge to `main` |
| `X.Y.Z` | most recent build of the current base version (moves on every build) |
| `X.Y.Z-build.42.sha.abc1234` | that exact build, immutable — pin your deployment to this to get a real rollback target |
| `sha-abc1234` | commit-addressed alias |

Both images are versioned this way. The application image and the MCP server image
(`ghcr.io/ykpaoschool/html-host-mcp`, built by `.github/workflows/mcp-build.yml`) derive from the
same `VERSION` file and the same commit, so the **base version and the SHA** identify a release
across both. Their `build.N` differs: `GITHUB_RUN_NUMBER` counts per workflow, and the MCP workflow
is newer, so one release's two images read (real values from the 0.2.0 release)
`0.2.0-build.17.sha.a865120` and `0.2.0-build.2.sha.a865120`. Pair them by SHA, not by full version
string. Each image reports its own string — the application in the UI, the MCP server in its MCP
handshake.

To release a new version, edit `VERSION` in your branch; the next merge to `main` picks it up. A plain `docker build` (no `APP_VERSION` build arg) and the local dev server both report `<VERSION>-dev` — that suffix means "not a released build".

The version appears on the login page, so it can be checked without an account, and at the bottom of the sidebar once signed in.

## Project Structure

```text
.
├── app.py              # Flask app factory, blueprint registration, schema migration
├── auth.py             # Authentication, first-user setup, Microsoft SSO
├── dashboard.py        # File, folder, upload, and share management
├── share.py            # Public shared page routes
├── admin.py            # Admin panel routes
├── api.py              # JSON API at /api/v1, with its own authentication
├── models.py           # SQLAlchemy models (User, Folder, File, ShareLink, Project, ApiToken)
├── config.py           # Application configuration
├── i18n.py             # Translation loading and language switching
├── version.py          # Resolves the release version the app reports
├── VERSION             # Hand-maintained base version (see Versioning)
├── run.sh              # Dev/prod launcher script
├── requirements.txt    # Python dependencies
├── mcp-server/         # MCP server package (see MCP Integration)
├── templates/          # Jinja2 templates
├── translations/       # Chinese and English translations
├── uploads/            # Uploaded HTML files
├── TRADEMARK.md        # Trademark policy
├── htmlhost.service    # Example systemd service
└── nginx.conf          # Example Nginx reverse proxy config
```

## How It Works

- Uploaded files are stored on disk under `uploads/<user_id>/...`; folder renames and moves sync both the database and the filesystem
- Version history lives beside them under `uploads/.history/<user_id>/<file_id>/`, inside the same data volume
- Folder hierarchy is stored in the database through a self-referential `Folder` model
- Public sharing uses token-based links such as `/s/<token>`
- Shared pages are rendered inside a sandboxed iframe for safer previewing
- Users authenticate either with a local password or via Microsoft Entra ID SSO; SSO users have no password stored locally
- On startup, the app automatically migrates missing database columns and rebuilds tables when needed (preserving foreign-key and unique constraints)

## Dependencies

Main Python dependencies:

- Flask
- Flask-SQLAlchemy
- Flask-Login
- Authlib
- requests
- bcrypt
- gunicorn

Install them with:

```bash
venv/bin/pip install -r requirements.txt
```

## Notes

- Only `.html` and `.htm` files are accepted
- Online editing needs a UTF-8 encoded file of at most 2 MB; larger or differently encoded files can still be uploaded, downloaded and shared
- The default database is SQLite and is created automatically on startup
- There is currently no dedicated test suite or lint configuration in this repository

## License

This project is licensed under the GNU Affero General Public License v3.0 
(AGPLv3). In short:

- ✅ You may use, modify, and distribute this software for any purpose
- ✅ Educational use is explicitly welcome
- ⚠️ If you modify and distribute this software (including as a network service),
     you MUST release your changes under the same license
- ℹ️ The project name and logo are protected trademarks — see [TRADEMARK.md](TRADEMARK.md)

