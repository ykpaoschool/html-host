"""The save pipeline shared by the single-file and the project editors.

``editor.py`` (one ``File`` row) and ``project_editor.py`` (one
``ProjectFile`` row) both let a user rewrite a blob in place behind an
optimistic lock. The parts that must behave identically - the atomic write, the
optimistic-lock check, the "keep the bytes you are about to replace" ordering,
the revision blobs, the retention trim - live here so the two callers cannot
drift apart. Everything that differs between them (which table a revision row
belongs to, where its blobs live) is a parameter.

The write order in ``apply_content`` is the contract, not an implementation
detail:

1. size check, 2. read the current bytes and check ``base_revision``,
3. write the *old* bytes out as a revision, 4. atomically replace them with the
new ones, 5. commit, 6. restore the old bytes if the commit fails, 7. prune.

Writing the revision last would leave a save that failed to commit with no way
back to what it replaced; pruning before the commit would offer the history
panel a version whose blob had already been deleted.

Nothing is cached in process memory: the lock value is a SHA-256 recomputed
from disk on every read, so two gunicorn workers cannot disagree about it.
"""

import os
import tempfile
from datetime import datetime, timezone

from flask import current_app

from models import content_revision, db

# The single "one file" limit for online editing, shared by both editors so the
# read-only check and the save check cannot drift apart. It constrains the
# editor only: uploads keep their own limits (50 MB global / 10 MB per file
# inside projects), so a larger file still uploads and still gets share links -
# it just cannot be edited in the browser.
MAX_EDITABLE_FILE_SIZE = 2 * 1024 * 1024

# How many revisions of one file are kept. Bounded on purpose: at the size the
# editor accepts, the history of one file costs at most this many times
# MAX_EDITABLE_FILE_SIZE on disk. If that threshold is ever raised, this is the
# number that has to be looked at again (editor requirements F9).
RETENTION = 10

# Extensions the browser editor accepts. Everything else in a project (images,
# fonts, archives) is still listed and downloadable, just not editable - a
# CodeMirror buffer is not a place to round-trip binary bytes.
EDITABLE_EXTENSIONS = frozenset(
    {
        ".html",
        ".htm",
        ".css",
        ".js",
        ".mjs",
        ".json",
        ".xml",
        ".txt",
        ".csv",
        ".md",
        ".map",
        ".svg",
    }
)

# Which CodeMirror language mode to attach, by extension. Everything not named
# here is edited as plain text; the bundle exposes html(), css() and
# javascript() only.
LANGUAGE_BY_EXTENSION = {
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".js": "javascript",
    ".mjs": "javascript",
    ".svg": "html",
    ".xml": "html",
}

# ``readonly_reason`` values returned to the browser. Strings rather than enum
# members because they cross the JSON boundary and the template maps them to
# translated messages.
READONLY_TOO_LARGE = "too_large"
READONLY_NOT_UTF8 = "not_utf8"
READONLY_NOT_EDITABLE = "not_editable"


def is_editable_path(path):
    """True when the editor accepts this project path as text."""
    return os.path.splitext(path)[1].lower() in EDITABLE_EXTENSIONS


def language_for(path):
    """The CodeMirror mode for a path: html / css / javascript / text."""
    return LANGUAGE_BY_EXTENSION.get(os.path.splitext(path)[1].lower(), "text")


