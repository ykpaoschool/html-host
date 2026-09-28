"""HTTP client for HTMLHost's /api/v1 plane.

This server is a *stateless proxy*: it parses no credentials, holds no user
data and keeps no per-user state. Each call builds its auth headers from the
request being served and hands them straight to HTMLHost, which is the only
place that decides who the caller is (implementation plan §6.1).

The consequence that shapes this module: the client object may be shared, but
**headers must never be**. A cached header set would make one user's calls
arrive as another user once more than one person uses the endpoint.
"""

from __future__ import annotations

import logging

import httpx2

from . import __version__
from .errors import (
    HtmlHostError,
    from_invalid_response,
    from_response,
    from_transport_error,
)

logger = logging.getLogger(__name__)

# The only headers forwarded upstream, in their canonical spelling. A whitelist
# rather than "pass everything through": request headers such as Host or
# Content-Length describe the *inbound* connection and would corrupt the
# outbound one, and nothing else the client sends is any of HTMLHost's
# business.
_CANONICAL_HEADERS = {
    "authorization": "Authorization",
    "x-htmlhost-key": "X-HtmlHost-Key",
    "x-htmlhost-user": "X-HtmlHost-User",
}


def _transport_headers(ctx):
    """The inbound request's headers, or None when the transport has none."""
    if ctx is None:
        return None
    try:
        headers = ctx.headers
    except (AttributeError, ValueError):
        # stdio carries no HTTP headers, and Context raises rather than
        # returning None when there is no request context at all.
        return None
    return dict(headers) if headers else None


def resolve_auth(ctx, config):
    """The headers that authenticate one call.

    Precedence, and why:

    1. **The caller's own headers** (HTTP mode) - open-webui injects the acting
       user's email server-side, so this is the identity the request is
       actually about.
    2. **HTMLHOST_PAT** (stdio mode, which has no headers) - a single-user
       agent's own token.

    The caller's headers win over a configured PAT on purpose: a shared token
    falling back into place would silently make every user act as one account.

    Headers with an empty value are dropped. They cannot authenticate anything,
    and an empty ``Authorization`` is a known shape in this ecosystem -
    open-webui sends ``Authorization: Bearer `` whenever its token field is
    left blank. HTMLHost tolerates that case itself and falls through to the
    trusted headers (api.py::_resolve_user), so nothing is lost by not
    relaying an empty value.
    """
    headers = _transport_headers(ctx)
    if headers:
        forwarded = {
            _CANONICAL_HEADERS[name.lower()]: value
            for name, value in headers.items()
            if name.lower() in _CANONICAL_HEADERS and value and value.strip()
        }
        if forwarded:
            return forwarded

    if config.htmlhost_pat:
        return {"Authorization": f"Bearer {config.htmlhost_pat}"}

    if headers is not None:
        raise HtmlHostError(
            "UNAUTHORIZED",
            "This request carried no credentials. Send either "
            "'Authorization: Bearer <api-token>' or both 'X-HtmlHost-Key' and "
            "'X-HtmlHost-User'.",
        )
    raise HtmlHostError(
        "UNAUTHORIZED",
        "No credentials available: running over stdio, which carries no "
        "headers, and HTMLHOST_PAT is not set. Create an API token in HTMLHost "
        "under Settings > API tokens and start this server with HTMLHOST_PAT.",
    )


class HtmlHostClient:
    """Async client for the /api/v1 plane."""

    def __init__(self, config):
        self._config = config
        # Absolute URLs are built per call instead of using httpx's base_url.
        # base_url merges paths by concatenation, so a path that happens to
        # start with "/" can silently replace the /api/v1 prefix - an easy way
        # to request the wrong endpoint.
        self._api_base = f"{config.htmlhost_url}/api/v1"
        self._client = httpx2.AsyncClient(
            timeout=config.timeout,
            # A private CA is supported through HTMLHOST_CA_BUNDLE. There is
            # deliberately no way to skip verification.
            verify=config.ca_bundle or True,
            follow_redirects=False,
            headers={"User-Agent": f"htmlhost-mcp/{__version__}"},
        )

    async def aclose(self):
        await self._client.aclose()

    async def request(self, method, path, *, auth, json=None, params=None):
        """Call the API, returning the decoded JSON body.

        Raises :class:`HtmlHostError` for anything that is not a 2xx with a
        JSON body.
        """
        url = f"{self._api_base}{path}"
        try:
            response = await self._client.request(
                method, url, headers=auth, json=json, params=params
            )
        except httpx2.HTTPError as exc:
            logger.warning("%s %s failed: %s", method, path, exc)
            raise from_transport_error(exc) from exc

        logger.info("%s %s -> %s", method, path, response.status_code)

        # 3xx counts as a failure: this client does not follow redirects, so
        # one means something other than the API answered (a login redirect or
        # an intercepting proxy), and reporting it as success would hand the
        # model a body it cannot use.
        if response.status_code >= 300:
            raise from_response(response)

        try:
            return response.json()
        except ValueError as exc:
            raise from_invalid_response(response) from exc
