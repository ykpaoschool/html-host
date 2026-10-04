import os

from flask import (
    Blueprint,
    abort,
    current_app,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_login import current_user

from i18n import t
from models import ShareLink

share_bp = Blueprint("share", __name__)


@share_bp.route("/s/<token>")
def view(token):
    link = ShareLink.query.filter_by(token=token).first()
    if not link or not link.is_active or link.is_expired():
        return render_template("share/not_found.html"), 404

    if link.require_login and not current_user.is_authenticated:
        # Relative path, not request.url: auth._safe_next_url rejects
        # absolute URLs, which is what request.url would hand it.
        return redirect(url_for("auth.login", next=request.path))

    file = link.file
    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)
    if not os.path.exists(full_path):
        return render_template("share/not_found.html", reason="file_moved"), 404

    # The iframe loads the real file URL, not an inlined srcdoc. A srcdoc
    # document's base URL is the parent page's, so an in-page "#anchor" link
    # inside it resolves to /s/<token>#anchor and navigates the frame there —
    # into the viewer page, whose X-Frame-Options: DENY blocks it.
    raw_url = url_for("share.raw_file", token=token)

    user = file.user
    uploaded_by = t("share_uploaded_by", name=user.display_name)
    return render_template(
        "share/view.html", file=file, raw_url=raw_url, link=link, uploaded_by=uploaded_by
    )


@share_bp.route("/s/<token>/raw")
def raw_file(token):
    """Serve the shared file itself, for the viewer's iframe to load.

    Same posture as projects.raw_file: the response carries the uploaded HTML
    under the app's own origin, so the framing headers must be relaxed just
    enough for the viewer's iframe — and a CSP sandbox must keep the document
    in an opaque origin whether it is framed or opened directly.
    """
    link = ShareLink.query.filter_by(token=token).first()
    if not link or not link.is_active or link.is_expired():
        abort(404)

    if link.require_login and not current_user.is_authenticated:
        abort(401)

    file = link.file
    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)
    if not os.path.exists(full_path):
        abort(404)

    response = send_file(full_path)
    response.headers["X-Content-Type-Options"] = "nosniff"
    # Direct assignment: the app-level hook already ran with DENY.
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    # frame-ancestors: only this app may frame the content, so no other site
    # can frame it either. sandbox: this URL is reachable on its own, and
    # without it the uploaded HTML would run as a first-party document on this
    # origin, with the viewer's session cookie within reach. The frame's own
    # sandbox attribute and this directive are intersected, so the allowances
    # here must match templates/share/view.html or they are silently dropped.
    response.headers["Content-Security-Policy"] = (
        "frame-ancestors 'self'; sandbox allow-scripts allow-popups"
    )
    return response


@share_bp.route("/s/<token>/download")
def download(token):
    link = ShareLink.query.filter_by(token=token).first()
    if not link or not link.is_active or link.is_expired():
        return render_template("share/not_found.html"), 404

    if link.require_login and not current_user.is_authenticated:
        abort(401)

    file = link.file
    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)
    if not os.path.exists(full_path):
        return render_template("share/not_found.html", reason="file_moved"), 404

    return send_file(full_path, as_attachment=True, download_name=file.name)
