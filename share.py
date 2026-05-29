import os

from flask import Blueprint, current_app, make_response, render_template

from i18n import t
from models import ShareLink

share_bp = Blueprint("share", __name__)


@share_bp.route("/s/<token>")
def view(token):
    link = ShareLink.query.filter_by(token=token).first()
    if not link or not link.is_active or link.is_expired():
        return render_template("share/not_found.html"), 404

    file = link.file
    full_path = os.path.join(current_app.config["UPLOAD_FOLDER"], file.storage_path)
    if not os.path.exists(full_path):
        return render_template("share/not_found.html", reason="file_moved"), 404

    with open(full_path, "r", encoding="utf-8") as f:
        content = f.read()

    user = file.user
    uploaded_by = t("share_uploaded_by", name=user.display_name)
    return render_template("share/view.html", file=file, content=content, link=link, uploaded_by=uploaded_by)
