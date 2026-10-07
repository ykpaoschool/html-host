import os
import secrets
import shutil

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    request,
    url_for,
)
from flask_login import current_user, login_required

from i18n import t
from models import File, Folder, ShareLink, db
from projects import _parse_expiry

dashboard_bp = Blueprint("dashboard", __name__)


def _is_valid_component_name(name):
    """True if name is safe to use as a single path segment on disk.

    File and folder names are joined into storage paths without further
    normalization (see _get_storage_path and Folder.get_path), so a name
    containing a separator or a '..' segment would traverse outside the
    user's upload directory - e.g. an upload named
    '../../../templates/base.html' could overwrite an application template.
    Unlike werkzeug's secure_filename this only rejects, never rewrites, so
    non-ASCII (e.g. Chinese) filenames keep working.
    """
    if not name or name in (".", ".."):
        return False
    if any(ch in name for ch in ("/", "\\", "\x00")):
        return False
    return True


def _get_storage_path(user_id, folder, filename):
    parts = [str(user_id)]
    if folder:
        parts.append(folder.get_path().replace("/", os.sep))
    parts.append(filename)
    return os.path.join(*parts)


def _ensure_upload_dir(storage_path):
    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], storage_path)
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    return full_path


# --- Editor version history: the disk half ---------------------------------
#
# Revision blobs live in UPLOAD_FOLDER/.history/<user_id>/<file_id>/<revision id>,
# a sibling of the user's own directory rather than a child of it:
# _is_valid_component_name lets a user create a folder literally called
# ".history", so nesting this inside <user_id>/ would let the two collide.
# Indexing by file id (not by storage_path) is what keeps a renamed or moved
# file's history reachable.
#
# The FileRevision *rows* need nothing extra - File.revisions cascades, and all
# four File deletion paths go through the ORM. The blobs are not rows, so these
# two helpers exist and are called from every one of those paths.

HISTORY_DIRNAME = ".history"


def history_dir(user_id, file_id):
    """The directory holding one file's revision blobs."""
    return os.path.join(
        current_app.config["UPLOAD_FOLDER"],
        HISTORY_DIRNAME,
        str(user_id),
        str(file_id),
    )


def delete_file_history(user_id, file_id):
    """Drop one file's revision blobs, rows or no rows.

    ignore_errors: this runs on the delete paths, where a missing directory is
    the ordinary case (a file saved once or never has none) and must not turn a
    delete into a 500.
    """
    shutil.rmtree(history_dir(user_id, file_id), ignore_errors=True)


def delete_user_history(user_id):
    """Drop every revision blob belonging to a user.

    admin.delete_user clears UPLOAD_FOLDER/<user_id> with a single rmtree, which
    cannot reach .history - it is a sibling of that directory, not a child of
    it, so without this call deleting a user leaks all of their history.
    """
    shutil.rmtree(
        os.path.join(current_app.config["UPLOAD_FOLDER"], HISTORY_DIRNAME, str(user_id)),
        ignore_errors=True,
    )


def create_file(user_id, name, content, folder_id=None):
    """Create a File row and write its bytes to disk.

    Shared by the Web UI upload handler and the JSON API so both enforce the
    same filename rules and the same uniquifying scheme. Returns
    ``(file, error)``: on success ``error`` is None, on failure ``file`` is
    None and ``error`` is a human-readable message.

    ``folder_id`` that does not resolve to one of the user's folders is
    treated as "no folder" rather than an error, matching the Web UI.
    """
    if not name:
        return None, "A filename is required."
    if not _is_valid_component_name(name):
        return None, "Filenames may not contain '/', '\\' or '..'."
    if not name.lower().endswith(".html") and not name.lower().endswith(".htm"):
        return None, "Only HTML files are allowed"

    folder = None
    if folder_id:
        folder = Folder.query.filter_by(id=folder_id, user_id=user_id).first()

    size = len(content)

    # Generate unique filename if needed
    base_name = name
    counter = 1
    existing = File.query.filter_by(
        user_id=user_id, folder_id=folder.id if folder else None, name=base_name
    ).first()
    while existing:
        name_part, ext = os.path.splitext(name)
        base_name = f"{name_part}_{counter}{ext}"
        counter += 1
        existing = File.query.filter_by(
            user_id=user_id,
            folder_id=folder.id if folder else None,
            name=base_name,
        ).first()

    storage_path = _get_storage_path(user_id, folder, base_name)
    full_path = _ensure_upload_dir(storage_path)
    with open(full_path, "wb") as f:
        f.write(content)

    file = File(
        user_id=user_id,
        folder_id=folder.id if folder else None,
        name=base_name,
        storage_path=storage_path,
        size=size,
    )
    db.session.add(file)
    db.session.commit()
    return file, None


