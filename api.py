"""The /api/v1 plane: a JSON REST API for MCP servers, CI and scripts.

Registered in app.py with ``url_prefix="/api/v1"``. Every request is resolved
to a user before the view runs and the view reads ``g.api_user``.

Auth modes (see requirements §4.1):

1. **Personal Access Token** — ``Authorization: Bearer hh_...`` (PR 1).
2. **Trusted headers** — ``X-HtmlHost-Key`` + ``X-HtmlHost-User`` (PR 3), used
   by open-webui which injects ``{{USER_EMAIL}}`` server-side.

Both are gated by ``User.mcp_enabled``: a single rule for the whole plane.

Error contract: every failure is ``{"error": <CODE>, "message": <English>}``,
never HTML. Codes: UNAUTHORIZED, MCP_DISABLED, NOT_FOUND, METHOD_NOT_ALLOWED,
INVALID_REQUEST, INVALID_PATH, DUPLICATE_PATH, UNSUPPORTED_FILE_TYPE,
INVALID_EXPIRY, PAYLOAD_TOO_LARGE, TOO_MANY_FILES, PROJECT_TOO_LARGE.

Two deliberate absences: there is **no endpoint that deletes content** (files
or projects) — MCP may only publish and revoke links — and no cross-user
access is ever reported as 403; another user's resource reads as 404 so a
probe cannot tell "not yours" from "does not exist".
"""

import logging
import os
import secrets
from datetime import datetime, timezone

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    request,
    url_for,
)

from dashboard import _ensure_upload_dir, create_file
from models import (
    ApiToken,
    AppSetting,
    File,
    Folder,
    Project,
    ProjectFile,
    ProjectShareLink,
    ShareLink,
    User,
    db,
    hash_api_token,
    is_well_formed_api_token,
)
from projects import (
    MAX_FILE_SIZE,
    MAX_PROJECT_FILES,
    MAX_PROJECT_SIZE,
    _allowed_extension,
    _ensure_project_dir,
    _get_project_storage_path,
    _normalize_rel_path,
    _parse_duration,
    _parse_expiry,
    _persist_project,
)

api_bp = Blueprint("api", __name__)

logger = logging.getLogger(__name__)

# Per-call content ceiling. This is about what an agent can push through a tool
# argument (and therefore through the model's context), not about what the
# store can hold: the Web UI still accepts up to MAX_FILE_SIZE per file.
MAX_API_CONTENT_SIZE = 3 * 1024 * 1024  # 3 MiB

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

# Path prefix whose errors must render as JSON rather than Flask's HTML pages.
API_PATH_PREFIX = "/api/"


class ApiError(Exception):
    """Raised anywhere in the API plane; rendered as a uniform JSON body."""

    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


class ApiAuthError(ApiError):
    """Raised by the auth layer. Same body, default 401."""

    def __init__(self, code, message, status=401):
        super().__init__(code, message, status)


def _json_error(code, message, status):
    return jsonify({"error": code, "message": message}), status


@api_bp.errorhandler(ApiError)
def _handle_api_error(exc):
    logger.warning("API %s: %s (%s)", exc.code, exc.message, request.path)
    return _json_error(exc.code, exc.message, exc.status)


def register_json_error_handlers(app):
    """Keep /api/* failures JSON even when Flask, not a view, raises them.

    A blueprint error handler only fires for errors raised *inside* a view of
    that blueprint. An unmatched URL fails during routing and an oversized body
    fails during parsing, and in both cases ``request.blueprint`` is still None
    - so those would have escaped as HTML pages. These app-level handlers fill
    that gap, and hand anything outside /api/ back to Flask untouched.
    """

    def _api_only(status, code, message):
        def handler(exc):
            if not request.path.startswith(API_PATH_PREFIX):
                # Not our plane: returning the exception keeps Flask's
                # stock HTML page for the Web UI.
                return exc
            return _json_error(code, message, status)

        return handler

    app.register_error_handler(
        404, _api_only(404, "NOT_FOUND", "No such endpoint or resource.")
    )
    app.register_error_handler(
        405,
        _api_only(
            405, "METHOD_NOT_ALLOWED", "This HTTP method is not allowed here."
        ),
    )

    def _too_large(exc):
        if not request.path.startswith(API_PATH_PREFIX):
            return exc
        limit_mb = current_app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return _json_error(
            "PAYLOAD_TOO_LARGE",
            f"Request body is over the {limit_mb} MB limit. "
            "Publish larger files through the HTMLHost web UI.",
            413,
        )

    app.register_error_handler(413, _too_large)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


