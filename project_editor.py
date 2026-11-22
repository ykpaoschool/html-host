"""In-browser editor for one text file inside a project (stage C1).

The project-side counterpart of ``editor.py``: it rewrites one ``ProjectFile``
blob in place, behind the same optimistic lock, through the same
``editor_core.apply_content`` pipeline. The ``Project`` row, its storage
directory and every ``ProjectShareLink`` are untouched, so ``/p/<token>`` keeps
working after a save - that is the whole point of editing in place rather than
deleting and re-uploading the file.

Two things here are easy to get wrong:

* The file is addressed by ``(project_id, path)`` in the database, never by
  joining the URL onto the filesystem (``projects._project_file_or_404``), so a
  path such as ``../../etc/passwd`` is a 404 and not a read.
* Preview must go through ``projects.project_preview``, the signed-token route,
  not ``projects.project_raw``. The preview iframe is sandboxed, so its document
  has an opaque origin and Chrome treats its subresource requests as
  cross-site: the ``SameSite=Lax`` session cookie is not sent, and a
  session-only raw URL would redirect CSS/JS/images to ``/login``. The token
  lives in the path so relative URLs inherit it. Do not "simplify" this back to
  the owner raw route.

No version history yet (C2): ``apply_content`` is called with
``revision_model=None``, which skips the revision blob and the retention trim.
"""

import os
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify, render_template, request, url_for
from flask_login import login_required

from editor_core import (
    MAX_EDITABLE_FILE_SIZE,
    READONLY_NOT_EDITABLE,
    READONLY_NOT_UTF8,
    READONLY_TOO_LARGE,
    apply_content,
    detect_line_separator,
    is_editable_path,
    iso_utc,
    language_for,
)
from models import content_revision, db
from projects import (
    _mint_preview_token,
    _project_file_or_404,
    _project_or_404,
)

project_editor_bp = Blueprint("project_editor", __name__)


def _full_path(project_file):
    return os.path.join(
        current_app.config["UPLOAD_FOLDER"], project_file.storage_path
    )


def _group_files(files):
    """Split a sorted file list into ``[(directory, [ProjectFile, ...])]``.

    The tree is rendered server-side and a project holds at most
    MAX_PROJECT_FILES (100) entries, so there is no paging. Root files come
    first because "" sorts before any directory name.
    """
    groups = {}
    for project_file in files:
        directory = (
            project_file.path.rsplit("/", 1)[0] if "/" in project_file.path else ""
        )
        groups.setdefault(directory, []).append(project_file)
    return sorted(groups.items())


@project_editor_bp.route("/projects/<int:project_id>/edit/<path:rel_path>")
@login_required
def edit(project_id, rel_path):
    """Render the editor shell for one project file."""
    project = _project_or_404(project_id)
    project_file = _project_file_or_404(project, rel_path)
    files = sorted(project.files, key=lambda pf: pf.path)

    # The preview token is minted per page render and carried in the path, so
    # the sandboxed iframe's css/js/image requests inherit it (see module
    # docstring).
    preview_url = url_for(
        "projects.project_preview",
        project_id=project.id,
        token=_mint_preview_token(project),
        rel_path=project_file.path,
    )
    return render_template(
        "editor/edit.html",
        scope="project",
        mode="edit",
        file=None,
        # Create mode is a single-file feature; these keep the shared template
        # inert rather than half-initialised.
        folder_id=None,
        base_name="",
        current_path=project_file.path,
        project=project,
        project_files=files,
        file_groups=_group_files(files),
        editable_paths={
            pf.path for pf in files if is_editable_path(pf.path)
        },
        back_url=url_for("projects.detail", project_id=project.id),
        urls={
            "content": url_for(
                "project_editor.get_content",
                project_id=project.id,
                rel_path=project_file.path,
            ),
            # C2 fills these in; None hides the history button.
            "history": None,
            "restore": None,
            "download": url_for(
                "projects.project_file_download",
                project_id=project.id,
                rel_path=project_file.path,
            ),
            "editBase": url_for(
                "project_editor.edit",
                project_id=project.id,
                rel_path=project_file.path,
            ),
            "preview": preview_url,
            "create": None,
        },
    )


