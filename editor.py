"""In-browser editor for a single uploaded HTML file.

Open an existing file and write it back in place, so the share links already
handed out keep working, or start a new one (see
``.claude/plans/editor-implementation.md`` §3 and §5). The API plane's own read
endpoint (§3.8) lives in ``api.py`` instead: it authenticates differently and
has no route here.

Two properties are load-bearing and easy to break:

* The bytes are round-tripped untouched. The editor restores the file's own line
  separator on the client (``view.state.sliceDoc()`` plus the ``lineSeparator``
  facet, plan §1.6), so nothing here may normalize newlines — a stray
  ``.replace("\\r\\n", "\\n")`` would rewrite every line of a Windows file
  without raising anything.
* Nothing is cached in process memory. Content hashes are recomputed from disk
  on every read, so the two gunicorn workers have no state to disagree about.
  The hash itself is ``models.content_revision`` and is deliberately not defined
  here: the API plane returns the same value to its own callers for the same
  file, and one definition is what keeps the two from drifting.

A third applies to saving: the bytes being replaced are kept as a revision
before the new ones land, and a revision's blob is named after its row's
primary key. The write order in ``_apply_content`` is therefore part of the
contract, not a detail to rearrange.
"""

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

from dashboard import (
    _ensure_upload_dir,
    _is_valid_component_name,
    create_file,
    history_dir,
)
from models import File, FileRevision, Folder, content_revision, db

editor_bp = Blueprint("editor", __name__)

# The single "one file" limit for online editing, defined once so the read-only
# check and the save check cannot drift apart. It constrains the editor only:
# uploads keep their own limits (50 MB global / 10 MB per file inside projects),
# so a larger file still uploads and still gets share links - it just cannot be
# edited in the browser.
MAX_EDITABLE_FILE_SIZE = 2 * 1024 * 1024

# How many revisions of one file are kept. Bounded on purpose: at the size the
# editor accepts, the history of one file costs at most this many times
# MAX_EDITABLE_FILE_SIZE on disk. If that threshold is ever raised, this is the
# number that has to be looked at again (requirements F9).
RETENTION = 10

READONLY_TOO_LARGE = "too_large"
READONLY_NOT_UTF8 = "not_utf8"

# The editor's filename field holds the base name; ".html" sits next to it as a
# fixed label that is not part of the value. Both suffixes are stripped off
# whatever the user typed, so carrying the habit over from the upload dialog and
# typing "page.html" does not produce "page.html.html".
HTML_SUFFIXES = (".html", ".htm")
HTML_SUFFIX = ".html"
DEFAULT_BASE_NAME = "untitled"


def _base_name(name):
    """The base name behind what the user typed, without an HTML suffix.

    Purely cosmetic normalization of a value that came from a text box: the
    suffix is stripped off the end and stray surrounding whitespace is dropped.
    Emptiness is the caller's problem - "page.html" and ".html" are different
    mistakes and only one of them leaves a name to work with.
    """
    base = name.strip()
    lowered = base.lower()
    for suffix in HTML_SUFFIXES:
        if lowered.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base.strip()


def _file_or_404(file_id):
    """The caller's own file, or 404.

    Every route filters by user_id, like the dashboard routes: the editor must
    never reach someone else's file, and admins do not edit on a user's behalf.
    """
    return File.query.filter_by(id=file_id, user_id=current_user.id).first_or_404()


def _full_path(file):
    return os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)


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


def _revision_storage_path(user_id, file_id, revision_id):
    """Where one revision's blob lives, relative to UPLOAD_FOLDER.

    Derived from dashboard.history_dir so the writer and the deleters cannot
    end up with two ideas of where history lives; relative, like every other
    storage_path in the app.
    """
    return os.path.relpath(
        os.path.join(history_dir(user_id, file_id), str(revision_id)),
        current_app.config["UPLOAD_FOLDER"],
    )


def _revision_or_404(file, revision_id):
    """One of *this* file's revisions, or 404.

    Filtered by file_id as well as by id: a revision id belonging to another
    file - the caller's own or anyone else's - must not be reachable through
    this file's routes.
    """
    return FileRevision.query.filter_by(id=revision_id, file_id=file.id).first_or_404()