@api_bp.before_request
def authenticate():
    """Resolve the caller before every view; views read ``g.api_user``."""
    g.api_user = _resolve_user(request.headers)


def _resolve_user(headers):
    """Resolve the request's user, or raise ApiAuthError."""
    auth_header = headers.get("Authorization", "")
    if auth_header:
        return _resolve_pat(auth_header)

    raise ApiAuthError(
        "UNAUTHORIZED",
        "Authentication required. Send 'Authorization: Bearer <api-token>'.",
    )


def _resolve_pat(auth_header):
    scheme, _, token = auth_header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise ApiAuthError(
            "UNAUTHORIZED",
            "Malformed Authorization header; expected 'Bearer <api-token>'.",
        )
    # Reject malformed tokens on shape/checksum before touching the database.
    if not is_well_formed_api_token(token):
        raise ApiAuthError("UNAUTHORIZED", "Invalid API token.")

    row = ApiToken.query.filter_by(token_hash=hash_api_token(token)).first()
    if row is None or not row.is_active:
        raise ApiAuthError("UNAUTHORIZED", "Invalid or revoked API token.")
    if row.is_expired():
        raise ApiAuthError("UNAUTHORIZED", "This API token has expired.")

    user = db.session.get(User, row.user_id)
    if user is None:
        raise ApiAuthError("UNAUTHORIZED", "Invalid or revoked API token.")
    if not user.mcp_enabled:
        raise ApiAuthError(
            "MCP_DISABLED",
            "API access is disabled for this account. "
            "Ask an administrator to enable it.",
            status=403,
        )

    row.last_used_at = datetime.now(timezone.utc)
    db.session.commit()

    logger.info(
        "API auth ok: user_id=%s token_prefix=%s path=%s",
        user.id,
        row.token_prefix,
        request.path,
    )
    return user


# ---------------------------------------------------------------------------
# Outward-facing URLs
# ---------------------------------------------------------------------------

PUBLIC_BASE_URL_KEY = "public_base_url"


def get_public_base_url():
    """The configured outward-facing address, or None.

    Priority: admin setting (AppSetting) -> PUBLIC_BASE_URL env var -> None,
    in which case callers fall back to the request's own Host.
    """
    row = db.session.get(AppSetting, PUBLIC_BASE_URL_KEY)
    if row and row.value:
        return row.value.rstrip("/")
    return current_app.config.get("PUBLIC_BASE_URL") or None


def share_url(endpoint, **values):
    """Absolute URL for a share or login link.

    API endpoints must never call ``url_for(..., _external=True)`` directly:
    that reads the request's Host header, which under container networking is
    an internal name (``htmlhost:5001``) the user cannot open. Build the
    relative URL and prefix the configured public address instead.
    """
    base = get_public_base_url()
    if base:
        return base + url_for(endpoint, **values)
    # Fallback so a fresh deployment works unconfigured. The URL is only
    # correct for the host that made the request - configure /admin/mcp
    # (PR 3) or PUBLIC_BASE_URL in any real deployment.
    return url_for(endpoint, _external=True, **values)


# ---------------------------------------------------------------------------
# Lookups and ownership
#
# Every lookup is scoped to the caller. A miss - whether the row belongs to
# someone else or does not exist - is a 404, never a 403, so the API does not
# leak which resources exist.
# ---------------------------------------------------------------------------


def _file_or_404(file_id, user_id):
    file = File.query.filter_by(id=file_id, user_id=user_id).first()
    if file is None:
        raise ApiError("NOT_FOUND", f"No file with id {file_id}.", 404)
    return file


def _folder_or_404(folder_id, user_id):
    folder = Folder.query.filter_by(id=folder_id, user_id=user_id).first()
    if folder is None:
        raise ApiError("NOT_FOUND", f"No folder with id {folder_id}.", 404)
    return folder


def _project_or_404(project_id, user_id):
    project = Project.query.filter_by(id=project_id, user_id=user_id).first()
    if project is None:
        raise ApiError("NOT_FOUND", f"No project with id {project_id}.", 404)
    return project


def _share_or_404(share_id, user_id):
    """Resolve a composite share id ('file:5' / 'project:5') owned by the user.

    Share ids are composite because file links and project links live in
    separate tables, so a bare integer would be ambiguous.
    """
    target_type, link_id = _split_share_id(share_id)
    if target_type == "file":
        link = (
            ShareLink.query.join(File)
            .filter(ShareLink.id == link_id, File.user_id == user_id)
            .first()
        )
    else:
        link = (
            ProjectShareLink.query.join(Project)
            .filter(ProjectShareLink.id == link_id, Project.user_id == user_id)
            .first()
        )
    if link is None:
        raise ApiError("NOT_FOUND", f"No share link with id '{share_id}'.", 404)
    return link, target_type