@project_editor_bp.route("/projects/<int:project_id>/files/<path:rel_path>/content")
@login_required
def get_content(project_id, rel_path):
    """The file's current bytes, plus the revision a save must be based on.

    A file that cannot be edited is still a 200 with ``content: null`` and a
    ``readonly_reason``: the page needs the path and the download link to
    explain itself.
    """
    project = _project_or_404(project_id)
    project_file = _project_file_or_404(project, rel_path)

    full_path = _full_path(project_file)
    if not os.path.exists(full_path):
        # Same posture as editor.get_content: a row whose bytes are gone is a
        # 404, not an empty editor that the next save would overwrite disk with.
        return jsonify({"error": "file_moved"}), 404

    with open(full_path, "rb") as handle:
        data = handle.read()

    payload = {
        "id": project_file.id,
        "project_id": project.id,
        "path": project_file.path,
        "name": project_file.path.rsplit("/", 1)[-1],
        "size": len(data),
        # Rows that predate the column have no updated_at.
        "updated_at": iso_utc(project_file.updated_at or project_file.uploaded_at),
        "editable": True,
        "readonly_reason": None,
        "content": None,
        "line_separator": None,
        "revision": None,
        "language": language_for(project_file.path),
        "download_url": url_for(
            "projects.project_file_download",
            project_id=project.id,
            rel_path=project_file.path,
        ),
    }

    if not is_editable_path(project_file.path):
        # Images, fonts and anything else outside the text whitelist: listed
        # and downloadable, never round-tripped through a CodeMirror buffer.
        payload["editable"] = False
        payload["readonly_reason"] = READONLY_NOT_EDITABLE
    elif len(data) > MAX_EDITABLE_FILE_SIZE:
        # No hash: a file this size can never be saved from here, so computing
        # the revision would be wasted work.
        payload["editable"] = False
        payload["readonly_reason"] = READONLY_TOO_LARGE
    else:
        try:
            # utf-8-sig so an uploaded BOM is not shown as a stray character.
            # Never errors="replace": the replacement characters would be
            # written back verbatim on the next save and destroy the file.
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            payload["editable"] = False
            payload["readonly_reason"] = READONLY_NOT_UTF8
        else:
            payload["content"] = text
            payload["line_separator"] = detect_line_separator(text)
            payload["revision"] = content_revision(data)

    response = jsonify(payload)
    # prevent_caching in app.py only covers text/html, so JSON is cached by the
    # browser and by any proxy between it and the app. Without this header a
    # save followed by a reload can show the stale body.
    response.headers["Cache-Control"] = "no-store"
    return response


@project_editor_bp.route(
    "/projects/<int:project_id>/files/<path:rel_path>/content",
    methods=["POST"],
)
@login_required
def save_content(project_id, rel_path):
    """Overwrite a project file in place. Body: {content, base_revision}.

    The ProjectFile row, its storage_path, the Project row and every share
    link are left alone; only the bytes and the size/updated_at change, so
    /p/<token> keeps working. Conflict and failure handling are
    editor_core.apply_content's, identical to the single-file editor.
    """
    project = _project_or_404(project_id)
    project_file = _project_file_or_404(project, rel_path)

    if not is_editable_path(project_file.path):
        return jsonify({"error": "not_editable"}), 400

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid_request"}), 400
    content = payload.get("content")
    base_revision = payload.get("base_revision")
    if not isinstance(content, str) or not isinstance(base_revision, str):
        return (
            jsonify(
                {
                    "error": "invalid_request",
                    "message": "'content' and 'base_revision' are required.",
                }
            ),
            400,
        )

    # No newline normalization, ever: the client restored the file's own
    # separator with sliceDoc(), and normalizing here would rewrite every line
    # of a Windows file silently.
    body, status = apply_content(
        project_file,
        content.encode("utf-8"),
        base_revision,
        revision_model=None,
        fk_field="project_file_id",
        history_dir=None,
        max_size=MAX_EDITABLE_FILE_SIZE,
        retention=None,
    )

    if status == 200:
        # Touching a child row does not bump the parent's onupdate timestamp,
        # and the project list shows updated_at. Best effort: the save itself
        # has already been committed, so a failure here must not report the
        # save as failed.
        project.updated_at = datetime.now(timezone.utc)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            current_app.logger.exception(
                "project_editor: could not bump updated_at of project %s",
                project.id,
            )

    return jsonify(body), status
