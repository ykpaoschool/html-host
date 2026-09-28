import json
import logging
import os
import shutil

import bcrypt
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

# The API plane owns these settings; the page only edits them, so both sides
# agree on one key name and one header spelling.
from api import (
    MCP_KEY_HEADER,
    MCP_SECRET_KEY,
    MCP_USER_HEADER,
    PUBLIC_BASE_URL_KEY,
    get_app_setting,
    get_mcp_secret_hash,
    get_public_base_url,
    set_app_setting,
)
from i18n import t
from models import AppSetting, File, Folder, Project, ShareLink, User, db, generate_mcp_secret

admin_bp = Blueprint("admin", __name__)

logger = logging.getLogger(__name__)


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
        user.mcp_enabled = request.form.get("mcp_enabled") == "on"

        new_password = request.form.get("password", "").strip()
        if new_password and user.auth_provider == "local":
            user.password_hash = bcrypt.hashpw(
                new_password.encode("utf-8"), bcrypt.gensalt()
            ).decode("utf-8")

        db.session.commit()
        flash(t("user_updated"), "success")
        return redirect(url_for("admin.index"))

    return render_template("admin/edit_user.html", edit_user=user)


@admin_bp.route("/users/<int:user_id>/toggle-mcp", methods=["POST"])
@login_required
@admin_required
def toggle_mcp(user_id):
    """Quick enable/disable of a user's API/MCP access, from the user list."""
    user = User.query.get_or_404(user_id)
    user.mcp_enabled = not user.mcp_enabled
    db.session.commit()

    status = t("active") if user.mcp_enabled else t("inactive")
    flash(f"{user.display_name}: {t('mcp_enabled')} → {status}", "success")
    return redirect(url_for("admin.index"))


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


# --- MCP integration (/admin/mcp) ------------------------------------------
#
# Two things live here, both consumed by the /api/v1 plane: the outward-facing
# address that share links are built from (api.get_public_base_url) and the
# shared secret that authenticates open-webui (api._resolve_trusted_header).

# Placeholder for the copyable config whenever the plaintext is not in hand -
# it is stored hashed, so it is only ever shown on the response that minted it.
SECRET_PLACEHOLDER = "<shared-secret>"


@admin_bp.route("/mcp")
@login_required
@admin_required
def mcp_settings():
    return _render_mcp_settings()


@admin_bp.route("/mcp/base-url", methods=["POST"])
@login_required
@admin_required
def save_mcp_base_url():
    """Set the outward-facing address that API links are built from."""
    # Normalise before validating: a bare "https://" would otherwise pass the
    # scheme check and then be stored as "https:" once the trailing slash goes.
    value = request.form.get("public_base_url", "").strip().rstrip("/")
    if value and not value.lower().startswith("https://"):
        flash(t("public_base_url_must_be_https"), "error")
        return redirect(url_for("admin.mcp_settings"))

    set_app_setting(PUBLIC_BASE_URL_KEY, value)
    db.session.commit()
    logger.info("Public base URL set to %r by user_id=%s", value, current_user.id)

    flash(t("public_base_url_saved"), "success")
    return redirect(url_for("admin.mcp_settings"))


@admin_bp.route("/mcp/secret", methods=["POST"])
@login_required
@admin_required
def rotate_mcp_secret():
    """Generate a shared secret, replacing any existing one."""
    plaintext, secret_hash = generate_mcp_secret()
    set_app_setting(MCP_SECRET_KEY, secret_hash)
    db.session.commit()
    logger.info("MCP shared secret rotated by user_id=%s", current_user.id)

    # Render directly rather than redirecting: this is the one and only time the
    # plaintext exists, and flashing it would put the secret in the session
    # cookie (signed, not encrypted). Same reasoning as auth.create_api_token.
    return _render_mcp_settings(new_secret=plaintext)


@admin_bp.route("/mcp/secret/revoke", methods=["POST"])
@login_required
@admin_required
def revoke_mcp_secret():
    row = db.session.get(AppSetting, MCP_SECRET_KEY)
    if row is not None:
        db.session.delete(row)
        db.session.commit()
        logger.info("MCP shared secret revoked by user_id=%s", current_user.id)

    flash(t("mcp_secret_revoked"), "success")
    return redirect(url_for("admin.mcp_settings"))


def _render_mcp_settings(new_secret=None):
    base_url = get_public_base_url()
    secret_set = get_mcp_secret_hash() is not None

    headers = {
        MCP_KEY_HEADER: new_secret or SECRET_PLACEHOLDER,
        # open-webui substitutes this per request; the app never sees the
        # literal braces.
        MCP_USER_HEADER: "{{USER_EMAIL}}",
    }

    return render_template(
        "admin/mcp.html",
        # What is stored vs. what is in effect: the form must show the stored
        # value, or saving once would silently copy the env fallback into the DB.
        stored_base_url=get_app_setting(PUBLIC_BASE_URL_KEY) or "",
        env_base_url=current_app.config.get("PUBLIC_BASE_URL") or "",
        mcp_url=f"{base_url}/mcp" if base_url else "",
        secret_set=secret_set,
        new_secret=new_secret,
        headers_json=json.dumps(headers, indent=2),
        enabled_user_count=User.query.filter_by(mcp_enabled=True).count(),
        total_user_count=User.query.count(),
    )
