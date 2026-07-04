import os
import shutil

import bcrypt
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from i18n import t
from models import File, Folder, Project, ShareLink, User, db

admin_bp = Blueprint("admin", __name__)


def admin_required(f):
    from functools import wraps

    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_admin:
            return render_template("403.html"), 403
        return f(*args, **kwargs)

    return decorated


@admin_bp.route("/")
@login_required
@admin_required
def index():
    users = User.query.order_by(User.created_at).all()
    return render_template("admin/users.html", users=users)


@admin_bp.route("/users/create", methods=["GET", "POST"])
@login_required
@admin_required
def create_user():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        display_name = request.form.get("display_name", "").strip()
        is_admin = request.form.get("is_admin") == "on"

        if not email or not password or not display_name:
            flash("All fields are required", "error")
            return redirect(request.referrer)

        if User.query.filter_by(email=email).first():
            flash("Email already exists", "error")
            return redirect(request.referrer)

        password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

        user = User(
            email=email,
            password_hash=password_hash,
            auth_provider="local",
            display_name=display_name,
            is_admin=is_admin,
        )
        db.session.add(user)
        db.session.commit()

        flash(t("user_created"), "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/create_user.html")


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def edit_user(user_id):
    user = User.query.get_or_404(user_id)
    if request.method == "POST":
        user.display_name = request.form.get("display_name", "").strip()
        user.is_admin = request.form.get("is_admin") == "on"

        new_password = request.form.get("password", "").strip()
        if new_password and user.auth_provider == "local":
            user.password_hash = bcrypt.hashpw(
                new_password.encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")

        db.session.commit()
        flash(t("user_updated"), "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/edit_user.html", edit_user=user)


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Cannot delete yourself", "error")
        return redirect(url_for("admin.index"))

    user_upload_dir = os.path.join(
        current_app.config["UPLOAD_FOLDER"], str(user.id)
    )
    if os.path.exists(user_upload_dir):
        shutil.rmtree(user_upload_dir)

    # Delete related objects explicitly in the correct order
    # to avoid cascade conflicts with self-referential Folder
    for file in list(user.files):
        for link in list(file.share_links):
            db.session.delete(link)
        db.session.delete(file)
    folders = sorted(user.folders, key=lambda f: f.get_path().count("/"), reverse=True)
    for folder in folders:
        db.session.delete(folder)
    # Projects cascade-delete their files + share links; the disk dir is
    # already removed by the rmtree above, so only DB rows remain.
    for project in list(user.projects):
        db.session.delete(project)
    db.session.delete(user)
    db.session.commit()

    flash(t("user_deleted"), "success")
    return redirect(url_for("admin.index"))


@admin_bp.route("/users/<int:user_id>/files")
@login_required
@admin_required
def user_files(user_id):
    user = User.query.get_or_404(user_id)
    folders = Folder.query.filter_by(user_id=user_id, parent_id=None).order_by(
        Folder.name
    ).all()
    files = File.query.filter_by(user_id=user_id, folder_id=None).order_by(
        File.name
    ).all()
    return render_template(
        "admin/user_files.html", target_user=user, folders=folders, files=files
    )


@admin_bp.route("/files/<int:file_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_file(file_id):
    file = File.query.get_or_404(file_id)
    user_id = file.user_id

    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)
    if os.path.exists(full_path):
        os.remove(full_path)

    for link in file.share_links:
        db.session.delete(link)
    db.session.delete(file)
    db.session.commit()

    flash(t("delete") + " ✓", "success")
    return redirect(url_for("admin.user_files", user_id=user_id))


@admin_bp.route("/users/<int:user_id>/projects")
@login_required
@admin_required
def user_projects(user_id):
    user = User.query.get_or_404(user_id)
    projects = (
        Project.query.filter_by(user_id=user_id)
        .order_by(Project.created_at.desc())
        .all()
    )
    return render_template(
        "admin/user_projects.html", target_user=user, projects=projects
    )


@admin_bp.route("/projects/<int:project_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_project(project_id):
    project = Project.query.get_or_404(project_id)
    user_id = project.user_id

    # Remove disk files first; if the commit then fails, the row remains and
    # the admin can retry (commit-then-rmtree would leak an orphaned dir).
    disk_dir = os.path.join(
        current_app.config["UPLOAD_FOLDER"],
        str(user_id),
        "projects",
        str(project.id),
    )
    if os.path.isdir(disk_dir):
        shutil.rmtree(disk_dir, ignore_errors=True)

    # Cascade removes ProjectFile + ProjectShareLink rows.
    db.session.delete(project)
    db.session.commit()

    flash(t("delete") + " ✓", "success")
    return redirect(url_for("admin.user_projects", user_id=user_id))