def _split_share_id(raw):
    target_type, sep, raw_id = str(raw).partition(":")
    if not sep or target_type not in ("file", "project") or not raw_id.isdigit():
        raise ApiError(
            "INVALID_REQUEST",
            "Share ids look like 'file:5' or 'project:5'.",
        )
    return target_type, int(raw_id)


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _iso(value):
    """Render a stored datetime as an explicit UTC ISO-8601 string.

    SQLite returns naive values that are UTC by construction (models write
    them UTC-aware), so stamp the offset instead of emitting an ambiguous one.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _link_url(target_type, token):
    if target_type == "file":
        return share_url("share.view", token=token)
    return share_url("projects.view", token=token)


def _share_payload(link, target_type):
    return {
        "id": f"{target_type}:{link.id}",
        "target_type": target_type,
        "target_id": link.file_id if target_type == "file" else link.project_id,
        "token": link.token,
        "url": _link_url(target_type, link.token),
        "expires_at": _iso(link.expires_at),
        "expired": link.is_expired(),
        "is_active": bool(link.is_active),
        "require_login": bool(link.require_login),
        "created_at": _iso(link.created_at),
    }


def _file_payload(file, include_links=False):
    payload = {
        "id": file.id,
        "name": file.name,
        "folder_id": file.folder_id,
        "size": file.size,
        "uploaded_at": _iso(file.uploaded_at),
        "updated_at": _iso(file.updated_at),
    }
    if include_links:
        payload["share_links"] = [
            _share_payload(link, "file") for link in file.share_links
        ]
    return payload


def _project_payload(project, include_files=False):
    payload = {
        "id": project.id,
        "name": project.name,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
        "file_count": len(project.files),
        "total_size": sum(pf.size or 0 for pf in project.files),
    }
    if include_files:
        payload["files"] = [
            {"path": pf.path, "size": pf.size, "uploaded_at": _iso(pf.uploaded_at)}
            for pf in sorted(project.files, key=lambda pf: pf.path)
        ]
        payload["share_links"] = [
            _share_payload(link, "project") for link in project.share_links
        ]
    return payload


def _paginated(key, items, page, limit, total):
    return jsonify(
        {
            key: items,
            "page": page,
            "limit": limit,
            "total": total,
            "has_more": page * limit < total,
        }
    )


# ---------------------------------------------------------------------------
# Request parsing helpers
# ---------------------------------------------------------------------------


def _json_body():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ApiError(
            "INVALID_REQUEST",
            "Send a JSON object body with Content-Type: application/json.",
        )
    return payload


def _pagination():
    try:
        page = int(request.args.get("page", 1))
        limit = int(request.args.get("limit", DEFAULT_PAGE_SIZE))
    except (TypeError, ValueError):
        raise ApiError("INVALID_REQUEST", "'page' and 'limit' must be integers.")
    if page < 1 or limit < 1:
        raise ApiError("INVALID_REQUEST", "'page' and 'limit' must be positive.")
    return page, min(limit, MAX_PAGE_SIZE)


def _optional_bool(payload, key, default=False):
    """Read a boolean field strictly, so a stringly-typed 'false' cannot be
    silently read as truthy."""
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ApiError("INVALID_REQUEST", f"'{key}' must be a boolean.")
    return value


def _check_content_size(size):
    if size > MAX_API_CONTENT_SIZE:
        raise ApiError(
            "PAYLOAD_TOO_LARGE",
            f"Content is {size} bytes, over the {MAX_API_CONTENT_SIZE} byte limit "
            "(3 MiB is the maximum for a single API call). "
            "Publish larger files through the HTMLHost web UI.",
            413,
        )


def _resolve_expiry(payload):
    """Resolve expires_in / expires_at from a request body into a UTC datetime.

    Returns None when neither is given - no expiry, matching the Web UI
    default - or when the caller asked for 'never'. ``expires_in`` wins when
    both are present, since it is the one agents are told to use.
    """
    if payload.get("expires_in") is not None:
        try:
            delta = _parse_duration(payload["expires_in"])
        except ValueError:
            raise ApiError(
                "INVALID_EXPIRY",
                "expires_in must look like '30m', '24h', '7d' or 'never'.",
            )
        return None if delta is None else datetime.now(timezone.utc) + delta

    if payload.get("expires_at") is not None:
        try:
            return _parse_expiry(payload["expires_at"])
        except (TypeError, ValueError):
            raise ApiError(
                "INVALID_EXPIRY",
                "expires_at must be an ISO-8601 timestamp, "
                "e.g. '2026-10-01T12:00:00Z'.",
            )
    return None


def _entry_list(payload):
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        raise ApiError(
            "INVALID_REQUEST",
            "'files' must be a non-empty list of {path, content} objects.",
        )
    return files


def _stage_project_files(entries, existing_sizes=None):
    """Validate ``[{path, content}]`` and return ``(staged, final_sizes)``.

    ``staged`` is ``[(rel_path, bytes)]`` ready for _persist_project or a disk
    write. ``existing_sizes`` maps the project's current paths to their sizes so
    that the project-wide limits are checked against the state the request
    would leave behind, not just against the bytes being sent.
    """
    final_sizes = dict(existing_sizes or {})
    staged = []
    seen = set()
    request_bytes = 0

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ApiError(
                "INVALID_REQUEST",
                f"files[{index}] must be an object with 'path' and 'content'.",
            )
        path = entry.get("path")
        content = entry.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            raise ApiError(
                "INVALID_REQUEST",
                f"files[{index}] requires string 'path' and 'content'.",
            )

        norm = _normalize_rel_path(path)
        if norm is None:
            raise ApiError(
                "INVALID_PATH",
                f"files[{index}].path is empty, absolute, or escapes the project root.",
            )
        if norm in seen:
            raise ApiError("DUPLICATE_PATH", f"Duplicate path in request: {norm}.")
        seen.add(norm)
        if not _allowed_extension(norm):
            raise ApiError(
                "UNSUPPORTED_FILE_TYPE", f"File type not allowed: {norm}."
            )

        data = content.encode("utf-8")
        if len(data) > MAX_FILE_SIZE:
            raise ApiError(
                "PAYLOAD_TOO_LARGE",
                f"{norm} is {len(data)} bytes; the per-file limit is "
                f"{MAX_FILE_SIZE} bytes.",
                413,
            )
        request_bytes += len(data)
        staged.append((norm, data))
        final_sizes[norm] = len(data)

    _check_content_size(request_bytes)

    if len(final_sizes) > MAX_PROJECT_FILES:
        raise ApiError(
            "TOO_MANY_FILES",
            f"This would leave {len(final_sizes)} files in the project; "
            f"the limit is {MAX_PROJECT_FILES}.",
        )
    total = sum(final_sizes.values())
    if total > MAX_PROJECT_SIZE:
        raise ApiError(
            "PROJECT_TOO_LARGE",
            f"This would make the project {total} bytes; the limit is "
            f"{MAX_PROJECT_SIZE} bytes ({MAX_PROJECT_SIZE // (1024 * 1024)} MB).",
            413,
        )
    return staged, final_sizes


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@api_bp.route("/me")
def me():
    """Identity of the authenticated user, for debugging agent bindings."""
    user = g.api_user
    return jsonify(
        {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "is_admin": bool(user.is_admin),
            "mcp_enabled": bool(user.mcp_enabled),
        }
    )


# ---------------------------------------------------------------------------
# Files (single HTML documents)
# ---------------------------------------------------------------------------


@api_bp.route("/files", methods=["POST"])
def files_create():
    """Create a single HTML file. Body: {name, content, folder_id?}."""
    payload = _json_body()
    name = payload.get("name")
    content = payload.get("content")
    if not isinstance(name, str) or not name.strip():
        raise ApiError("INVALID_REQUEST", "'name' is required and must be a string.")
    if not isinstance(content, str):
        raise ApiError("INVALID_REQUEST", "'content' is required and must be a string.")
    if not name.lower().endswith((".html", ".htm")):
        raise ApiError(
            "UNSUPPORTED_FILE_TYPE", "Only .html and .htm files can be published."
        )

    folder_id = payload.get("folder_id")
    if folder_id is not None:
        if not isinstance(folder_id, int):
            raise ApiError("INVALID_REQUEST", "'folder_id' must be an integer or null.")
        # Unlike the Web UI's form field, where a stale id silently means
        # "root", an explicit API value must resolve - silently filing the
        # document somewhere else is worse than an error.
        _folder_or_404(folder_id, g.api_user.id)

    data = content.encode("utf-8")
    _check_content_size(len(data))

    # create_file() is the same path the Web UI upload uses, so both share the
    # filename-uniquifying rules.
    file, error = create_file(g.api_user.id, name, data, folder_id)
    if error:
        raise ApiError("INVALID_REQUEST", error)

    return jsonify(_file_payload(file, include_links=True)), 201


@api_bp.route("/files")
def files_list():
    """List the caller's files, newest first. Query: page, limit."""
    page, limit = _pagination()
    query = (
        File.query.filter_by(user_id=g.api_user.id)
        .order_by(File.uploaded_at.desc(), File.id.desc())
    )
    total = query.count()
    items = query.offset((page - 1) * limit).limit(limit).all()
    return _paginated("files", [_file_payload(f) for f in items], page, limit, total)


