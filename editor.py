"""In-browser editor for a single uploaded HTML file.

This is phase 2 of the editor work (see ``.claude/plans/editor-implementation.md``
§3 and §5): open an existing file and write it back in place, so the share links
already handed out keep working. Creating files (§3.5), version history (§3.6),
preview and the API read endpoint (§3.8) belong to later phases and deliberately
have no route here yet.

Two properties are load-bearing and easy to break:

* The bytes are round-tripped untouched. The editor restores the file's own line
  separator on the client (``view.state.sliceDoc()`` plus the ``lineSeparator``
  facet, plan §1.6), so nothing here may normalize newlines — a stray
  ``.replace("\\r\\n", "\\n")`` would rewrite every line of a Windows file
  without raising anything.
* Nothing is cached in process memory. Content hashes are recomputed from disk
  on every read, so the two gunicorn workers have no state to disagree about.
"""

import hashlib
import os
import tempfile
from datetime import datetime, timezone

from flask import (
    Blueprint,
    current_app,
    jsonify,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user, login_required

from dashboard import _ensure_upload_dir
from models import File, Folder, db

editor_bp = Blueprint("editor", __name__)

# The single "one file" limit for online editing, defined once so the read-only
# check and the save check cannot drift apart. It constrains the editor only:
# uploads keep their own limits (50 MB global / 10 MB per file inside projects),
# so a larger file still uploads and still gets share links - it just cannot be
# edited in the browser.
MAX_EDITABLE_FILE_SIZE = 2 * 1024 * 1024

READONLY_TOO_LARGE = "too_large"
READONLY_NOT_UTF8 = "not_utf8"


def _file_or_404(file_id):
    """The caller's own file, or 404.

    Every route filters by user_id, like the dashboard routes: the editor must
    never reach someone else's file, and admins do not edit on a user's behalf.
    """
    return File.query.filter_by(id=file_id, user_id=current_user.id).first_or_404()


def _full_path(file):
    return os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)


def _revision(data):
    """Content hash used as the optimistic-lock token.

    A hash rather than ``updated_at``: SQLite returns naive datetimes (so every
    comparison needs timezone normalization) and a file edited on disk behind
    the app's back never updates the column at all.
    """
    return hashlib.sha256(data).hexdigest()


