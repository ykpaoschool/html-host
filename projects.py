import io
import os
import shutil
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.exc import IntegrityError

from editor_core import is_editable_path
from i18n import t
from models import Project, ProjectFile, ProjectShareLink, db, mint_share_token

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

    now = datetime.now(timezone.utc)
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
                    uploaded_at=now,
                    updated_at=now,
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


def _stage_uploaded_files(uploaded, paths=None):
    """Validate uploaded files into ``(staged, total, error)``.

    ``staged`` is a list of ``(rel_path, content_bytes)``. ``paths`` optionally
    supplies a project-relative path per uploaded entry (positional); a blank
    entry falls back to the upload's own filename. ``error`` is ``None`` or a
    ``(message, path_or_None)`` tuple already passed through ``t()``.

    Nothing is written to disk or the DB here, so a validation failure leaves
    the project untouched.
    """
    entries = list(uploaded or [])
    if not any(f and f.filename for f in entries):
        return [], 0, (t("project_no_files"), None)
    if paths is not None and len(paths) != len(entries):
        return [], 0, (t("project_update_paths_mismatch"), None)

    staged = []
    total = 0
    seen_paths = set()
    for index, f in enumerate(entries):
        if not f or not f.filename:
            continue
        rel_path = ""
        if paths is not None:
            rel_path = (paths[index] or "").strip()
        rel_path = rel_path or f.filename
        content = f.read()
        err, total = _validate_file_entry(rel_path, len(content), total, len(staged))
        if err:
            return [], 0, (err, rel_path)
        norm = _normalize_rel_path(rel_path)
        if norm in seen_paths:
            return [], 0, (t("project_duplicate_path", path=norm), None)
        seen_paths.add(norm)
        staged.append((norm, content))
    return staged, total, None


def _stage_zip(uploaded):
    """Validate a ZIP upload into ``(staged, total, error)``, like
    ``_stage_uploaded_files``.

    Entries go through the same zip-slip, whitelist and limit checks as a new
    project upload; nothing touches disk until the caller applies it.
    """
    if not uploaded or not uploaded.filename:
        return [], 0, (t("project_no_files"), None)
    if not uploaded.filename.lower().endswith(".zip"):
        return [], 0, (t("project_zip_invalid"), None)

    # Read the zip into memory (capped by MAX_CONTENT_LENGTH at the WSGI layer).
    zip_bytes = uploaded.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        return [], 0, (t("project_zip_invalid"), None)

    staged = []
    total = 0
    seen_paths = set()
    try:
        for info in zf.infolist():
            if info.is_dir():
                continue  # directory entries are recreated on demand
            # Reject Zip Slip: absolute paths or '..' segments.
            norm = _normalize_rel_path(info.filename)
            if norm is None:
                return [], 0, (t("project_invalid_path"), info.filename)
            if not _allowed_extension(norm):
                return [], 0, (t("project_invalid_extension", path=norm), None)
            size = info.file_size
            if size > MAX_FILE_SIZE:
                return [], 0, (
                    t("project_upload_too_large", limit=MAX_FILE_SIZE // (1024 * 1024)),
                    None,
                )
            if len(staged) + 1 > MAX_PROJECT_FILES:
                return [], 0, (
                    t("project_upload_too_many", limit=MAX_PROJECT_FILES),
                    None,
                )
            new_total = total + size
            if new_total > MAX_PROJECT_SIZE:
                return [], 0, (
                    t("project_upload_too_large", limit=MAX_PROJECT_SIZE // (1024 * 1024)),
                    None,
                )
            total = new_total
            if norm in seen_paths:
                return [], 0, (t("project_duplicate_path", path=norm), None)
            seen_paths.add(norm)
            staged.append((norm, zf.read(info)))
    finally:
        zf.close()

    if not staged:
        return [], 0, (t("project_no_files"), None)
    return staged, total, None


def _project_upload_error(error):
    """Turn a ``(message, path)`` stemming error into a JSON 400 response."""
    message, path = error
    body = {"error": message}
    if path:
        body["path"] = path
    return jsonify(body), 400


@projects_bp.route("/projects/upload", methods=["POST"])
@login_required
def upload_files():
    """Batch multi-file upload. Form fields: name, files[].

    Returns JSON {id, name, file_count, total_size} on success.
    """
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"error": t("project_name_required")}), 400

    staged, total, error = _stage_uploaded_files(request.files.getlist("files"))
    if error:
        return _project_upload_error(error)

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

    staged, total, error = _stage_zip(request.files.get("file"))
    if error:
        return _project_upload_error(error)

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
# In-place project updates.
#
# These routes mutate the existing Project row and its storage directory;
# they never create a second Project and never touch ProjectShareLink, so
# /p/<token> keeps resolving to the same (now updated) project.
# ---------------------------------------------------------------------------

