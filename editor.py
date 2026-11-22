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
primary key. That ordering lives in ``editor_core.apply_content`` now - shared
with the project editor, which is why it is not a detail to rearrange. This
module keeps only what is specific to a ``File`` row: which table a revision
belongs to and where its blobs live.
"""

import os

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
    _is_valid_component_name,
    create_file,
    history_dir,
)
from editor_core import (
    MAX_EDITABLE_FILE_SIZE,
    RETENTION,
    READONLY_NOT_UTF8,
    READONLY_TOO_LARGE,
    apply_content,
    detect_line_separator,
    iso_utc,
    ordered_revisions,
    read_revision_blob,
)
from models import File, FileRevision, Folder, content_revision

editor_bp = Blueprint("editor", __name__)

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


def _folder_url(folder_id):
    if folder_id:
        return url_for("dashboard.view_folder", folder_id=folder_id)
    return url_for("dashboard.index")


def _editor_urls(file_id):
    """The endpoint URLs the editor template fetches, or None where the scope
    has no such endpoint.

    Everything the template calls comes from here rather than being assembled
    in JavaScript: ``url_for`` is what percent-encodes a file name with spaces,
    '#' or Chinese characters, and the project editor injects the same shape of
    object with different routes. ``history`` and ``restore`` are the
    collection URLs; the revision id is appended by the page. In create mode
    the file does not exist yet, so only ``create`` is usable.
    """
    create_url = url_for("editor.create_new_file")
    if file_id is None:
        return {
            "content": None,
            "history": None,
            "restore": None,
            "download": None,
            "editBase": None,
            "preview": None,
            "create": create_url,
        }
    history_url = url_for("editor.list_history", file_id=file_id)
    return {
        "content": url_for("editor.get_content", file_id=file_id),
        "history": history_url,
        "restore": history_url,
        "download": url_for("editor.download", file_id=file_id),
        "editBase": url_for("editor.edit_file", file_id=file_id),
        "preview": None,
        "create": create_url,
    }


def _revision_or_404(file, revision_id):
    """One of *this* file's revisions, or 404.

    Filtered by file_id as well as by id: a revision id belonging to another
    file - the caller's own or anyone else's - must not be reachable through
    this file's routes.
    """
    return FileRevision.query.filter_by(id=revision_id, file_id=file.id).first_or_404()


def _save(file, data, base_revision):
    """Save one File through the shared pipeline.

    The revision table and the blob directory are the only things this module
    contributes; the ordering and the error contract are editor_core's.
    """
    return apply_content(
        file,
        data,
        base_revision,
        revision_model=FileRevision,
        fk_field="file_id",
        history_dir=history_dir(file.user_id, file.id),
        max_size=MAX_EDITABLE_FILE_SIZE,
        retention=RETENTION,
    )


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
        scope="file",
        mode="create",
        file=None,
        folder_id=folder_id,
        base_name=base_name,
        current_path="",
        back_url=_folder_url(folder_id),
        urls=_editor_urls(None),
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
                "download_url": url_for("editor.download", file_id=file.id),
                # The create page was rendered before the file existed, so its
                # urls object has no content/history routes yet. Hand them over
                # here rather than letting the page assemble /files/<id>/...
                # paths on the client.
                "content_url": url_for("editor.get_content", file_id=file.id),
                "history_url": url_for("editor.list_history", file_id=file.id),
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
        scope="file",
        mode="edit",
        file=file,
        folder_id=file.folder_id,
        # Unused here, but the name field is in the markup either way (a
        # successful first save turns the create page into this one).
        base_name="",
        current_path="",
        back_url=_folder_url(file.folder_id),
        urls=_editor_urls(file.id),
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
        "updated_at": iso_utc(file.updated_at),
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
            payload["line_separator"] = detect_line_separator(text)
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
    bytes being replaced are kept as a revision first; see editor_core.apply_content.
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
    body, status = _save(file, content.encode("utf-8"), base_revision)
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
                    "created_at": iso_utc(revision.created_at),
                }
                for revision in ordered_revisions(FileRevision, "file_id", file.id)
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

    data = read_revision_blob(revision.storage_path)
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
            "created_at": iso_utc(revision.created_at),
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

    data = read_revision_blob(revision.storage_path)
    if data is None:
        return jsonify({"error": "revision_moved"}), 404

    body, status = _save(file, data, base_revision)
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
