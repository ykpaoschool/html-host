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

dashboard_bp = Blueprint("dashboard", __name__)


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
        breadcrumbs=breadcrumbs,
    )


@dashboard_bp.route("/files/upload", methods=["POST"])
@login_required
def upload_file():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        flash(t("upload_file"), "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    filename = uploaded.filename
    if not filename.lower().endswith(".html") and not filename.lower().endswith(
        ".htm"
    ):
        flash("Only HTML files are allowed", "error")
        return redirect(request.referrer or url_for("dashboard.index"))

    folder_id = request.form.get("folder_id", type=int)
    folder = None
    if folder_id:
        folder = Folder.query.filter_by(
            id=folder_id, user_id=current_user.id
        ).first()

    content = uploaded.read()
    size = len(content)

    # Generate unique filename if needed
    base_name = filename
    counter = 1
    existing = File.query.filter_by(
        user_id=current_user.id, folder_id=folder.id if folder else None, name=base_name
    ).first()
    while existing:
        name_part, ext = os.path.splitext(filename)
        base_name = f"{name_part}_{counter}{ext}"
        counter += 1
        existing = File.query.filter_by(
            user_id=current_user.id,
            folder_id=folder.id if folder else None,
            name=base_name,
        ).first()

    storage_path = _get_storage_path(current_user.id, folder, base_name)
    full_path = _ensure_upload_dir(storage_path)
    with open(full_path, "wb") as f:
        f.write(content)

    file = File(
        user_id=current_user.id,
        folder_id=folder.id if folder else None,
        name=base_name,
        storage_path=storage_path,
        size=size,
    )
    db.session.add(file)
    db.session.commit()

    return redirect(
        url_for("dashboard.view_folder", folder_id=folder.id)
        if folder
        else url_for("dashboard.index")
    )


@dashboard_bp.route("/files/<int:file_id>/rename", methods=["POST"])
@login_required
def rename_file(file_id):
    file = File.query.filter_by(id=file_id, user_id=current_user.id).first_or_404()
    new_name = request.form.get("name", "").strip()
    if not new_name:
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

    folder.name = new_name
    db.session.commit()

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

    # Check for circular reference
    if target_parent_id:
        target = Folder.query.get(target_parent_id)
        current = target
        while current:
            if current.id == folder.id:
                return redirect(request.referrer)
            current = current.parent

    folder.parent_id = target_parent_id if target_parent_id else None
    db.session.commit()

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


def _delete_folder_recursive(folder):
    for child in folder.children:
        _delete_folder_recursive(child)
    for file in folder.files:
        full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)
        if os.path.exists(full_path):
            os.remove(full_path)
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
    expires_at = request.form.get("expires_at")
    if expires_at:
        from datetime import datetime

        expires_at = datetime.fromisoformat(expires_at)
    else:
        expires_at = None

    link = ShareLink(
        file_id=file.id,
        token=token,
        expires_at=expires_at,
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
        expires_at = request.form.get("expires_at")
        if expires_at:
            from datetime import datetime
            link.expires_at = datetime.fromisoformat(expires_at)
        else:
            link.expires_at = None

    db.session.commit()
    return redirect(request.referrer or url_for("dashboard.index"))