_UPDATE_MODES = ("merge", "replace")


def _parse_update_mode():
    """Return the requested update mode, defaulting to 'merge'."""
    mode = (request.form.get("mode") or "merge").strip().lower()
    return mode if mode in _UPDATE_MODES else None


def _stage_project_update(project, staged, mode):
    """Check an update against the file set it would leave behind.

    Returns ``(staged, to_delete, error)``. ``to_delete`` holds the
    ProjectFile rows that ``mode == "replace"`` removes; ``error`` is an
    already-translated message or None. Limits are applied to the *final*
    state, not just to the bytes in this request.
    """
    staged_map = dict(staged)
    existing = {pf.path: pf for pf in project.files}

    if mode == "replace":
        to_delete = [pf for path, pf in existing.items() if path not in staged_map]
    else:
        to_delete = []

    # Sizes of the files the update leaves in place, plus the new bytes.
    # In replace mode nothing that is absent from the upload survives.
    if mode == "replace":
        final_sizes = {}
    else:
        final_sizes = {
            path: (pf.size or 0)
            for path, pf in existing.items()
            if path not in staged_map
        }
    final_sizes.update({path: len(data) for path, data in staged})

    if len(final_sizes) > MAX_PROJECT_FILES:
        return (
            None,
            None,
            t(
                "project_update_too_many",
                count=len(final_sizes),
                limit=MAX_PROJECT_FILES,
            ),
        )
    total = sum(final_sizes.values())
    if total > MAX_PROJECT_SIZE:
        return (
            None,
            None,
            t(
                "project_update_too_large",
                size=round(total / (1024 * 1024), 1),
                limit=MAX_PROJECT_SIZE // (1024 * 1024),
            ),
        )
    if not any(path.lower().endswith((".html", ".htm")) for path in final_sizes):
        # Without an HTML file the share viewer would 404 even though the
        # update itself succeeded, so refuse it now and keep the old version.
        return None, None, t("project_no_index")
    return staged, to_delete, None


