import os

from flask import Flask, redirect, url_for
from flask_login import LoginManager, current_user

from config import Config
from i18n import get_language, load_translations, t_filter
from models import User, db

login_manager = LoginManager()
login_manager.login_view = "auth.login"


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)

    load_translations(app)

    app.jinja_env.filters["t"] = t_filter
    app.jinja_env.globals["get_language"] = get_language
    app.jinja_env.globals["LANGUAGES"] = app.config["LANGUAGES"]

    from auth import auth_bp
    from dashboard import dashboard_bp
    from share import share_bp
    from admin import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(share_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

    with app.app_context():
        db.create_all()

    @app.context_processor
    def inject_lang():
        return {"lang": get_language()}

    return app


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
