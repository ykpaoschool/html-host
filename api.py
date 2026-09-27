"""The /api/v1 plane: a JSON REST API for MCP servers, CI and scripts.

Registered in app.py with ``url_prefix="/api/v1"``. Every request is resolved
to a user before the view runs and the view reads ``g.api_user``.

Auth modes (see requirements §4.1):

1. **Personal Access Token** — ``Authorization: Bearer hh_...`` (this PR).
2. **Trusted headers** — ``X-HtmlHost-Key`` + ``X-HtmlHost-User`` (PR 3), used
   by open-webui which injects ``{{USER_EMAIL}}`` server-side.

Both are gated by ``User.mcp_enabled``: a single rule for the whole plane.
"""

import logging
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, request

from models import ApiToken, User, db, hash_api_token, is_well_formed_api_token

api_bp = Blueprint("api", __name__)

logger = logging.getLogger(__name__)


class ApiAuthError(Exception):
    """Raised by the auth layer; rendered as a uniform JSON error body."""

    def __init__(self, code, message, status=401):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@api_bp.before_request
def authenticate():
    try:
        g.api_user = _resolve_user(request.headers)
    except ApiAuthError as exc:
        logger.warning("API auth rejected: %s (%s)", exc.code, request.path)
        return jsonify({"error": exc.code, "message": exc.message}), exc.status
    return None


def _resolve_user(headers):
    """Resolve the request's user, or raise ApiAuthError."""
    auth_header = headers.get("Authorization", "")
    if auth_header:
        return _resolve_pat(auth_header)

    raise ApiAuthError(
        "UNAUTHORIZED",
        "Authentication required. Send 'Authorization: Bearer <api-token>'.",
    )


def _resolve_pat(auth_header):
    scheme, _, token = auth_header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise ApiAuthError(
            "UNAUTHORIZED",
            "Malformed Authorization header; expected 'Bearer <api-token>'.",
        )
    # Reject malformed tokens on shape/checksum before touching the database.
    if not is_well_formed_api_token(token):
        raise ApiAuthError("UNAUTHORIZED", "Invalid API token.")

    row = ApiToken.query.filter_by(token_hash=hash_api_token(token)).first()
    if row is None or not row.is_active:
        raise ApiAuthError("UNAUTHORIZED", "Invalid or revoked API token.")
    if row.is_expired():
        raise ApiAuthError("UNAUTHORIZED", "This API token has expired.")

    user = db.session.get(User, row.user_id)
    if user is None:
        raise ApiAuthError("UNAUTHORIZED", "Invalid or revoked API token.")
    if not user.mcp_enabled:
        raise ApiAuthError(
            "MCP_DISABLED",
            "API access is disabled for this account. "
            "Ask an administrator to enable it.",
            status=403,
        )

    row.last_used_at = datetime.now(timezone.utc)
    db.session.commit()

    logger.info(
        "API auth ok: user_id=%s token_prefix=%s path=%s",
        user.id,
        row.token_prefix,
        request.path,
    )
    return user


@api_bp.route("/me")
def me():
    """Identity of the authenticated user, for debugging agent bindings."""
    user = g.api_user
    return jsonify(
        {
            "id": user.id,
            "email": user.email,
            "display_name": user.display_name,
            "is_admin": bool(user.is_admin),
            "mcp_enabled": bool(user.mcp_enabled),
        }
    )