def _apply_project_update(project, staged, mode, name=None):
    """Apply a validated update to the project's directory and DB rows.

    The new file set is built in a sibling staging directory, then swapped in
    with two renames on the same filesystem; the old directory is only deleted
    after the DB commit succeeds, and is restored if it does not.
    """
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    projects_root = os.path.join(upload_folder, str(project.user_id), "projects")
    live_dir = os.path.join(projects_root, str(project.id))
    token = uuid.uuid4().hex
    staging_dir = os.path.join(projects_root, f".staging-{project.id}-{token}")
    trash_dir = os.path.join(projects_root, f".trash-{project.id}-{token}")

    existing = {pf.path: pf for pf in project.files}
    staged_map = dict(staged)
    if mode == "replace":
        final_paths = list(staged_map)
    else:
        final_paths = list(existing)
        final_paths.extend(p for p in staged_map if p not in existing)
    final_paths.sort()

    # 1. Build the complete final tree in staging (new bytes + copies of the
    #    files being kept). Any failure here leaves the live directory alone.
    os.makedirs(staging_dir)
    try:
        for rel_path in final_paths:
            dest = os.path.join(staging_dir, rel_path.replace("/", os.sep))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if rel_path in staged_map:
                with open(dest, "wb") as out:
                    out.write(staged_map[rel_path])
            else:
                src = os.path.join(upload_folder, existing[rel_path].storage_path)
                if not os.path.exists(src):
                    raise FileNotFoundError(src)
                shutil.copy2(src, dest)
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    # 2. Swap the directories. The window between the two renames is a pair
    #    of syscalls, not a copy, so a concurrent reader sees either the old
    #    tree or the new one.
    swapped = False
    try:
        if os.path.isdir(live_dir):
            os.rename(live_dir, trash_dir)
        os.rename(staging_dir, live_dir)
        swapped = True

        # 3. Commit the row changes. storage_path is derived from
        #    (user_id, project_id, path), so it never needs rewriting.
        now = datetime.now(timezone.utc)
        removed_ids = []
        for rel_path in final_paths:
            pf = existing.get(rel_path)
            data = staged_map.get(rel_path)
            if pf is None:
                db.session.add(
                    ProjectFile(
                        project_id=project.id,
                        path=rel_path,
                        storage_path=_get_project_storage_path(
                            project.user_id, project.id, rel_path
                        ),
                        size=len(data),
                        uploaded_at=now,
                        updated_at=now,
                    )
                )
            elif data is not None:
                pf.size = len(data)
                pf.uploaded_at = now
                pf.updated_at = now
        for pf in existing.values():
            if mode == "replace" and pf.path not in staged_map:
                # Remember the id: after the commit the row is gone and its
                # history directory has nothing left pointing at it.
                removed_ids.append(pf.id)
                db.session.delete(pf)
        if name:
            project.name = name
        # Touching child rows does not bump the parent's onupdate timestamp.
        project.updated_at = now
        db.session.commit()
    except Exception:
        db.session.rollback()
        # 4a. Put the old directory back and drop the half-applied one.
        if swapped:
            shutil.rmtree(live_dir, ignore_errors=True)
        else:
            shutil.rmtree(staging_dir, ignore_errors=True)
        if os.path.isdir(trash_dir) and not os.path.isdir(live_dir):
            os.rename(trash_dir, live_dir)
        raise

    # 4b. Commit succeeded: the old tree is now unreachable.
    shutil.rmtree(trash_dir, ignore_errors=True)

    # 5. The files replace mode removed have no rows now, so their revision
    #    blobs are unreachable too. Only here, after the commit: a failed
    #    commit above restores the old directory and leaves those rows in
    #    place, and their history has to survive with them.
    for project_file_id in removed_ids:
        delete_project_file_history(project.user_id, project.id, project_file_id)


def _project_update_response(project, to_delete, mode):
    return jsonify(
        {
            "id": project.id,
            "name": project.name,
            "file_count": len(project.files),
            "total_size": sum(pf.size or 0 for pf in project.files),
            "mode": mode,
            "removed_count": len(to_delete),
        }
    )


@projects_bp.route("/projects/<int:project_id>/upload", methods=["POST"])
@login_required
def update_project_files(project_id):
    """Update an existing project from a batch of files, in place.

    Form fields: files[], mode (merge|replace), optional paths[] (one
    project-relative path per file), optional name.
    """
    project = _project_or_404(project_id)
    mode = _parse_update_mode()
    if mode is None:
        return jsonify({"error": t("project_update_mode_invalid")}), 400

    paths = request.form.getlist("paths") or None
    staged, _, error = _stage_uploaded_files(
        request.files.getlist("files"), paths=paths
    )
    if error:
        return _project_upload_error(error)

    staged, to_delete, error = _stage_project_update(project, staged, mode)
    if error:
        return jsonify({"error": error}), 400

    name = (request.form.get("name") or "").strip() or None
    try:
        _apply_project_update(project, staged, mode, name=name)
    except Exception:
        return jsonify({"error": t("project_update_failed")}), 500

    return _project_update_response(project, to_delete, mode)


@projects_bp.route("/projects/<int:project_id>/upload-zip", methods=["POST"])
@login_required
def update_project_zip(project_id):
    """Update an existing project from a ZIP archive, in place.

    Form fields: file (the .zip), mode (merge|replace), optional name.
    """
    project = _project_or_404(project_id)
    mode = _parse_update_mode()
    if mode is None:
        return jsonify({"error": t("project_update_mode_invalid")}), 400

    staged, _, error = _stage_zip(request.files.get("file"))
    if error:
        return _project_upload_error(error)

    staged, to_delete, error = _stage_project_update(project, staged, mode)
    if error:
        return jsonify({"error": error}), 400

    name = (request.form.get("name") or "").strip() or None
    try:
        _apply_project_update(project, staged, mode, name=name)
    except Exception:
        return jsonify({"error": t("project_update_failed")}), 500

    return _project_update_response(project, to_delete, mode)


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