def _revision_blob(revision):
    """A revision's bytes, or None if the row outlived them.

    None rather than an exception so both history routes answer 404 the same
    way get_content does for a File: a row whose bytes are gone is a 404, not
    an empty viewer that looks like an empty document.
    """
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], revision.storage_path)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


def _ordered_revisions(file_id):
    """A file's revisions, newest first.

    The id breaks ties: created_at only resolves to the microsecond, and two
    revisions written inside the same one would otherwise come back in an
    arbitrary order, letting the panel and the retention pruner disagree about
    which is the oldest.
    """
    return (
        FileRevision.query.filter_by(file_id=file_id)
        .order_by(FileRevision.created_at.desc(), FileRevision.id.desc())
        .all()
    )


def _remove_revision_blob(storage_path):
    """Undo a revision whose row never reached a commit.

    Best effort by design: this runs on error paths that are already reporting
    a failure, and the directory it leaves behind (when something else put a
    revision in it in the meantime) is a legitimate directory, not damage.
    """
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], storage_path)
    try:
        os.remove(path)
    except OSError:
        pass
    try:
        os.rmdir(os.path.dirname(path))
    except OSError:
        pass


def _prune_revisions(file):
    """Trim a file's history down to the newest RETENTION revisions.

    Called only once the save has been committed (plan §3.4 step 7): pruning
    first would let a failed commit leave the panel offering a revision whose
    blob had already been deleted. The blob goes before the row, which is the
    order the rest of the app deletes in - a row left pointing at nothing is
    the failure this prefers to bytes nothing points at.

    Housekeeping, and treated as such: a save that has already been committed
    must not be reported to the user as failed because the trim went wrong.
    Anything left behind is trimmed by the next save, which is why a stale row
    whose blob has already gone is recovered from rather than fatal.
    """
    stale = _ordered_revisions(file.id)[RETENTION:]
    if not stale:
        return
    try:
        for revision in stale:
            path = os.path.join(current_app.config["UPLOAD_FOLDER"], revision.storage_path)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                # Keep the row: deleting it would hide the blob rather than
                # free it.
                current_app.logger.exception("editor: could not remove revision blob %s", path)
                continue
            db.session.delete(revision)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception("editor: could not prune the revisions of file %s", file.id)


def _apply_content(file, data, base_revision):
    """Write new bytes over a file, keeping the bytes they replace.

    Returns ``(payload, status)``. The save route and the history restore route
    both come through here: restoring a version is an ordinary save whose
    content happens to come from a revision, which is what makes a restore
    itself restorable.

    The order below is the contract. The bytes about to be replaced are on disk
    as a revision *before* the new ones overwrite them, so a save that cannot
    be committed can be undone with what is already written.
    """
    # 1. Size first: a document the editor should never have sent is refused
    #    before anything is read or written.
    if len(data) > MAX_EDITABLE_FILE_SIZE:
        return {"error": "too_large", "limit": MAX_EDITABLE_FILE_SIZE}, 413

    # 2. The optimistic lock, and the only check that the bytes are still there.
    full_path = _full_path(file)
    if not os.path.exists(full_path):
        return {"error": "file_moved"}, 404
    with open(full_path, "rb") as handle:
        previous = handle.read()

    current_revision = content_revision(previous)
    if current_revision != base_revision:
        # Someone (or something) changed the file since the editor loaded it.
        # Hand back the revision that is actually on disk so the client can
        # offer "overwrite" without a second round trip.
        return {"error": "conflict", "revision": current_revision}, 409

    now = datetime.now(timezone.utc)

    # 3. Keep what is about to be replaced. The blob is named after the row's
    #    primary key, so the row is flushed to get one - a flush, not a commit:
    #    neither the revision nor the new size is visible until step 6. The
    #    empty path is a placeholder that satisfies NOT NULL until the flush
    #    hands back the id it is really named after.
    revision_storage_path = None
    try:
        revision = FileRevision(
            file_id=file.id, storage_path="", size=len(previous), created_at=now
        )
        db.session.add(revision)
        db.session.flush()
        revision_storage_path = _revision_storage_path(file.user_id, file.id, revision.id)
        revision.storage_path = revision_storage_path
        _write_atomically(_ensure_upload_dir(revision_storage_path), previous)
    except Exception:
        db.session.rollback()
        if revision_storage_path:
            _remove_revision_blob(revision_storage_path)
        current_app.logger.exception("editor: could not keep a revision of file %s", file.id)
        return {"error": "save_failed"}, 500

    # 4. The new bytes replace the old ones atomically. _ensure_upload_dir
    #    recreates the parent directory if the file's folder was moved on disk
    #    since the upload.
    try:
        _write_atomically(_ensure_upload_dir(file.storage_path), data)
    except OSError:
        db.session.rollback()
        _remove_revision_blob(revision_storage_path)
        current_app.logger.exception("editor: could not write %s", full_path)
        return {"error": "save_failed"}, 500

    # 5. Size and timestamp are both assigned explicitly: the columns' onupdate
    #    only fires when a value really changes, so rewriting content at the
    #    same byte count would leave updated_at stale even though the bytes did
    #    change (same note as api.py).
    file.size = len(data)
    file.updated_at = now

    # 6. The revision row, the new size and the file on disk become true
    #    together here - which is the point of writing the old bytes out first.
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
        # The revision this save wrote has no row now, so it would be a blob
        # nothing can ever reach.
        _remove_revision_blob(revision_storage_path)
        return {"error": "save_failed"}, 500

    # 7. Retention, and only now: see _prune_revisions.
    _prune_revisions(file)

    return {
        "revision": content_revision(data),
        "size": len(data),
        "updated_at": now.isoformat(),
    }, 200


