"""Turning a failed HTMLHost call into an error the model can act on.

The API plane answers every failure with ``{"error": <CODE>, "message": <str>}``
(api.py), and some of those messages carry an actionable URL - most importantly
USER_NOT_REGISTERED, whose message tells the user to sign in once so their
account gets created. Both the code and the message are therefore preserved
exactly, and only the wrapping changes.
"""

from __future__ import annotations


class HtmlHostError(Exception):
    """A failed call to HTMLHost.

    ``code`` and ``message`` are HTMLHost's own, when it produced them; the
    synthetic codes below (UPSTREAM_*, INVALID_RESPONSE) are added by this
    client for failures that never reached the API's error handling.
    """

    def __init__(self, code, message, status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def __str__(self):
        return f"[{self.code}] {self.message}"


def from_response(response):
    """Build an HtmlHostError from a non-2xx response.

    Only the API's JSON error shape is relayed. Anything else - an HTML error
    page injected by a proxy, a truncated body, a login redirect's markup - is
    reduced to a status-only message: raw HTML is unreadable to the model and
    an obvious prompt-injection surface, which is exactly why the API plane
    renders its own 404/405/413 as JSON in the first place.
    """
    payload = None
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        code = payload.get("error")
        message = payload.get("message")
        if isinstance(code, str) and isinstance(message, str) and message:
            return HtmlHostError(code, message, response.status_code)

    detail = _STATUS_HINTS.get(response.status_code, "")
    if not detail:
        detail = (
            "The response body was not HTMLHost's JSON error object, so it was "
            "discarded rather than shown. This usually means something between "
            "this server and HTMLHost (a reverse proxy, a VPN portal) answered "
            "instead of the API."
        )
    return HtmlHostError(
        f"HTTP_{response.status_code}",
        f"HTMLHost returned HTTP {response.status_code}. {detail}",
        response.status_code,
    )


def from_transport_error(exc):
    """Build an HtmlHostError from a connection-level failure."""
    return HtmlHostError(
        "UPSTREAM_UNREACHABLE",
        f"Could not reach HTMLHost at the configured address: {exc}. "
        "Check HTMLHOST_URL, that the service is running, and - if it uses a "
        "certificate from a private CA - that HTMLHOST_CA_BUNDLE points at "
        "that CA's certificate.",
    )


def from_invalid_response(response):
    """A 2xx/3xx whose body is not the JSON this client expects."""
    return HtmlHostError(
        "INVALID_RESPONSE",
        f"HTMLHost returned HTTP {response.status_code} with a body that is not "
        "JSON. If a reverse proxy or SSO portal is in front of HTMLHost, it is "
        "intercepting API requests before they reach the service.",
        response.status_code,
    )


# Codes the API never sends: generated here, for failures that happened before
# or outside its error handling.
_STATUS_HINTS = {
    401: (
        "The request reached HTMLHost but its credentials were rejected - but "
        "not in the API's usual JSON form."
    ),
    403: "The request reached HTMLHost but was refused.",
    404: (
        "No such API route. This usually means HTMLHOST_URL points at "
        "something other than the HTMLHost service, or does not include a path "
        "prefix the service is mounted under."
    ),
    413: (
        "The request body was rejected before the API could read it. Content "
        "over 3 MiB must be published through the HTMLHost web UI."
    ),
    502: (
        "The proxy in front of HTMLHost could not reach it. Check that the "
        "HTMLHost service is running."
    ),
    503: "HTMLHost is temporarily unavailable.",
    504: "The proxy in front of HTMLHost timed out waiting for it.",
}


__all__ = [
    "HtmlHostError",
    "from_response",
    "from_transport_error",
    "from_invalid_response",
]