# Revision blobs live beside the user's own directory, not inside it, for the
# same reason as the single-file history (dashboard.HISTORY_DIRNAME): a user can
# create a folder named ".history", so nesting history under <user_id>/ would
# let the two trees collide. The fixed "projects" segment is safe because the
# single-file layout names its directories after integer file ids.
def project_history_dir(user_id, project_id):
    """Absolute directory holding every revision blob of one project."""
    return os.path.join(
        current_app.config["UPLOAD_FOLDER"],
        ".history",
        str(user_id),
        "projects",
        str(project_id),
    )


def project_file_history_dir(user_id, project_id, project_file_id):
    """Absolute directory holding one ProjectFile's revision blobs.

    The one place this path is assembled: the save pipeline writes here and
    every delete path removes here, so a writer and a cleaner cannot end up
    with two different ideas of where history lives.
    """
    return os.path.join(
        project_history_dir(user_id, project_id), str(project_file_id)
    )


def delete_project_file_history(user_id, project_id, project_file_id):
    """Drop one ProjectFile's revision blobs, rows or no rows.

    ignore_errors: this runs on the delete paths, where a missing directory is
    the ordinary case (a file edited once or never has none) and must not turn
    a delete into a 500.
    """
    shutil.rmtree(
        project_file_history_dir(user_id, project_id, project_file_id),
        ignore_errors=True,
    )


def delete_project_history(user_id, project_id):
    """Drop every revision blob belonging to one project."""
    shutil.rmtree(project_history_dir(user_id, project_id), ignore_errors=True)


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
    # History lives beside the project directory, not inside it, so the rmtree
    # above cannot reach it.
    delete_project_history(current_user.id, project.id)

    # Cascade removes ProjectFile + ProjectShareLink rows.
    db.session.delete(project)
    db.session.commit()

    return redirect(url_for("projects.index"))


# ---------------------------------------------------------------------------
# Project detail + single-file management.
#
# These are session-authenticated Web UI routes (the API plane has its own
# equivalents). They touch individual ProjectFile rows and blobs only: the
# Project row, its storage directory and every ProjectShareLink survive, so
# /p/<token> keeps working across any of them.
# ---------------------------------------------------------------------------


def _set_raw_headers(response, no_store=False):
    """Apply the headers every route that serves user content must carry.

    Single source of truth for the share-token raw route and the owner
    preview route, so the framing/CSP posture cannot drift between them
    (or a future third). Assigns, not setdefault, because the app-wide
    after_request hook has already applied X-Frame-Options: DENY and
    frame-ancestors 'none'.
    """
    # Sibling assets are fetched from the sandboxed frame; ACAO keeps that
    # working, nosniff stops a mislabelled file from being reinterpreted.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Only this app may frame user content.
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # sandbox: the raw URL is reachable on its own, not only inside a viewer.
    # Without it, uploaded HTML would run as a first-party document on this
    # origin with the session cookie within reach. The frame's own sandbox
    # attribute and this directive are intersected, so the allowances here
    # must stay in sync with the templates that frame these URLs.
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self'; sandbox allow-scripts allow-popups"
    )
    if no_store:
        # A replace writes new bytes at the same URL; never let the browser
        # serve the previous version into the preview iframe.
        response.headers["Cache-Control"] = "no-store"
    return response


def _project_file_or_404(project, rel_path):
    """Return a ProjectFile by (project_id, normalized path), or 404.

    The URL path is only a key into the DB — never joined onto the
    filesystem — so path traversal is impossible.
    """
    norm = _normalize_rel_path(rel_path)
    if norm is None:
        abort(404)
    project_file = ProjectFile.query.filter_by(
        project_id=project.id, path=norm
    ).first()
    if project_file is None:
        abort(404)
    return project_file


def _prune_empty_parents(directory, stop_dir):
    """Remove empty directories from ``directory`` up to (excluding) ``stop_dir``.

    Best-effort: stops at the first non-empty (or already gone) directory,
    so deleting ``css/app.css`` drops an emptied ``css/`` but never the
    project root.
    """
    directory = os.path.abspath(directory)
    stop_dir = os.path.abspath(stop_dir)
    while directory != stop_dir and directory.startswith(stop_dir + os.sep):
        try:
            os.rmdir(directory)
        except OSError:
            return
        directory = os.path.dirname(directory)


