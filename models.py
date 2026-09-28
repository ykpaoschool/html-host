import hashlib
import hmac
import secrets
from datetime import datetime, timezone

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint

db = SQLAlchemy()


def _is_expired(expires_at):
    """True if expires_at is in the past.

    SQLite strips tzinfo on read, so an expiry stored as UTC-aware comes back
    naive; coerce naive values to UTC before comparing against now(utc) to
    avoid TypeError (which would 500 the share viewer).
    """
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) > expires_at


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(256), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=True)
    auth_provider = db.Column(db.String(32), default="local", nullable=False)
    display_name = db.Column(db.String(128), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    # Gates the whole /api/v1 plane (both auth modes). Default on so the MCP
    # integration works out of the box; admins can disable individual users.
    mcp_enabled = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    folders = db.relationship("Folder", backref="user", lazy=True, cascade="all, delete-orphan")
    files = db.relationship("File", backref="user", lazy=True, cascade="all, delete-orphan")
    projects = db.relationship(
        "Project", backref="user", lazy=True, cascade="all, delete-orphan"
    )
    api_tokens = db.relationship(
        "ApiToken", backref="user", lazy=True, cascade="all, delete-orphan"
    )


class Folder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    parent_id = db.Column(db.Integer, db.ForeignKey("folder.id"), nullable=True)
    name = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    parent = db.relationship("Folder", remote_side=[id], backref="children")
    files = db.relationship("File", backref="folder", lazy=True, cascade="all, delete-orphan")

    def get_path(self):
        parts = []
        current = self
        while current:
            parts.append(current.name)
            current = current.parent
        return "/".join(reversed(parts))


class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    folder_id = db.Column(db.Integer, db.ForeignKey("folder.id"), nullable=True)
    name = db.Column(db.String(256), nullable=False)
    storage_path = db.Column(db.String(512), nullable=False)
    size = db.Column(db.Integer, default=0)
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    share_links = db.relationship("ShareLink", backref="file", lazy=True, cascade="all, delete-orphan")


class ShareLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), nullable=False)
    token = db.Column(db.String(128), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = db.Column(db.Boolean, default=True)
    require_login = db.Column(db.Boolean, default=False)

    def is_expired(self):
        return _is_expired(self.expires_at)


class Project(db.Model):
    """A group of HTML + static assets shared as a single unit with real URLs,
    so relative links between pages resolve correctly (unlike single-file srcdoc)."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = db.Column(
        db.DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    files = db.relationship(
        "ProjectFile", backref="project", lazy=True, cascade="all, delete-orphan"
    )
    share_links = db.relationship(
        "ProjectShareLink", backref="project", lazy=True, cascade="all, delete-orphan"
    )


class ProjectFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    # Project-relative path, e.g. "css/app.css". Uses "/" separators regardless of OS.
    path = db.Column(db.String(512), nullable=False)
    # Disk-relative path under UPLOAD_FOLDER.
    storage_path = db.Column(db.String(512), nullable=False)
    size = db.Column(db.Integer, default=0)
    uploaded_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (UniqueConstraint("project_id", "path", name="uq_project_file_path"),)


class ProjectShareLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("project.id"), nullable=False)
    token = db.Column(db.String(128), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    is_active = db.Column(db.Boolean, default=True)
    require_login = db.Column(db.Boolean, default=False)

    def is_expired(self):
        return _is_expired(self.expires_at)


class AppSetting(db.Model):
    """Admin-configurable key/value settings.

    Holds deployment-level configuration that admins edit at runtime rather
    than through environment variables, so it survives restarts without a
    redeploy. Currently ``public_base_url`` (see api.get_public_base_url) and,
    from PR 3, the MCP shared secret.

    Created by db.create_all(); no migration script needed.
    """

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.String(512), nullable=False)


# --- Personal Access Tokens (API / MCP auth) -------------------------------
#
# Format: hh_<43-char urlsafe body>_<8-char checksum>
#
# Only the SHA-256 of the full token is stored. PATs are high-entropy random
# strings, so there is no dictionary attack surface and the hashing happens on
# every API call - bcrypt's deliberately slow KDF would be pure overhead here.
# The checksum lets us reject malformed tokens without a database round-trip.

TOKEN_PREFIX = "hh_"


def _token_checksum(body):
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]


def generate_api_token():
    """Return (plaintext, prefix, hash) for a new token.

    The plaintext is only ever returned here - it is never persisted.
    """
    body = secrets.token_urlsafe(32)
    plaintext = f"{TOKEN_PREFIX}{body}_{_token_checksum(body)}"
    return plaintext, f"{TOKEN_PREFIX}{body[:8]}", hash_api_token(plaintext)


def hash_api_token(plaintext):
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def is_well_formed_api_token(plaintext):
    """Shape + checksum check, no DB access."""
    if not plaintext.startswith(TOKEN_PREFIX):
        return False
    # rpartition, not split: the urlsafe alphabet includes "_", so the body may
    # contain underscores and only the final one separates the checksum.
    body, sep, checksum = plaintext[len(TOKEN_PREFIX):].rpartition("_")
    if not sep or not body or not checksum:
        return False
    return hmac.compare_digest(checksum, _token_checksum(body))


class ApiToken(db.Model):
    """A user-created Personal Access Token for the /api/v1 plane."""

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(128), nullable=False)
    token_hash = db.Column(db.String(128), unique=True, nullable=False)
    token_prefix = db.Column(db.String(16), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at = db.Column(db.DateTime, nullable=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    # Reserved for v1 fine-grained scopes; not enforced yet.
    scopes = db.Column(db.String(256), nullable=True)

    def is_expired(self):
        return _is_expired(self.expires_at)
