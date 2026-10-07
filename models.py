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
    # Newest first, which is the order the editor's history panel lists them in.
    # "all, delete-orphan" is what keeps FileRevision rows from outliving their
    # file: all four File deletion paths go through the ORM. The blobs the rows
    # point at are *not* covered by this - see dashboard.delete_file_history.
    revisions = db.relationship(
        "FileRevision",
        backref="file",
        lazy=True,
        cascade="all, delete-orphan",
        order_by="FileRevision.created_at.desc()",
    )


class FileRevision(db.Model):
    """A previous version of a File's bytes, kept so an edit can be undone.

    The row points at a blob under UPLOAD_FOLDER/.history/<user_id>/<file_id>/,
    indexed by file id rather than by path so renaming or moving the file keeps
    its history (and leaves no directory nothing points at). That directory
    sits outside the user's own tree - _is_valid_component_name lets a user
    create a folder literally named ".history", so nesting it inside <user_id>/
    would let the two collide. See editor-implementation.md 1.4.

    The blob is named after this row's primary key, so it is written after the
    row has been flushed. Rows are created by editor._apply_content and removed
    either by the cascade above or by its retention pruning.
    """

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.Integer, db.ForeignKey("file.id"), nullable=False)
    storage_path = db.Column(db.String(512), nullable=False)
    size = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))


def content_revision(data):
    """The SHA-256 of a file's bytes: how both planes name one version of it.

    Defined once, and deliberately not in either plane. The editor hands this
    value to the browser as the optimistic-lock token a save must be based on,
    and ``GET /api/v1/files/<id>/content`` hands the same string to API clients
    (requirements F11), so the two must agree byte for byte - a second
    implementation would drift and tell a client that a file it had just read
    had changed underneath it.

    A hash rather than ``updated_at``: SQLite returns naive datetimes (so every
    comparison needs timezone normalization) and a file edited on disk behind
    the app's back never updates the column at all. Always computed from the
    bytes on disk, never reconstructed from a size or a timestamp.
    """
    return hashlib.sha256(data).hexdigest()


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
    """A group of HTML + static assets shared as a single unit, so relative
    links between pages all resolve within one shared artifact."""

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
    redeploy. Currently ``public_base_url`` (see api.get_public_base_url) and
    ``mcp_shared_secret_hash`` (see api.MCP_SECRET_KEY), which stores only the
    hash - the key name says so to keep anyone from reading it as a secret.

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


# --- MCP shared secret (trusted-header auth) -------------------------------
#
# The secret authenticates the *gateway* (open-webui) rather than a person: it
# is what entitles the caller to assert someone else's identity through
# X-HtmlHost-User. Stored hashed for the same reason as PATs - it is a
# high-entropy random string, so SHA-256 has no dictionary attack surface and
# the comparison runs on every API call.
#
# The hash lives in AppSetting under api.MCP_SECRET_KEY.


def hash_mcp_secret(plaintext):
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_mcp_secret():
    """Return (plaintext, hash) for a new shared secret.

    The plaintext is only ever returned here - it is never persisted, so it can
    be read exactly once, at generation time.
    """
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_mcp_secret(plaintext)