# The preview iframe is sandboxed, so its document has an opaque origin and
# the browser treats its subresource requests as cross-site: a SameSite=Lax
# session cookie is NOT sent, and sibling assets would redirect to /login
# and fail. A short-lived signed token carried in the path is the credential
# instead - relative URLs in the uploaded HTML inherit the path prefix, so
# every css/js/image request keeps the token. It is read-only and scoped to
# one project, and the owner's session is accepted as a fallback so opening
# the URL directly keeps working after the token expires.
_PREVIEW_TOKEN_SALT = "htmlhost-project-preview"
_PREVIEW_TOKEN_MAX_AGE = 60 * 60  # 1 hour


def _preview_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt=_PREVIEW_TOKEN_SALT)


def _mint_preview_token(project):
    """Signed (project_id, user_id) capability for the preview iframe."""
    return _preview_serializer().dumps({"p": project.id, "u": project.user_id})


def _preview_token_valid(project, token):
    try:
        data = _preview_serializer().loads(token, max_age=_PREVIEW_TOKEN_MAX_AGE)
    except BadSignature:
        return False
    return data.get("p") == project.id and data.get("u") == project.user_id


@projects_bp.route("/projects/<int:project_id>")
@login_required
def detail(project_id):
    """Owner-facing project page: file list, preview, replace, delete, upload."""
    project = _project_or_404(project_id)
    files = sorted(project.files, key=lambda pf: pf.path)
    total_size = sum(pf.size or 0 for pf in files)
    has_html = any(
        pf.path.lower().endswith((".html", ".htm")) for pf in files
    )
    share_links = (
        ProjectShareLink.query.filter_by(project_id=project.id)
        .order_by(ProjectShareLink.created_at.desc())
        .all()
    )
    return render_template(
        "projects/detail.html",
        project=project,
        files=files,
        total_size=total_size,
        has_html=has_html,
        share_links=share_links,
        # Only these get an "edit" link; everything else can be previewed,
        # downloaded, replaced or deleted, but not opened in the text editor.
        editable_paths={pf.path for pf in files if is_editable_path(pf.path)},
        preview_token=_mint_preview_token(project),
    )


def _project_file_response(project_file, *, as_attachment=False):
    """Send a ProjectFile's blob, turning a missing file into a 404.

    The blob can briefly be absent while an in-place update swaps the
    project directory, so a stat-then-send would race; letting send_file
    fail and mapping FileNotFoundError keeps that a 404, never a 500.
    """
    full_path = os.path.join(
        current_app.config["UPLOAD_FOLDER"], project_file.storage_path
    )
    try:
        return send_file(
            full_path,
            as_attachment=as_attachment,
            download_name=(
                os.path.basename(project_file.path) if as_attachment else None
            ),
        )
    except FileNotFoundError:
        abort(404)


@projects_bp.route("/projects/<int:project_id>/raw/<path:rel_path>")
@login_required
def project_raw(project_id, rel_path):
    """Owner preview of one project file, served from a real URL.

    Carries the same headers as the share-token raw route (see
    ``_set_raw_headers``), plus no-store so a replaced file is never shown
    out of the browser cache.
    """
    project = _project_or_404(project_id)
    project_file = _project_file_or_404(project, rel_path)
    return _set_raw_headers(_project_file_response(project_file), no_store=True)


@projects_bp.route("/projects/<int:project_id>/preview/<token>/<path:rel_path>")
def project_preview(project_id, token, rel_path):
    """Serve a project file to the owner's preview iframe.

    Deliberately not ``@login_required``: the sandboxed frame's subresource
    requests carry no session cookie (see the token note above), so the
    signed path token is the credential. Either a valid token or the owner's
    session is accepted; anything else is a 404 so project existence does
    not leak.
    """
    project = db.session.get(Project, project_id)
    if project is None:
        abort(404)

    session_ok = (
        current_user.is_authenticated and current_user.id == project.user_id
    )
    if not (session_ok or _preview_token_valid(project, token)):
        abort(404)

    project_file = _project_file_or_404(project, rel_path)
    return _set_raw_headers(_project_file_response(project_file), no_store=True)


@projects_bp.route("/projects/<int:project_id>/files/<path:rel_path>/download")
@login_required
def project_file_download(project_id, rel_path):
    """Download one project file as an attachment."""
    project = _project_or_404(project_id)
    project_file = _project_file_or_404(project, rel_path)
    return _project_file_response(project_file, as_attachment=True)