def _iso_utc(value):
    """Serialize a datetime as UTC-aware.

    SQLite hands back naive datetimes; a naive value means UTC, the same
    convention ``models._is_expired`` documents.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _detect_line_separator(text):
    return "crlf" if "\r\n" in text else "lf"


def _folder_url(folder_id):
    if folder_id:
        return url_for("dashboard.view_folder", folder_id=folder_id)
    return url_for("dashboard.index")


def _write_atomically(full_path, data):
    """Replace a file's bytes in one step.

    Writes a temp file next to the target and renames it over the original:
    same directory means the same filesystem, so ``os.replace`` is atomic and a
    crash or a full disk leaves the previous bytes intact instead of a
    truncated file.
    """
    fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(full_path), prefix=".edit-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, full_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


@editor_bp.route("/files/new")
@login_required
def new_file():
    """Render a blank editor.

    Only the page: creating the file is ``POST /files/new``, which arrives with
    phase 3 along with the dashboard's "new file" entry point, so for now this
    page is reachable by URL only.
    """
    folder_id = request.args.get("folder_id", type=int)
    # A folder_id that is not the caller's is treated as "root" rather than an
    # error, matching how create_file() treats it on the upload path.
    folder = None
    if folder_id:
        folder = Folder.query.filter_by(id=folder_id, user_id=current_user.id).first()
    folder_id = folder.id if folder else None

    return render_template(
        "editor/edit.html",
        mode="create",
        file=None,
        folder_id=folder_id,
        back_url=_folder_url(folder_id),
    )


@editor_bp.route("/files/<int:file_id>/edit")
@login_required
def edit_file(file_id):
    """Render the editor shell.

    The contents arrive afterwards from the JSON endpoint below, so a large
    file does not hold up the first paint.
    """
    file = _file_or_404(file_id)
    return render_template(
        "editor/edit.html",
        mode="edit",
        file=file,
        folder_id=file.folder_id,
        back_url=_folder_url(file.folder_id),
    )


@editor_bp.route("/files/<int:file_id>/content")
@login_required
def get_content(file_id):
    """The file's current bytes, plus the revision a save must be based on.

    A file that cannot be edited is still a 200 with ``content: null`` and a
    ``readonly_reason``: the page needs the name and the download link to
    explain itself.
    """
    file = _file_or_404(file_id)
    full_path = _full_path(file)
    if not os.path.exists(full_path):
        # Same posture as share.view: a row whose bytes are gone is a 404, not
        # an empty editor that the next save would overwrite the disk with.
        return jsonify({"error": "file_moved"}), 404

    with open(full_path, "rb") as handle:
        data = handle.read()

    payload = {
        "id": file.id,
        "name": file.name,
        "folder_id": file.folder_id,
        "size": len(data),
        "updated_at": _iso_utc(file.updated_at),
        "editable": True,
        "readonly_reason": None,
        "content": None,
        "line_separator": None,
        "revision": None,
        "download_url": url_for("editor.download", file_id=file.id),
    }

    if len(data) > MAX_EDITABLE_FILE_SIZE:
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
            payload["line_separator"] = _detect_line_separator(text)
            payload["revision"] = _revision(data)

    response = jsonify(payload)
    # prevent_caching in app.py only covers text/html, so JSON is cached by the
    # browser and by any proxy between it and the app. Without this header a
    # save followed by a reload can show the stale body.
    response.headers["Cache-Control"] = "no-store"
    return response


@editor_bp.route("/files/<int:file_id>/content", methods=["POST"])
@login_required
def save_content(file_id):
    """Overwrite the file in place. Body: {content, base_revision}.

    The File row and its storage_path are untouched, so every share link
    already minted for this file keeps working - which is the whole point.
    """
    file = _file_or_404(file_id)

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

    # No newline normalization, ever: the client already restored the file's
    # own separator with sliceDoc() (plan §1.6), and normalizing here would
    # rewrite every line of a Windows file silently.
    data = content.encode("utf-8")
    if len(data) > MAX_EDITABLE_FILE_SIZE:
        return jsonify({"error": "too_large", "limit": MAX_EDITABLE_FILE_SIZE}), 413

    full_path = _full_path(file)
    if not os.path.exists(full_path):
        return jsonify({"error": "file_moved"}), 404
    with open(full_path, "rb") as handle:
        previous = handle.read()

    current_revision = _revision(previous)
    if current_revision != base_revision:
        # Someone (or something) changed the file since the editor loaded it.
        # Hand back the revision that is actually on disk so the client can
        # offer "overwrite" without a second round trip.
        return jsonify({"error": "conflict", "revision": current_revision}), 409

    # _ensure_upload_dir recreates the parent directory if the folder holding
    # this file was moved on disk since the upload.
    full_path = _ensure_upload_dir(file.storage_path)
    try:
        _write_atomically(full_path, data)
    except OSError:
        current_app.logger.exception("editor: could not write %s", full_path)
        return jsonify({"error": "save_failed"}), 500

    file.size = len(data)
    # Assigned explicitly: the column's onupdate only fires when a value really
    # changes, so rewriting content at the same byte count would leave
    # updated_at stale even though the bytes did change (same note as api.py).
    now = datetime.now(timezone.utc)
    file.updated_at = now
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        # The new bytes are already on disk, so put the old ones back: a save
        # that reports failure must not leave the file changed, and a row
        # claiming a size the disk does not have is the other half of that bug.
        try:
            _write_atomically(full_path, previous)
        except OSError:
            current_app.logger.exception(
                "editor: rollback of %s failed, on-disk content is newer "
                "than the database says",
                full_path,
            )
        return jsonify({"error": "save_failed"}), 500

    return jsonify(
        {
            "revision": _revision(data),
            "size": len(data),
            "updated_at": now.isoformat(),
        }
    )


@editor_bp.route("/files/<int:file_id>/download")
@login_required
def download(file_id):
    """Download one of the caller's own files.

    The read-only notice tells the user to download the file and edit it
    locally, and until now the only download route in the app hung off a share
    token - a file without a share link could not be retrieved at all.
    """
    file = _file_or_404(file_id)
    full_path = _full_path(file)
    if not os.path.exists(full_path):
        return render_template("share/not_found.html", reason="file_moved"), 404

    # as_attachment makes the browser save rather than render the file, so none
    # of the framing/sandbox headers share.raw_file needs apply here.
    return send_file(full_path, as_attachment=True, download_name=file.name)
