import json
import os

from flask import current_app, request, session


_translations = {}


def load_translations(app):
    trans_dir = os.path.join(app.root_path, "translations")
    for lang in app.config["LANGUAGES"]:
        path = os.path.join(trans_dir, f"{lang}.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                _translations[lang] = json.load(f)


def get_language():
    return session.get("lang", current_app.config["DEFAULT_LANGUAGE"])


def t(key, **kwargs):
    lang = get_language()
    value = _translations.get(lang, {}).get(key, key)
    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError):
            return value
    return value


def t_filter(key):
    return t(key)
