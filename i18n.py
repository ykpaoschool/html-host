import json
import os

from flask import current_app, request, session
from jinja2 import pass_context


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


@pass_context
def t_filter(ctx, key):
    # pass_context exists for its side effect: Jinja's optimizer constant-folds
    # `{{ 'literal' | t }}` at compile time and bakes the translated string into
    # the compiled template, freezing the UI language per worker process.
    # nodes.Filter.as_const refuses to fold filters that take a context.
    return t(key)
