import bcrypt
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import login_required, login_user, logout_user

from i18n import t
from models import User, db

auth_bp = Blueprint("auth", __name__)


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


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))
