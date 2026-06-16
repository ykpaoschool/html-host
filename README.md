# HTMLHost

HTMLHost is a self-hosted HTML file hosting and sharing service built with Flask. It lets authenticated users upload `.html` and `.htm` files, organize them into nested folders, and publish time-limited share links for public access.

The project is designed to be simple to deploy and operate: a Flask app, SQLite by default, file-based storage for uploads, and an admin panel for user management.

## Features

- Upload and manage `.html` / `.htm` files
- Organize files in nested folders, with rename/move sync across disk and database
- Generate public share links with optional expiration
- Preview shared HTML in a sandboxed iframe
- Microsoft Entra ID (Azure AD) SSO login via OAuth 2.0
- Admin panel for managing users and browsing user files
- Automatic database schema migration on startup
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

```bash
export MICROSOFT_CLIENT_ID="your-client-id"
export MICROSOFT_CLIENT_SECRET="your-client-secret"
export MICROSOFT_TENANT_ID="your-tenant-id"
```

When these are configured, a "Sign in with Microsoft" button appears on the login page. SSO users are created automatically on first login with `is_admin=False`. If any variable is left empty (the default), SSO is disabled.

## Production Run

### Option A: Docker (Recommended)

Build and run with Docker:

```bash
docker build -t htmlhost .

docker run -d \
  --name htmlhost \
  -p 5001:5001 \
  -v htmlhost-data:/opt/htmlhost/data \
  -e SECRET_KEY="replace-this-with-a-secure-secret" \
  htmlhost
```

Or use Docker Compose:

```bash
# Edit SECRET_KEY in docker-compose.yml first
docker compose up -d
```

The application will be available at `http://localhost:5001`.

**Data persistence:** The `/opt/htmlhost/data` directory inside the container holds both the SQLite database (`data.db`) and uploaded files (`uploads/`). Mount it as a volume to persist data across container rebuilds.

**Environment variables for Docker:**

| Variable | Docker Default | Description |
| --- | --- | --- |
| `SECRET_KEY` | *(required)* | Flask secret key — must be set in production |
| `DATABASE_URL` | `sqlite:////opt/htmlhost/data/data.db` | SQLAlchemy database URL |
| `UPLOAD_FOLDER` | `/opt/htmlhost/data/uploads` | Directory for uploaded files |
| `MICROSOFT_CLIENT_ID` | `""` | Microsoft SSO OAuth client ID |
| `MICROSOFT_CLIENT_SECRET` | `""` | Microsoft SSO OAuth client secret |
| `MICROSOFT_TENANT_ID` | `""` | Microsoft Entra ID tenant ID |

**Enable Microsoft SSO:**

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
| `SECRET_KEY` | Flask secret key | `dev-secret-key-change-in-production` |
| `DATABASE_URL` | SQLAlchemy database URL | `sqlite:///data.db` |
| `UPLOAD_FOLDER` | Directory for uploaded files | `uploads/` (relative to project root) |
| `MICROSOFT_CLIENT_ID` | Microsoft SSO OAuth client ID | `""` (SSO disabled) |
| `MICROSOFT_CLIENT_SECRET` | Microsoft SSO OAuth client secret | `""` |
| `MICROSOFT_TENANT_ID` | Microsoft Entra ID tenant ID | `""` |

Other built-in defaults:

- Maximum upload size: 50 MB
- Default language: `zh`

Example:

```bash
export SECRET_KEY="replace-this-in-production"
export DATABASE_URL="sqlite:///data.db"
./run.sh prod
```

## Project Structure

```text
.
├── app.py              # Flask app factory, blueprint registration, schema migration
├── auth.py             # Authentication, first-user setup, Microsoft SSO
├── dashboard.py        # File, folder, upload, and share management
├── share.py            # Public shared page routes
├── admin.py            # Admin panel routes
├── models.py           # SQLAlchemy models (User, Folder, File, ShareLink)
├── config.py           # Application configuration
├── i18n.py             # Translation loading and language switching
├── run.sh              # Dev/prod launcher script
├── requirements.txt    # Python dependencies
├── templates/          # Jinja2 templates
├── translations/       # Chinese and English translations
├── uploads/            # Uploaded HTML files
├── TRADEMARK.md        # Trademark policy
├── htmlhost.service    # Example systemd service
└── nginx.conf          # Example Nginx reverse proxy config
```

## How It Works

- Uploaded files are stored on disk under `uploads/<user_id>/...`; folder renames and moves sync both the database and the filesystem
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

