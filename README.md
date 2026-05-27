# HTMLHost

HTMLHost is a self-hosted HTML file hosting and sharing service built with Flask. It lets authenticated users upload `.html` and `.htm` files, organize them into nested folders, and publish time-limited share links for public access.

The project is designed to be simple to deploy and operate: a Flask app, SQLite by default, file-based storage for uploads, and an admin panel for user management.

## Features

- Upload and manage `.html` / `.htm` files
- Organize files in nested folders
- Generate public share links with optional expiration
- Preview shared HTML in a sandboxed iframe
- Admin panel for managing users and browsing user files
- Chinese and English interface, with Chinese as the default language
- Lightweight deployment with Flask + Gunicorn + Nginx

## Tech Stack

- Python 3
- Flask
- Flask-Login
- Flask-SQLAlchemy
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

On first launch, the app initializes the database automatically. The first registered user is intended to complete the initial setup flow and become the administrator.

## Production Run

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

Other built-in defaults:

- Upload directory: `uploads/`
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
├── app.py              # Flask app factory and blueprint registration
├── auth.py             # Authentication and first-user setup
├── dashboard.py        # File, folder, upload, and share management
├── share.py            # Public shared page routes
├── admin.py            # Admin panel routes
├── models.py           # SQLAlchemy models
├── config.py           # Application configuration
├── i18n.py             # Translation loading and language switching
├── templates/          # Jinja2 templates
├── translations/       # Chinese and English translations
├── uploads/            # Uploaded HTML files
├── htmlhost.service    # Example systemd service
└── nginx.conf          # Example Nginx reverse proxy config
```

## How It Works

- Uploaded files are stored on disk under `uploads/<user_id>/...`
- Folder hierarchy is stored in the database through a self-referential `Folder` model
- Public sharing uses token-based links such as `/s/<token>`
- Shared pages are rendered inside a sandboxed iframe for safer previewing

## Dependencies

Main Python dependencies:

- Flask
- Flask-SQLAlchemy
- Flask-Login
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

