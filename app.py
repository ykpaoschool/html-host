import logging
import os

from flask import Flask, redirect, url_for
from flask_login import LoginManager, current_user
from sqlalchemy import inspect, text

from config import Config
from i18n import get_language, load_translations, t_filter
from models import User, db

logger = logging.getLogger(__name__)

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
        _migrate_schema(db)

    @app.context_processor
    def inject_lang():
        return {"lang": get_language()}

    return app


def _migrate_schema(db):
    """Auto-migrate schema: add missing columns and relax nullable constraints.

    Handles the case where db.create_all() only creates new tables
    but cannot alter existing ones (e.g. adding columns, changing nullable
    after deployment).

    SQLite does not support ALTER COLUMN, so nullable relaxations use
    the table-rebuild pattern (rename → create → copy → drop → rename).
    """
    with db.engine.connect() as conn:
        for table_name in db.metadata.tables:
            db_cols = {c["name"]: c for c in inspect(db.engine).get_columns(table_name)}
            model = db.metadata.tables[table_name]

            # 1. Add missing columns
            for col in model.columns:
                if col.name not in db_cols:
                    col_type = col.type.compile(db.engine.dialect)
                    default = ""
                    if col.default is not None and col.default.is_scalar:
                        default = f" DEFAULT {col.default.arg!r}"
                    nullable = "" if col.nullable else " NOT NULL"
                    sql = f'ALTER TABLE "{table_name}" ADD COLUMN {col.name} {col_type}{default}{nullable}'
                    conn.execute(text(sql))
                    conn.commit()
                    logger.info("Auto-migrated: %s", sql)

            # 2. Relax nullable: model says nullable but DB says NOT NULL
            nullable_to_relax = []
            for col in model.columns:
                if col.name in db_cols and col.nullable and not db_cols[col.name]["nullable"]:
                    nullable_to_relax.append(col.name)
            if nullable_to_relax:
                _rebuild_table(conn, db, table_name, model, nullable_to_relax)
                logger.info(
                    "Relaxed nullable on %s.%s", table_name, nullable_to_relax
                )


def _rebuild_table(conn, db, table_name, model, nullable_cols):
    """Rebuild a SQLite table to relax NOT NULL constraints.

    Uses the standard SQLite table-rebuild pattern:
    rename old → create new → copy data → drop old → rename new
    """
    tmp_name = f"_old_{table_name}"

    # Build column definitions from model
    col_defs = []
    for col in model.columns:
        col_type = col.type.compile(db.engine.dialect)
        default = ""
        if col.default is not None and col.default.is_scalar:
            default = f" DEFAULT {col.default.arg!r}"
        nullable = "" if col.nullable else " NOT NULL"
        pk = " PRIMARY KEY" if col.primary_key else ""
        col_defs.append(f"{col.name} {col_type}{pk}{default}{nullable}")

    col_names = ", ".join(c.name for c in model.columns)

    conn.execute(text(f'ALTER TABLE "{table_name}" RENAME TO "{tmp_name}"'))
    conn.execute(
        text(f'CREATE TABLE "{table_name}" ({", ".join(col_defs)})')
    )
    conn.execute(
        text(f'INSERT INTO "{table_name}" ({col_names}) SELECT {col_names} FROM "{tmp_name}"')
    )
    conn.execute(text(f'DROP TABLE "{tmp_name}"'))
    conn.commit()


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5001)
