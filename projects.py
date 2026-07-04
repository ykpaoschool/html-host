import io
import os
import secrets
import shutil
import zipfile

from flask import (
    Blueprint,
    abort,
    current_app,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from i18n import t
from models import Project, ProjectFile, ProjectShareLink, db

projects_bp = Blueprint("projects", __name__)

# Upload limits (per project).
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB per file
MAX_PROJECT_SIZE = 50 * 1024 * 1024  # 50 MB total per project
MAX_PROJECT_FILES = 100  # max number of files per project

# Allowed file extensions for project assets.
ALLOWED_EXTENSIONS = frozenset(
    {
        ".html",
        ".htm",
        ".css",
        ".js",
        ".mjs",
        ".map",
        ".json",
        ".xml",
        ".txt",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".svg",
        ".webp",
        ".ico",
        ".bmp",
        ".woff",
        ".woff2",
        ".ttf",
        ".eot",
        ".otf",
        ".csv",
        ".md",
    }
)


def _get_project_storage_path(user_id, project_id, rel_path):
    """Disk-relative path: uploads/{user_id}/projects/{project_id}/{rel_path}.

    rel_path uses '/' separators and is assumed already normalized + safe.
    """
    return os.path.join(
        str(user_id), "projects", str(project_id), rel_path.replace("/", os.sep)
    )


def _ensure_project_dir(storage_path):
    """Ensure the parent directory of storage_path exists, return absolute path."""
    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], storage_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    return full_path


def _normalize_rel_path(raw):
    """Normalize a project-relative path to posix style.

    Returns the normalized path, or None if the path is empty, absolute,
    or escapes the project root (contains '..' after normalization).
    """
    if not raw:
        return None
    # Accept both slash styles (Windows zips may use backslashes).
    raw = raw.replace("\\", "/")
    # Reject absolute paths.
    if raw.startswith("/"):
        return None
    norm = os.path.normpath(raw)
    if not norm or norm == "." or os.path.isabs(norm):
        return None
    parts = norm.split(os.sep)
    if any(p in ("..", "") for p in parts):
        return None
    return norm.replace(os.sep, "/")


def _allowed_extension(path):
    return os.path.splitext(path)[1].lower() in ALLOWED_EXTENSIONS


def _persist_project(user_id, name, staged):
    """Create the Project row, write all staged files to disk, and commit.

    staged is a list of (rel_path, content_bytes). On DB failure the
    project directory is removed so no orphan files remain.
    Returns the committed Project.
    """
    project = Project(user_id=user_id, name=name)
    db.session.add(project)
    db.session.flush()  # populate project.id before writing files

    try:
        for rel_path, content in staged:
            storage_path = _get_project_storage_path(user_id, project.id, rel_path)
            full_path = _ensure_project_dir(storage_path)
            with open(full_path, "wb") as out:
                out.write(content)
            db.session.add(
                ProjectFile(
                    project_id=project.id,
                    path=rel_path,
                    storage_path=storage_path,
                    size=len(content),
                )
            )
        db.session.commit()
    except Exception:
        db.session.rollback()
        # Clean up any files already written for this project.
        project_dir = os.path.join(
            current_app.config["UPLOAD_FOLDER"],
            str(user_id),
            "projects",
            str(project.id),
        )
        if os.path.isdir(project_dir):
            shutil.rmtree(project_dir, ignore_errors=True)
        raise
    return project


def _validate_file_entry(rel_path, size, total_so_far, count_so_far):
    """Validate a single file entry against whitelist + limits.

    Returns (error_message_or_None, new_total). error_message is a plain
    string already passed through t().
    """
    norm = _normalize_rel_path(rel_path)
    if norm is None:
        return t("project_invalid_path"), total_so_far
    if not _allowed_extension(norm):
        return t("project_invalid_extension", path=norm), total_so_far
    if size > MAX_FILE_SIZE:
        return t("project_upload_too_large", limit=MAX_FILE_SIZE // (1024 * 1024)), total_so_far
    if count_so_far + 1 > MAX_PROJECT_FILES:
        return t("project_upload_too_many", limit=MAX_PROJECT_FILES), total_so_far
    new_total = total_so_far + size
    if new_total > MAX_PROJECT_SIZE:
        return t("project_upload_too_large", limit=MAX_PROJECT_SIZE // (1024 * 1024)), total_so_far
    return None, new_total


@projects_bp.route("/projects/upload", methods=["POST"])
@login_required
def upload_files():
    """Batch multi-file upload. Form fields: name, files[].

    Returns JSON {id, name, file_count, total_size} on success.
    """
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"error": t("project_name_required")}), 400

    uploaded = request.files.getlist("files")
    if not uploaded or not any(f and f.filename for f in uploaded):
        return jsonify({"error": t("project_no_files")}), 400

    # Stage entries (validate before writing anything to disk/DB).
    staged = []  # list of (rel_path, content_bytes)
    total = 0
    seen_paths = set()
    for f in uploaded:
        if not f or not f.filename:
            continue
        content = f.read()
        size = len(content)
        err, total = _validate_file_entry(f.filename, size, total, len(staged))
        if err:
            return jsonify({"error": err, "path": f.filename}), 400
        norm = _normalize_rel_path(f.filename)
        if norm in seen_paths:
            return jsonify({"error": t("project_duplicate_path", path=norm)}), 400
        seen_paths.add(norm)
        staged.append((norm, content))

    project = _persist_project(current_user.id, name, staged)
    return jsonify(
        {
            "id": project.id,
            "name": project.name,
            "file_count": len(staged),
            "total_size": total,
        }
    )