@api_bp.route("/files/<int:file_id>")
def file_get(file_id):
    """File details, including the share links already minted for it."""
    file = _file_or_404(file_id, g.api_user.id)
    return jsonify(_file_payload(file, include_links=True))


@api_bp.route("/files/<int:file_id>/content", methods=["PUT"])
def file_update_content(file_id):
    """Overwrite a file's bytes. Body: {content}. Existing links keep working."""
    file = _file_or_404(file_id, g.api_user.id)
    payload = _json_body()
    content = payload.get("content")
    if not isinstance(content, str):
        raise ApiError("INVALID_REQUEST", "'content' is required and must be a string.")

    data = content.encode("utf-8")
    _check_content_size(len(data))

    # _ensure_upload_dir recreates the parent if the folder was moved on disk.
    full_path = _ensure_upload_dir(file.storage_path)
    with open(full_path, "wb") as fh:
        fh.write(data)

    file.size = len(data)
    # Set explicitly: the column's onupdate only fires when a value actually
    # changes, so rewriting content with the same byte count would otherwise
    # leave updated_at stale even though the bytes did change.
    file.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify(_file_payload(file, include_links=True))


# ---------------------------------------------------------------------------
# Projects (HTML + assets served over real URLs)
# ---------------------------------------------------------------------------


