# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HTMLHost is a self-hosted HTML file hosting and sharing service. Authenticated users upload `.html`/`.htm` files, organize them into nested folders, and share them via time-limited public links. Admin panel for user management. Bilingual UI (Chinese/English, default: Chinese).

## Commands

```bash
# Dev server (port 5001, debug mode)
./run.sh
# or directly:
venv/bin/python app.py

# Production (gunicorn, port 5000)
./run.sh prod

# Install dependencies
venv/bin/pip install -r requirements.txt
```

No tests, linter, or build step exist in this project.

## Architecture

**Entry point**: `app.py` — Flask app factory (`create_app()`), initializes SQLAlchemy, Flask-Login, i18n, registers all blueprints, creates DB tables on startup.

**Four Flask blueprints**:

| Blueprint | File | URL Prefix | Responsibility |
|-----------|------|------------|----------------|
| `auth_bp` | `auth.py` | `/` | Login, first-user setup wizard, logout |
| `dashboard_bp` | `dashboard.py` | `/` | File/folder CRUD, upload, share link management |
| `share_bp` | `share.py` | `/` | Public share viewing (`/s/<token>`) |
| `admin_bp` | `admin.py` | `/admin` | User CRUD, user file management |

**Models** (`models.py`): User, Folder (self-referential parent for nesting), File, ShareLink. Shared `db = SQLAlchemy()` instance initialized in `create_app()`.

**I18n** (`i18n.py`): Loads `translations/{zh,en}.json`. Language stored in session, switchable via `?lang=` query param. Jinja2 filter `{{ "key" | t }}` for translations.

**File storage**: Uploaded files saved to `uploads/<user_id>/<folder_path>/<filename>`. Duplicate filenames get `_1`, `_2` suffixes. Only `.html`/`.htm` accepted.

**Frontend**: Jinja2 templates with `base.html` → `app_layout.html` → page templates. Tailwind CSS and Alpine.js via CDN (no local static assets). Shared files rendered in sandboxed `<iframe srcdoc="...">`.

**Database**: SQLite (`data.db`), auto-created via `db.create_all()` on startup.

**Config** (`config.py`): `SECRET_KEY` and `DATABASE_URL` from env vars (with dev defaults). 50MB max upload. Languages config.

**Production deployment**: `htmlhost.service` (systemd/gunicorn), `nginx.conf` (reverse proxy to port 5001).