@dashboard_bp.route("/")
@login_required
def index():
    return _render_folder(None)


@dashboard_bp.route("/folder/<int:folder_id>")
@login_required
def view_folder(folder_id):
    folder = Folder.query.filter_by(
        id=folder_id, user_id=current_user.id
    ).first_or_404()
    return _render_folder(folder)


def _render_folder(folder):
    from flask import render_template

    parent_id = folder.id if folder else None
    folders = Folder.query.filter_by(
        user_id=current_user.id, parent_id=parent_id
    ).order_by(Folder.name).all()
    files = File.query.filter_by(
        user_id=current_user.id, folder_id=parent_id
    ).order_by(File.name).all()

    share_links = (
        ShareLink.query.join(File)
        .filter(File.user_id == current_user.id)
        .order_by(ShareLink.created_at.desc())
        .all()
    )

    breadcrumbs = []
    if folder:
        current = folder
        while current:
            breadcrumbs.insert(0, current)
            current = current.parent

    return render_template(
        "dashboard/index.html",
        current_folder=folder,
        folders=folders,
        files=files,
        share_links=share_links,
        breadcrumbs=breadcrumbs,
    )


@dashboard_bp.route("/files/upload", methods=["POST"])
@login_required
def upload_file():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash(t("upload_file"), "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    file, error = create_file(
        current_user.id,
        uploaded.filename,
        uploaded.read(),
        request.form.get("folder_id", type=int),
    )
    if error:
        flash(error, "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    return redirect(
        url_for("dashboard.view_folder", folder_id=file.folder_id)
        if file.folder_id
        else url_for("dashboard.index")
    )


@dashboard_bp.route("/files/<int:file_id>/rename", methods=["POST"])
@login_required
def rename_file(file_id):
    file = File.query.filter_by(id=file_id, user_id=current_user.id).first_or_404()
    new_name = request.form.get("name", "").strip()
    if not new_name:
        return redirect(request.referrer or url_for("dashboard.index"))
    if not _is_valid_component_name(new_name):
        flash("Filenames may not contain '/', '\\' or '..'.", "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    # Rename on filesystem
    old_full = os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)
    folder = file.folder
    new_storage = _get_storage_path(current_user.id, folder, new_name)
    new_full = _ensure_upload_dir(new_storage)
    if os.path.exists(old_full):
        os.rename(old_full, new_full)

    file.name = new_name
    file.storage_path = new_storage
    db.session.commit()

    return redirect(
        url_for("dashboard.view_folder", folder_id=file.folder_id)
        if file.folder_id
        else url_for("dashboard.index")
    )


@dashboard_bp.route("/files/<int:file_id>/move", methods=["POST"])
@login_required
def move_file(file_id):
    file = File.query.filter_by(id=file_id, user_id=current_user.id).first_or_404()
    target_folder_id = request.form.get("folder_id", type=int)
    target_folder = None
    if target_folder_id:
        target_folder = Folder.query.filter_by(
            id=target_folder_id, user_id=current_user.id
        ).first_or_404()

    old_full = os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)
    new_storage = _get_storage_path(current_user.id, target_folder, file.name)
    new_full = _ensure_upload_dir(new_storage)
    if os.path.exists(old_full):
        shutil.move(old_full, new_full)
        # Clean up empty dirs
        old_dir = os.path.dirname(old_full)
        if os.path.isdir(old_dir) and not os.listdir(old_dir):
            os.rmdir(old_dir)

    file.folder_id = target_folder.id if target_folder else None
    file.storage_path = new_storage
    db.session.commit()

    return redirect(
        url_for("dashboard.view_folder", folder_id=file.folder_id)
        if file.folder_id
        else url_for("dashboard.index")
    )


