"""Release version resolution.

The ``VERSION`` file in the repository root is the single hand-maintained
source of truth: it holds the semantic base version (``0.1.0``) and is bumped
by hand in the pull request that warrants it.

Everything else is derived. The CI workflow computes a unique, immutable
build version for every merge to main —

    0.1.0-build.42.sha.abc1234

and injects it as the ``APP_VERSION`` environment variable (via a Docker
build arg). That exact string is both what the UI displays and what the
image is tagged with, so the version a user reads on screen can be pasted
straight into ``docker pull``. Separators are "-" and "." because Docker
tags allow only ``[A-Za-z0-9_.-]`` — semver's "+" is not usable there.

Outside CI (local ``run.sh``, a plain ``docker build``) there is no
``APP_VERSION``, so the base version is reported with a ``-dev`` suffix to
make it obvious that the running code is not a released build.
"""

import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
VERSION_FILE = os.path.join(BASE_DIR, "VERSION")

FALLBACK_VERSION = "0.0.0"
DEV_SUFFIX = "-dev"


def base_version():
    """The semantic base version from the VERSION file (``0.1.0``)."""
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as f:
            version = f.read().strip()
    except OSError:
        return FALLBACK_VERSION
    # Tolerate a "v" prefix so `git describe`-style tags can be pasted in.
    return version.lstrip("v") or FALLBACK_VERSION


def full_version():
    """The version to display and to tag images with.

    ``APP_VERSION`` wins when set (CI builds); an empty value counts as
    unset, which is what an un-parameterised docker build produces.
    """
    injected = os.environ.get("APP_VERSION", "").strip()
    if injected:
        return injected
    return f"{base_version()}{DEV_SUFFIX}"