@api_bp.route("/projects", methods=["POST"])
def projects_create():
    """Create a project. Body: {name, files:[{path, content}]}."""
    payload = _json_body()
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ApiError("INVALID_REQUEST", "'name' is required and must be a string.")

    staged, _ = _stage_project_files(_entry_list(payload))
    project = _persist_project(g.api_user.id, name.strip(), staged)

    return jsonify(_project_payload(project, include_files=True)), 201


@api_bp.route("/projects")
def projects_list():
    """List the caller's projects, newest first. Query: page, limit."""
    page, limit = _pagination()
    query = (
        Project.query.filter_by(user_id=g.api_user.id)
        .order_by(Project.created_at.desc(), Project.id.desc())
    )
    total = query.count()
    items = query.offset((page - 1) * limit).limit(limit).all()
    return _paginated(
        "projects", [_project_payload(p) for p in items], page, limit, total
    )


@api_bp.route("/projects/<int:project_id>")
def project_get(project_id):
    """Project details: file listing plus the share links already minted."""
    project = _project_or_404(project_id, g.api_user.id)
    return jsonify(_project_payload(project, include_files=True))


@api_bp.route("/projects/<int:project_id>/files", methods=["PUT"])
def project_files_upsert(project_id):
    """Add or overwrite project files by path. Body: {files:[{path, content}]}.

    Overwriting a path keeps every existing share link working, which is the
    point of addressing files by path rather than deleting and recreating them.
    """
    project = _project_or_404(project_id, g.api_user.id)
    payload = _json_body()

    existing = {pf.path: pf for pf in project.files}
    staged, _ = _stage_project_files(
        _entry_list(payload),
        existing_sizes={path: pf.size or 0 for path, pf in existing.items()},
    )

    for rel_path, data in staged:
        pf = existing.get(rel_path)
        storage_path = (
            pf.storage_path
            if pf
            else _get_project_storage_path(project.user_id, project.id, rel_path)
        )
        full_path = _ensure_project_dir(storage_path)
        with open(full_path, "wb") as fh:
            fh.write(data)
        if pf:
            pf.size = len(data)
        else:
            db.session.add(
                ProjectFile(
                    project_id=project.id,
                    path=rel_path,
                    storage_path=storage_path,
                    size=len(data),
                )
            )

    # Touching a child row does not bump the parent's onupdate timestamp.
    project.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify(_project_payload(project, include_files=True))