@dashboard_bp.route("/files/<int:file_id>/delete", methods=["POST"])
@login_required
def delete_file(file_id):
    file = File.query.filter_by(id=file_id, user_id=current_user.id).first_or_404()
    folder_id = file.folder_id

    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)
    if os.path.exists(full_path):
        os.remove(full_path)

    # The revision blobs have to be told about separately, and before the row
    # goes: nothing points at the directory once this returns.
    delete_file_history(file.user_id, file.id)

    for link in file.share_links:
        db.session.delete(link)
    db.session.delete(file)
    db.session.commit()

    return redirect(
        url_for("dashboard.view_folder", folder_id=folder_id)
        if folder_id
        else url_for("dashboard.index")
    )


@dashboard_bp.route("/folders/create", methods=["POST"])
@login_required
def create_folder():
    name = request.form.get("name", "").strip()
    parent_id = request.form.get("parent_id", type=int)

    if not name:
        return redirect(request.referrer or url_for("dashboard.index"))
    if not _is_valid_component_name(name):
        flash("Folder names may not contain '/', '\\' or '..'.", "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    # Only accept a parent folder owned by the caller; a foreign id would
    # nest this folder into another user's tree.
    if parent_id and not Folder.query.filter_by(
        id=parent_id, user_id=current_user.id
    ).first():
        parent_id = None

    folder = Folder(
        user_id=current_user.id,
        parent_id=parent_id if parent_id else None,
        name=name,
    )
    db.session.add(folder)
    db.session.commit()

    return redirect(
        url_for("dashboard.view_folder", folder_id=parent_id)
        if parent_id
        else url_for("dashboard.index")
    )


@dashboard_bp.route("/folders/<int:folder_id>/rename", methods=["POST"])
@login_required
def rename_folder(folder_id):
    folder = Folder.query.filter_by(
        id=folder_id, user_id=current_user.id
    ).first_or_404()
    new_name = request.form.get("name", "").strip()
    if not new_name:
        return redirect(request.referrer or url_for("dashboard.index"))
    if not _is_valid_component_name(new_name):
        flash("Folder names may not contain '/', '\\' or '..'.", "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    upload_folder = current_app.config["UPLOAD_FOLDER"]
    old_disk_dir = os.path.join(upload_folder, str(folder.user_id), folder.get_path())

    folder.name = new_name
    new_disk_dir = os.path.join(upload_folder, str(folder.user_id), folder.get_path())

    if os.path.isdir(old_disk_dir) and old_disk_dir != new_disk_dir:
        os.makedirs(os.path.dirname(new_disk_dir), exist_ok=True)
        shutil.move(old_disk_dir, new_disk_dir)
        # Clean up empty parent dirs left behind
        old_parent = os.path.dirname(old_disk_dir)
        if os.path.isdir(old_parent) and not os.listdir(old_parent):
            os.rmdir(old_parent)

    removed = _sync_descendants(folder, upload_folder)
    db.session.commit()

    if removed:
        flash(t("shares_auto_removed", count=removed), "info")

    return redirect(
        url_for("dashboard.view_folder", folder_id=folder.id)
    )


@dashboard_bp.route("/folders/<int:folder_id>/move", methods=["POST"])
@login_required
def move_folder(folder_id):
    folder = Folder.query.filter_by(
        id=folder_id, user_id=current_user.id
    ).first_or_404()
    target_parent_id = request.form.get("parent_id", type=int)
    if target_parent_id == folder.id:
        return redirect(request.referrer)

    # Check for circular reference; the parent must also be one of the
    # caller's own folders, or the move would nest into another user's tree.
    if target_parent_id:
        target = Folder.query.filter_by(
            id=target_parent_id, user_id=current_user.id
        ).first()
        if not target:
            return redirect(request.referrer or url_for("dashboard.index"))
        current = target
        while current:
            if current.id == folder.id:
                return redirect(request.referrer)
            current = current.parent

    upload_folder = current_app.config["UPLOAD_FOLDER"]
    old_disk_dir = os.path.join(upload_folder, str(folder.user_id), folder.get_path())

    if target_parent_id:
        folder.parent = target
    else:
        folder.parent = None
    new_disk_dir = os.path.join(upload_folder, str(folder.user_id), folder.get_path())

    if os.path.isdir(old_disk_dir) and old_disk_dir != new_disk_dir:
        os.makedirs(os.path.dirname(new_disk_dir), exist_ok=True)
        shutil.move(old_disk_dir, new_disk_dir)
        # Clean up empty parent dirs left behind
        old_parent = os.path.dirname(old_disk_dir)
        if os.path.isdir(old_parent) and not os.listdir(old_parent):
            os.rmdir(old_parent)

    removed = _sync_descendants(folder, upload_folder)
    db.session.commit()

    if removed:
        flash(t("shares_auto_removed", count=removed), "info")

    return redirect(url_for("dashboard.view_folder", folder_id=folder.id))


@dashboard_bp.route("/folders/<int:folder_id>/delete", methods=["POST"])
@login_required
def delete_folder(folder_id):
    folder = Folder.query.filter_by(
        id=folder_id, user_id=current_user.id
    ).first_or_404()
    parent_id = folder.parent_id

    _delete_folder_recursive(folder)
    db.session.commit()

    return redirect(
        url_for("dashboard.view_folder", folder_id=parent_id)
        if parent_id
        else url_for("dashboard.index")
    )


def _get_all_descendant_folders(folder):
    """Return the folder and all its descendants (depth-first)."""
    result = []
    for child in folder.children:
        result.extend(_get_all_descendant_folders(child))
    result.append(folder)
    return result


def _sync_descendants(folder, upload_folder):
    """Update storage_path for all files in folder and descendants, remove broken share links.
    Returns the number of share links removed."""
    removed = 0
    for f in _get_all_descendant_folders(folder):
        for file in f.files:
            file.storage_path = _get_storage_path(folder.user_id, file.folder, file.name)
            for link in list(file.share_links):
                db.session.delete(link)
                removed += 1
    return removed


def _delete_folder_recursive(folder):
    for child in folder.children:
        _delete_folder_recursive(child)
    for file in folder.files:
        full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)
        if os.path.exists(full_path):
            os.remove(full_path)
        # Deleting a folder deletes the files in it, so it has to clear their
        # history too - this is the path that is easiest to forget.
        delete_file_history(file.user_id, file.id)
        for link in file.share_links:
            db.session.delete(link)
        db.session.delete(file)
    disk_dir = os.path.join(current_app.config["UPLOAD_FOLDER"], str(folder.user_id), folder.get_path())
    db.session.delete(folder)
    if os.path.isdir(disk_dir) and not os.listdir(disk_dir):
        os.rmdir(disk_dir)


@dashboard_bp.route("/files/<int:file_id>/share", methods=["POST"])
@login_required
def create_share(file_id):
    file = File.query.filter_by(id=file_id, user_id=current_user.id).first_or_404()

    token = secrets.token_urlsafe(32)
    expires_at = _parse_expiry(request.form.get("expires_at"))
    require_login = request.form.get("require_login") == "1"

    link = ShareLink(
        file_id=file.id,
        token=token,
        expires_at=expires_at,
        require_login=require_login,
    )
    db.session.add(link)
    db.session.commit()

    share_url = url_for("share.view", token=token, _external=True)
    return jsonify({"url": share_url, "token": token, "id": link.id})


@dashboard_bp.route("/shares/<int:share_id>", methods=["POST"])
@login_required
def update_share(share_id):
    link = ShareLink.query.join(File).filter(
        ShareLink.id == share_id, File.user_id == current_user.id
    ).first_or_404()

    action = request.form.get("action")
    if action == "toggle":
        link.is_active = not link.is_active
    elif action == "delete":
        db.session.delete(link)
    elif action == "update_expiry":
        link.expires_at = _parse_expiry(request.form.get("expires_at"))

    db.session.commit()
    return redirect(request.referrer or url_for("dashboard.index"))
