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
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    folders = db.relationship("Folder", backref="user", lazy=True, cascade="all, delete-orphan")
    files = db.relationship("File", backref="user", lazy=True, cascade="all, delete-orphan")
    projects = db.relationship(
        "Project", backref="user", lazy=True, cascade="all, delete-orphan"
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

    def is_expired(self):
        return _is_expired(self.expires_at)
