import logging

import bcrypt
from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user

from i18n import t
from models import User, db

auth_bp = Blueprint("auth", __name__)

logger = logging.getLogger(__name__)


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    # If no users exist, redirect to setup
    if User.query.count() == 0:
        return redirect(url_for("auth.setup"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()
        if user and user.auth_provider != "local":
            flash(t("login_sso_only"), "error")
        elif user and user.password_hash and bcrypt.checkpw(
            password.encode("utf-8"), user.password_hash.encode("utf-8")
        ):
            login_user(user)
            next_page = request.args.get("next", url_for("dashboard.index"))
            return redirect(next_page)
        else:
            flash(t("login_error"), "error")
    return render_template("login.html")


@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    # Only allow setup if no users exist
    if User.query.count() > 0:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        display_name = request.form.get("display_name", "").strip()

        if not email or not password or not display_name:
            flash("All fields are required", "error")
            return redirect(url_for("auth.setup"))

        password_hash = bcrypt.hashpw(
            password.encode("utf-8"), bcrypt.gensalt()
        ).decode("utf-8")

        user = User(
            email=email,
            password_hash=password_hash,
            auth_provider="local",
            display_name=display_name,
            is_admin=True,
        )
        db.session.add(user)
        db.session.commit()

        login_user(user)
        return redirect(url_for("dashboard.index"))

    return render_template("setup.html")


@auth_bp.route("/login/microsoft")
def login_microsoft():
    if not current_app.config.get("MICROSOFT_CLIENT_ID"):
        flash(t("sso_error"), "error")
        return redirect(url_for("auth.login"))
    redirect_uri = url_for("auth.login_microsoft_callback", _external=True)
    return current_app.oauth.microsoft.authorize_redirect(redirect_uri)


@auth_bp.route("/login/microsoft/callback")
def login_microsoft_callback():
    try:
        token = current_app.oauth.microsoft.authorize_access_token()
        resp = current_app.oauth.microsoft.get(
            "https://graph.microsoft.com/v1.0/me"
        )
        userinfo = resp.json()
    except Exception:
        logger.exception("SSO callback failed")
        flash(t("sso_error"), "error")
        return redirect(url_for("auth.login"))

    email = userinfo.get("mail") or userinfo.get("userPrincipalName", "")
    display_name = userinfo.get("displayName", email)

    if not email:
        flash(t("sso_error"), "error")
        return redirect(url_for("auth.login"))

    email = email.strip().lower()

    # Check if a local account already uses this email
    local_user = User.query.filter_by(email=email, auth_provider="local").first()
    if local_user:
        flash(t("sso_email_conflict"), "error")
        return redirect(url_for("auth.login"))

    # Find or create SSO user
    user = User.query.filter_by(email=email, auth_provider="microsoft").first()
    if not user:
        user = User(
            email=email,
            password_hash=None,
            auth_provider="microsoft",
            display_name=display_name,
            is_admin=False,
        )
        db.session.add(user)
        db.session.commit()

    login_user(user)
    return redirect(url_for("dashboard.index"))


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