@projects_bp.route("/projects/upload-zip", methods=["POST"])
@login_required
def upload_zip():
    """Upload a ZIP archive; extract into a new project.

    Form fields: name, file (the .zip).
    """
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"error": t("project_name_required")}), 400

    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": t("project_no_files")}), 400

    if not uploaded.filename.lower().endswith(".zip"):
        return jsonify({"error": t("project_zip_invalid")}), 400

    # Read the zip into memory (capped by MAX_CONTENT_LENGTH at the WSGI layer).
    zip_bytes = uploaded.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return jsonify({"error": t("project_zip_invalid")}), 400

    # Stage entries with zip-slip + whitelist + limits validation.
    staged = []  # list of (rel_path, content_bytes)
    total = 0
    seen_paths = set()
    for info in zf.infolist():
        if info.is_dir():
            continue  # directory entries are recreated on demand
        # Reject Zip Slip: absolute paths or '..' segments.
        norm = _normalize_rel_path(info.filename)
        if norm is None:
            zf.close()
            return jsonify({"error": t("project_invalid_path"), "path": info.filename}), 400
        if not _allowed_extension(norm):
            zf.close()
            return jsonify({"error": t("project_invalid_extension", path=norm)}), 400
        size = info.file_size
        if size > MAX_FILE_SIZE:
            zf.close()
            return jsonify(
                {"error": t("project_upload_too_large", limit=MAX_FILE_SIZE // (1024 * 1024))}
            ), 400
        if len(staged) + 1 > MAX_PROJECT_FILES:
            zf.close()
            return jsonify({"error": t("project_upload_too_many", limit=MAX_PROJECT_FILES)}), 400
        new_total = total + size
        if new_total > MAX_PROJECT_SIZE:
            zf.close()
            return jsonify(
                {"error": t("project_upload_too_large", limit=MAX_PROJECT_SIZE // (1024 * 1024))}
            ), 400
        total = new_total
        if norm in seen_paths:
            zf.close()
            return jsonify({"error": t("project_duplicate_path", path=norm)}), 400
        seen_paths.add(norm)
        staged.append((norm, zf.read(info)))
    zf.close()

    if not staged:
        return jsonify({"error": t("project_no_files")}), 400

    project = _persist_project(current_user.id, name, staged)
    return jsonify(
        {
            "id": project.id,
            "name": project.name,
            "file_count": len(staged),
            "total_size": total,
        }
    )


# ---------------------------------------------------------------------------
# Management routes
# ---------------------------------------------------------------------------


def _project_or_404(project_id):
    """Return the current user's project or 404."""
    return (
        Project.query.filter_by(id=project_id, user_id=current_user.id)
        .first_or_404()
    )


def _project_share_or_404(share_id):
    """Return a ProjectShareLink owned by the current user or 404."""
    return (
        ProjectShareLink.query.join(Project)
        .filter(
            ProjectShareLink.id == share_id,
            Project.user_id == current_user.id,
        )
        .first_or_404()
    )


def _project_disk_dir(user_id, project_id):
    """Absolute path to a project's storage directory."""
    return os.path.join(
        current_app.config["UPLOAD_FOLDER"], str(user_id), "projects", str(project_id)
    )


@projects_bp.route("/projects")
@login_required
def index():
    """List the current user's projects and their share links."""
    projects = (
        Project.query.filter_by(user_id=current_user.id)
        .order_by(Project.created_at.desc())
        .all()
    )
    share_links = (
        ProjectShareLink.query.join(Project)
        .filter(Project.user_id == current_user.id)
        .order_by(ProjectShareLink.created_at.desc())
        .all()
    )
    return render_template(
        "projects/index.html",
        projects=projects,
        share_links=share_links,
    )


@projects_bp.route("/projects/<int:project_id>/delete", methods=["POST"])
@login_required
def delete_project(project_id):
    project = _project_or_404(project_id)

    # Remove disk files first: if the commit then fails, the project row
    # remains and the user can retry; the alternative (commit-then-rmtree)
    # leaks an orphaned directory with no DB record pointing at it.
    disk_dir = _project_disk_dir(current_user.id, project.id)
    if os.path.isdir(disk_dir):
        shutil.rmtree(disk_dir, ignore_errors=True)

    # Cascade removes ProjectFile + ProjectShareLink rows.
    db.session.delete(project)
    db.session.commit()

    return redirect(url_for("projects.index"))


@projects_bp.route("/projects/<int:project_id>/share", methods=["POST"])
@login_required
def create_share(project_id):
    """Create a ProjectShareLink. Returns JSON {url, token, id}."""
    project = _project_or_404(project_id)

    token = secrets.token_urlsafe(32)
    expires_at = request.form.get("expires_at")
    expires_at = _parse_expiry(expires_at)

    link = ProjectShareLink(
        project_id=project.id,
        token=token,
        expires_at=expires_at,
    )
    db.session.add(link)
    db.session.commit()

    share_url = url_for("projects.view", token=token, _external=True)
    return jsonify({"url": share_url, "token": token, "id": link.id})


@projects_bp.route("/projects/shares/<int:share_id>", methods=["POST"])
@login_required
def update_share(share_id):
    link = _project_share_or_404(share_id)

    action = request.form.get("action")
    if action == "toggle":
        link.is_active = not link.is_active
    elif action == "delete":
        db.session.delete(link)
    elif action == "update_expiry":
        link.expires_at = _parse_expiry(request.form.get("expires_at"))

    db.session.commit()
    return redirect(request.referrer or url_for("projects.index"))


def _parse_expiry(raw):
    """Parse an ISO-8601 expiry string into a UTC-aware datetime (or None).

    Coerced to UTC so is_expired() (which compares against
    datetime.now(timezone.utc)) never raises TypeError on a naive value.
    """
    if not raw:
        return None
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Public project viewer
# ---------------------------------------------------------------------------


def _resolve_share(token):
    """Return the active, non-expired ProjectShareLink for token, or None."""
    link = ProjectShareLink.query.filter_by(token=token).first()
    if not link or not link.is_active or link.is_expired():
        return None
    return link


def _find_index_file(project):
    """Pick the entry HTML file for a project.

    Preference order:
      1. root-level index.html / index.htm
      2. any other root-level .html/.htm (alphabetical)
      3. shallowest .html/.htm anywhere, preferring index-named files
    Returns a ProjectFile or None.
    """
    files = sorted(project.files, key=lambda f: f.path)
    html_exts = (".html", ".htm")

    root_html = [
        f for f in files if "/" not in f.path and f.path.lower().endswith(html_exts)
    ]
    for f in root_html:
        if f.path.lower() in ("index.html", "index.htm"):
            return f
    if root_html:
        return root_html[0]

    any_html = [f for f in files if f.path.lower().endswith(html_exts)]
    if any_html:
        any_html.sort(
            key=lambda f: (
                f.path.count("/"),
                0 if os.path.basename(f.path).lower().split(".")[0] == "index" else 1,
                f.path,
            )
        )
        return any_html[0]
    return None


@projects_bp.route("/p/<token>")
def view(token):
    link = _resolve_share(token)
    if not link:
        return render_template("share/not_found.html"), 404

    project = link.project
    index_file = _find_index_file(project)
    if index_file is None:
        return render_template("share/not_found.html", reason="file_moved"), 404

    # iframe loads the real file URL so relative links resolve correctly.
    raw_url = url_for("projects.raw_file", token=token, rel_path=index_file.path)

    uploader = project.user
    uploaded_by = (
        t("share_uploaded_by", name=uploader.display_name) if uploader else ""
    )
    return render_template(
        "projects/view.html",
        link=link,
        project=project,
        raw_url=raw_url,
        uploaded_by=uploaded_by,
    )


@projects_bp.route("/p/<token>/raw/<path:rel_path>")
def raw_file(token, rel_path):
    """Serve a single project file by its project-relative path.

    Looked up by (project_id, path) in the DB — never built from the URL —
    so path traversal is impossible. Token must be active + unexpired.
    """
    link = _resolve_share(token)
    if not link:
        abort(404)

    # Normalize the same way uploads are stored (posix, no '..').
    norm = _normalize_rel_path(rel_path)
    if norm is None:
        abort(404)

    project_file = (
        ProjectFile.query.filter_by(project_id=link.project_id, path=norm).first()
    )
    if project_file is None:
        abort(404)

    full_path = os.path.join(
        current_app.config["UPLOAD_FOLDER"], project_file.storage_path
    )
    if not os.path.exists(full_path):
        abort(404)

    response = send_file(full_path)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response