@projects_bp.route(
    "/projects/<int:project_id>/files/<path:rel_path>/delete",
    methods=["POST"],
)
@login_required
def project_file_delete(project_id, rel_path):
    """Delete one file from a project, in place.

    Disk blob first, then the DB row (same ordering as deleting a whole
    project), then best-effort empty-directory cleanup. The Project row and
    its share links are never touched.
    """
    project = _project_or_404(project_id)
    project_file = _project_file_or_404(project, rel_path)

    full_path = os.path.join(
        current_app.config["UPLOAD_FOLDER"], project_file.storage_path
    )
    # Missing blob is not an error: deleting is idempotent.
    if os.path.isfile(full_path):
        try:
            os.remove(full_path)
        except OSError:
            # Never leave a row pointing at a file we could not remove.
            flash(t("project_update_failed"), "error")
            return redirect(url_for("projects.detail", project_id=project.id))

    db.session.delete(project_file)
    # The row is going away, so its revision blobs would be unreachable.
    # Disk before the commit, like the blob above.
    delete_project_file_history(
        project.user_id, project.id, project_file.id
    )
    # Touching a child row does not bump the parent's onupdate timestamp.
    project.updated_at = datetime.now(timezone.utc)
    db.session.commit()

    _prune_empty_parents(
        os.path.dirname(full_path),
        _project_disk_dir(project.user_id, project.id),
    )

    flash(t("file_deleted"), "success")
    return redirect(url_for("projects.detail", project_id=project.id))


@projects_bp.route("/projects/<int:project_id>/share", methods=["POST"])
@login_required
def create_share(project_id):
    """Create a ProjectShareLink. Returns JSON {url, token, id}."""
    project = _project_or_404(project_id)

    token, error = mint_share_token(
        ProjectShareLink, request.form.get("custom_token")
    )
    if error:
        key = "share_token_taken" if error == "taken" else "share_token_invalid"
        return jsonify({"error": t(key)}), 409 if error == "taken" else 400

    expires_at = request.form.get("expires_at")
    expires_at = _parse_expiry(expires_at)
    require_login = request.form.get("require_login") == "1"

    link = ProjectShareLink(
        project_id=project.id,
        token=token,
        expires_at=expires_at,
        require_login=require_login,
    )
    db.session.add(link)
    try:
        db.session.commit()
    except IntegrityError:
        # Lost the race between the advisory check and the insert; the token
        # column's UNIQUE constraint is the real arbiter.
        db.session.rollback()
        return jsonify({"error": t("share_token_taken")}), 409

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

    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Compact duration suffixes accepted by _parse_duration, in seconds.
_DURATION_UNITS = {"m": 60, "h": 3600, "d": 86400}


def _parse_duration(raw):
    """Parse a compact duration ('30m' / '24h' / '7d' / 'never') into a timedelta.

    'never' returns None, meaning "no expiry". Raises ValueError on anything
    unrecognised, so callers can turn a typo into a 400 rather than silently
    minting a link that never expires.
    """
    value = str(raw).strip().lower()
    if value in ("never", "none"):
        return None
    unit = value[-1:]
    if unit not in _DURATION_UNITS or not value[:-1].isdigit():
        raise ValueError(f"Unrecognised duration: {raw!r}")
    seconds = int(value[:-1]) * _DURATION_UNITS[unit]
    if seconds <= 0:
        raise ValueError(f"Duration must be positive: {raw!r}")
    return timedelta(seconds=seconds)


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

    if link.require_login and not current_user.is_authenticated:
        # Relative path, not request.url: auth._safe_next_url rejects
        # absolute URLs, which is what request.url would hand it.
        return redirect(url_for("auth.login", next=request.path))

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

    if link.require_login and not current_user.is_authenticated:
        abort(401)

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

    # An in-place update swaps the project directory with two renames, so a
    # path can be momentarily absent while both syscalls run. Retry briefly
    # before reporting the file as gone; a genuinely deleted file still 404s.
    response = None
    for attempt in range(4):
        try:
            response = send_file(full_path)
            break
        except FileNotFoundError:
            if attempt == 3:
                abort(404)
            time.sleep(0.005)
    return _set_raw_headers(response)