@api_bp.route("/projects/<int:project_id>/files/<path:rel_path>", methods=["DELETE"])
def project_file_delete(project_id, rel_path):
    """Delete one file inside a project. The project and its links survive."""
    project = _project_or_404(project_id, g.api_user.id)

    norm = _normalize_rel_path(rel_path)
    if norm is None:
        raise ApiError("INVALID_PATH", "Invalid project file path.")

    pf = ProjectFile.query.filter_by(project_id=project.id, path=norm).first()
    if pf is None:
        raise ApiError("NOT_FOUND", f"No file at '{norm}' in this project.", 404)

    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], pf.storage_path)
    if os.path.exists(full_path):
        os.remove(full_path)
    db.session.delete(pf)
    project.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify({"deleted": True, "path": norm, "project_id": project.id})


# ---------------------------------------------------------------------------
# Share links
# ---------------------------------------------------------------------------


@api_bp.route("/shares", methods=["POST"])
def shares_create():
    """Mint a share link.

    Body: {target_type, target_id, expires_in?|expires_at?, require_login?}.
    ``require_login`` defaults to False (public), matching the Web UI.
    """
    payload = _json_body()
    target_type = payload.get("target_type")
    target_id = payload.get("target_id")
    if target_type not in ("file", "project") or not isinstance(target_id, int):
        raise ApiError(
            "INVALID_REQUEST",
            "'target_type' must be 'file' or 'project' and 'target_id' an integer.",
        )

    # Resolve the target first: nothing should be minted for a resource the
    # caller does not own.
    if target_type == "file":
        _file_or_404(target_id, g.api_user.id)
    else:
        _project_or_404(target_id, g.api_user.id)

    token = secrets.token_urlsafe(32)
    expires_at = _resolve_expiry(payload)
    require_login = _optional_bool(payload, "require_login")

    if target_type == "file":
        link = ShareLink(
            file_id=target_id,
            token=token,
            expires_at=expires_at,
            require_login=require_login,
        )
    else:
        link = ProjectShareLink(
            project_id=target_id,
            token=token,
            expires_at=expires_at,
            require_login=require_login,
        )
    db.session.add(link)
    db.session.commit()

    return jsonify(_share_payload(link, target_type)), 201


@api_bp.route("/shares")
def shares_list():
    """List the caller's share links across both kinds, newest first."""
    page, limit = _pagination()
    user_id = g.api_user.id

    links = [
        (link, "file")
        for link in ShareLink.query.join(File).filter(File.user_id == user_id)
    ] + [
        (link, "project")
        for link in ProjectShareLink.query.join(Project).filter(
            Project.user_id == user_id
        )
    ]
    # ISO strings sort correctly and, unlike the raw datetimes, never mix
    # naive and aware values.
    links.sort(
        key=lambda pair: (_iso(pair[0].created_at) or "", pair[0].id),
        reverse=True,
    )

    total = len(links)
    window = links[(page - 1) * limit : page * limit]
    return _paginated(
        "shares",
        [_share_payload(link, kind) for link, kind in window],
        page,
        limit,
        total,
    )


@api_bp.route("/shares/<share_id>")
def share_get(share_id):
    link, target_type = _share_or_404(share_id, g.api_user.id)
    return jsonify(_share_payload(link, target_type))


@api_bp.route("/shares/<share_id>", methods=["PATCH"])
def share_update(share_id):
    """Adjust a link. Body: any of {expires_in|expires_at, require_login, is_active}.

    Passing ``expires_at: null`` (or ``expires_in: "never"``) clears the expiry.
    """
    link, target_type = _share_or_404(share_id, g.api_user.id)
    payload = _json_body()

    if "expires_in" in payload or "expires_at" in payload:
        link.expires_at = _resolve_expiry(payload)
    if "require_login" in payload:
        link.require_login = _optional_bool(payload, "require_login")
    if "is_active" in payload:
        link.is_active = _optional_bool(payload, "is_active", True)

    db.session.commit()
    return jsonify(_share_payload(link, target_type))


@api_bp.route("/shares/<share_id>", methods=["DELETE"])
def share_delete(share_id):
    """Revoke a share link. The file or project it pointed at is untouched."""
    link, target_type = _share_or_404(share_id, g.api_user.id)
    target_id = link.file_id if target_type == "file" else link.project_id

    db.session.delete(link)
    db.session.commit()

    return jsonify(
        {
            "deleted": True,
            "id": share_id,
            "target_type": target_type,
            "target_id": target_id,
        }
    )
