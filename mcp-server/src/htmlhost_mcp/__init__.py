"""HTMLHost MCP server: a stateless proxy in front of HTMLHost's /api/v1 plane.

This package parses no credentials and stores no user data. It translates MCP
tool calls into HTTP calls against HTMLHost, forwarding the caller's auth
headers unchanged, and lets HTMLHost decide who the caller is.
"""

import os

# Reported in the MCP handshake (serverInfo.version). CI injects APP_VERSION as
# a Docker build arg, the same way the main app is versioned, so both images
# carry the same release string. A plain local build has no base version to
# read - the repository's VERSION file describes the HTMLHost app, not this
# package - so it reports an obviously-unreleased value instead.
__version__ = os.environ.get("APP_VERSION", "").strip() or "0.0.0-dev"