def iso_utc(value):
    """Serialize a datetime as UTC-aware.

    SQLite hands back naive datetimes; a naive value means UTC, the same
    convention ``models._is_expired`` documents.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def detect_line_separator(text):
    return "crlf" if "\r\n" in text else "lf"


def write_atomically(full_path, data):
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


def revision_storage_path(upload_folder, directory, revision_id):
    """Where one revision's blob lives, relative to UPLOAD_FOLDER.

    Relative, like every other storage_path in the app, and derived from the
    caller's directory helper so the writer and the deleters cannot end up with
    two ideas of where history lives.
    """
    return os.path.relpath(os.path.join(directory, str(revision_id)), upload_folder)


def read_revision_blob(storage_path):
    """A revision's bytes, or None if the row outlived them.

    None rather than an exception so both history routes answer 404 the same
    way a missing file does: a row whose bytes are gone is a 404, not an empty
    viewer that looks like an empty document.
    """
    path = os.path.join(current_app.config["UPLOAD_FOLDER"], storage_path)
    if not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return handle.read()


def remove_revision_blob(storage_path):
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


def ordered_revisions(revision_model, fk_field, parent_id):
    """A parent's revisions, newest first.

    The id breaks ties: created_at only resolves to the microsecond, and two
    revisions written inside the same one would otherwise come back in an
    arbitrary order, letting the panel and the retention pruner disagree about
    which is the oldest.
    """
    return (
        revision_model.query.filter_by(**{fk_field: parent_id})
        .order_by(revision_model.created_at.desc(), revision_model.id.desc())
        .all()
    )


def prune_revisions(target, *, revision_model, fk_field, retention):
    """Trim a target's history down to the newest ``retention`` revisions.

    Called only once the save has been committed: pruning first would let a
    failed commit leave the panel offering a revision whose blob had already
    been deleted. The blob goes before the row, which is the order the rest of
    the app deletes in - a row left pointing at nothing is the failure this
    prefers to bytes nothing points at.

    Housekeeping, and treated as such: a save that has already been committed
    must not be reported to the user as failed because the trim went wrong.
    Anything left behind is trimmed by the next save, which is why a stale row
    whose blob has already gone is recovered from rather than fatal.
    """
    stale = ordered_revisions(revision_model, fk_field, target.id)[retention:]
    if not stale:
        return
    upload_folder = current_app.config["UPLOAD_FOLDER"]
    try:
        for revision in stale:
            path = os.path.join(upload_folder, revision.storage_path)
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                # Keep the row: deleting it would hide the blob rather than
                # free it.
                current_app.logger.exception(
                    "editor: could not remove revision blob %s", path
                )
                continue
            db.session.delete(revision)
        db.session.commit()
    except Exception:
        db.session.rollback()
        current_app.logger.exception(
            "editor: could not prune the revisions of %s %s",
            type(target).__name__,
            target.id,
        )


def apply_content(
    target,
    data,
    base_revision,
    *,
    revision_model,
    fk_field,
    history_dir,
    max_size,
    retention,
):
    """Write new bytes over a target, optionally keeping the bytes replaced.

    Returns ``(payload, status)``. The single-file save route and its history
    restore route both come through here: restoring a version is an ordinary
    save whose content happens to come from a revision, which is what makes a
    restore itself restorable.

    ``revision_model`` is None for a caller that keeps no history (the project
    editor in C1): steps 3, 6's blob cleanup and 7 are then skipped, and
    ``fk_field`` / ``history_dir`` / ``retention`` are ignored. Otherwise
    ``fk_field`` is the foreign-key column name on the revision row and
    ``history_dir`` the absolute directory its blobs live in.

    The order below is the contract. The bytes about to be replaced are on disk
    as a revision *before* the new ones overwrite them, so a save that cannot
    be committed can be undone with what is already written.
    """
    # 1. Size first: a document the editor should never have sent is refused
    #    before anything is read or written.
    if len(data) > max_size:
        return {"error": "too_large", "limit": max_size}, 413

    # 2. The optimistic lock, and the only check that the bytes are still there.
    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], target.storage_path)
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
    revision_storage = None
    if revision_model is not None:
        try:
            revision = revision_model(
                **{
                    fk_field: target.id,
                    "storage_path": "",
                    "size": len(previous),
                    "created_at": now,
                }
            )
            db.session.add(revision)
            db.session.flush()
            revision_storage = revision_storage_path(
                current_app.config["UPLOAD_FOLDER"], history_dir, revision.id
            )
            revision.storage_path = revision_storage
            os.makedirs(history_dir, exist_ok=True)
            write_atomically(
                os.path.join(current_app.config["UPLOAD_FOLDER"], revision_storage),
                previous,
            )
        except Exception:
            db.session.rollback()
            if revision_storage:
                remove_revision_blob(revision_storage)
            current_app.logger.exception(
                "editor: could not keep a revision of %s %s",
                type(target).__name__,
                target.id,
            )
            return {"error": "save_failed"}, 500

    # 4. The new bytes replace the old ones atomically. Creating the parent
    #    directory first recreates it if the file's folder was moved on disk
    #    since the upload.
    try:
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        write_atomically(full_path, data)
    except OSError:
        db.session.rollback()
        if revision_storage:
            remove_revision_blob(revision_storage)
        current_app.logger.exception("editor: could not write %s", full_path)
        return {"error": "save_failed"}, 500

    # 5. Size and timestamp are both assigned explicitly: the columns' onupdate
    #    only fires when a value really changes, so rewriting content at the
    #    same byte count would leave updated_at stale even though the bytes did
    #    change (same note as api.py).
    target.size = len(data)
    target.updated_at = now

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
            write_atomically(full_path, previous)
        except OSError:
            current_app.logger.exception(
                "editor: rollback of %s failed, on-disk content is newer "
                "than the database says",
                full_path,
            )
        # The revision this save wrote has no row now, so it would be a blob
        # nothing can ever reach.
        if revision_storage:
            remove_revision_blob(revision_storage)
        return {"error": "save_failed"}, 500

    # 7. Retention, and only now: see prune_revisions.
    if revision_model is not None and retention is not None:
        prune_revisions(
            target,
            revision_model=revision_model,
            fk_field=fk_field,
            retention=retention,
        )

    return {
        "revision": content_revision(data),
        "size": len(data),
        "updated_at": now.isoformat(),
    }, 200