@editor_bp.route("/files/new")
@login_required
def new_file():
    """Render a blank editor.

    Nothing is created here - the file appears on the first save, which posts to
    ``POST /files/new`` below. That is what makes the URL safe to reload, and it
    is why this page carries the name the user is about to use rather than a
    row id.
    """
    folder_id = request.args.get("folder_id", type=int)
    # A folder_id that is not the caller's is treated as "root" rather than an
    # error, matching how create_file() treats it on the upload path. (The POST
    # below is deliberately stricter - see the note there.)
    folder = None
    if folder_id:
        folder = Folder.query.filter_by(id=folder_id, user_id=current_user.id).first()
    folder_id = folder.id if folder else None

    # The base name from the dashboard's dialog arrives as ?name=. Stripped the
    # same way the POST strips it, so the field never shows "page.html" right
    # next to the fixed ".html" label.
    base_name = _base_name(request.args.get("name", "")) or DEFAULT_BASE_NAME

    return render_template(
        "editor/edit.html",
        mode="create",
        file=None,
        folder_id=folder_id,
        base_name=base_name,
        back_url=_folder_url(folder_id),
    )


@editor_bp.route("/files/new", methods=["POST"])
@login_required
def create_new_file():
    """Create the file the create-mode editor has been holding.

    Body: ``{name, content, folder_id}``. The order of the checks is part of the
    contract: a name that is taken is refused *before* anything is written, so a
    rejected create leaves neither a row nor a directory behind (and in
    particular no "page_1.html" - see the note on the clash check).
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid_request"}), 400
    name = payload.get("name")
    content = payload.get("content")
    folder_id = payload.get("folder_id")
    if not isinstance(name, str) or not isinstance(content, str):
        return (
            jsonify(
                {
                    "error": "invalid_request",
                    "message": "'name' and 'content' are required.",
                }
            ),
            400,
        )

    # 1. Strip a suffix the user typed anyway, then re-attach the fixed one.
    base = _base_name(name)
    final_name = base + HTML_SUFFIX

    # 2. The same validator every other write path uses. An empty base has to be
    #    refused on its own: ".html" would sail through the validator and leave
    #    a dot-file in the user's folder.
    if not base or not _is_valid_component_name(final_name):
        return jsonify({"error": "invalid_name"}), 400

    # 3. A folder_id that does not resolve is an error here, unlike the upload
    #    path where create_file() quietly falls back to the root. The editor got
    #    this id from a folder the user was looking at, so a miss means the
    #    folder is gone - filing the file at the root instead would put it
    #    somewhere the user is not looking.
    folder = None
    if folder_id not in (None, ""):
        folder = Folder.query.filter_by(id=folder_id, user_id=current_user.id).first()
        if folder is None:
            return jsonify({"error": "folder_not_found"}), 404
    folder_pk = folder.id if folder else None

    # 4. The editor's own limit - not MAX_CONTENT_LENGTH or MAX_API_CONTENT_SIZE,
    #    neither of which was written for this path.
    data = content.encode("utf-8")
    if len(data) > MAX_EDITABLE_FILE_SIZE:
        return jsonify({"error": "too_large", "limit": MAX_EDITABLE_FILE_SIZE}), 413

    # 5. create_file() resolves a clash by quietly renaming to "page_1.html".
    #    That is right for an upload - the bytes exist and have to land
    #    somewhere - and wrong here, where the user named one file and can
    #    simply be asked for another name. Checked before anything is created,
    #    so a 409 writes nothing. The upload path keeps its own behaviour.
    clash = File.query.filter_by(
        user_id=current_user.id, folder_id=folder_pk, name=final_name
    ).first()
    if clash:
        return jsonify({"error": "name_taken"}), 409

    # 6. The third caller of create_file(), alongside the web upload handler and
    #    the JSON API: one filename ruleset and one write path, as its docstring
    #    requires.
    file, error = create_file(current_user.id, final_name, data, folder_pk)
    if error:
        return jsonify({"error": "invalid_request", "message": error}), 400

    return (
        jsonify(
            {
                "id": file.id,
                "name": file.name,
                # 7. The hash the editor will save its next edit against.
                "revision": content_revision(data),
                "size": len(data),
                "edit_url": url_for("editor.edit_file", file_id=file.id),
            }
        ),
        201,
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
        # Unused here, but the name field is in the markup either way (a
        # successful first save turns the create page into this one).
        base_name="",
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
            payload["revision"] = content_revision(data)

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
    already minted for this file keeps working - which is the whole point. The
    bytes being replaced are kept as a revision first; see _apply_content.
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
    body, status = _apply_content(file, content.encode("utf-8"), base_revision)
    return jsonify(body), status


@editor_bp.route("/files/<int:file_id>/history")
@login_required
def list_history(file_id):
    """The revisions kept for this file, newest first.

    Deliberately without content: ten revisions of up to
    MAX_EDITABLE_FILE_SIZE each would make this the largest response in the
    app, and the panel only ever shows one at a time.
    """
    file = _file_or_404(file_id)
    response = jsonify(
        {
            "revisions": [
                {
                    "id": revision.id,
                    "size": revision.size,
                    "created_at": _iso_utc(revision.created_at),
                }
                for revision in _ordered_revisions(file.id)
            ],
            "retention": RETENTION,
        }
    )
    # Same reason as get_content: prevent_caching only covers text/html, and a
    # stale history list is what a save followed by a reload would show.
    response.headers["Cache-Control"] = "no-store"
    return response


@editor_bp.route("/files/<int:file_id>/history/<int:revision_id>")
@login_required
def get_revision(file_id, revision_id):
    """One revision's content, for the panel's preview."""
    file = _file_or_404(file_id)
    revision = _revision_or_404(file, revision_id)

    data = _revision_blob(revision)
    if data is None:
        return jsonify({"error": "revision_moved"}), 404

    try:
        # utf-8-sig, matching get_content. Reaching the except would mean bytes
        # that were never served as editable: a revision is only ever written
        # from content that passed the UTF-8 decode on its way into the editor,
        # so this is a guard against a hand-edited database or disk, not a case
        # the app can produce.
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "revision_not_utf8"}), 404

    response = jsonify(
        {
            "id": revision.id,
            "content": text,
            "created_at": _iso_utc(revision.created_at),
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@editor_bp.route("/files/<int:file_id>/history/<int:revision_id>/restore", methods=["POST"])
@login_required
def restore_revision(file_id, revision_id):
    """Put an old revision back. Body: {base_revision}.

    The same optimistic lock as a normal save, because the file can have been
    changed while the history panel was open. The content being replaced is
    itself kept as a revision, so a restore can be rolled back like anything
    else - and the response is the save endpoint's, 409 and 413 included.
    """
    file = _file_or_404(file_id)
    revision = _revision_or_404(file, revision_id)

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "invalid_request"}), 400
    base_revision = payload.get("base_revision")
    if not isinstance(base_revision, str):
        return (
            jsonify(
                {
                    "error": "invalid_request",
                    "message": "'base_revision' is required.",
                }
            ),
            400,
        )

    data = _revision_blob(revision)
    if data is None:
        return jsonify({"error": "revision_moved"}), 404

    body, status = _apply_content(file, data, base_revision)
    return jsonify(body), status


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
