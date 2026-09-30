import os

from version import full_version

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


# Secret keys that must never reach a real deployment: the built-in dev
# default and the docker-compose.yml placeholder. create_app() refuses to
# start with any of them (or an empty value) - a known key makes the signed
# session cookie forgeable, i.e. a full authentication bypass.
INSECURE_SECRET_KEYS = frozenset(
    {
        "dev-secret-key-change-in-production",
        "replace-this-with-a-secure-secret",
    }
)


class Config:
    # Release version shown in the UI; see version.py for how it is derived.
    APP_VERSION = full_version()
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-in-production")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'data.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    LANGUAGES = {"zh": "中文", "en": "English"}
    DEFAULT_LANGUAGE = "zh"
    MICROSOFT_CLIENT_ID = os.environ.get("MICROSOFT_CLIENT_ID", "")
    MICROSOFT_CLIENT_SECRET = os.environ.get("MICROSOFT_CLIENT_SECRET", "")
    MICROSOFT_TENANT_ID = os.environ.get("MICROSOFT_TENANT_ID", "")
    # Public base URL used when building share / login links for API responses.
    # Fallback only: the primary source is the admin setting on /admin/mcp.
    PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
