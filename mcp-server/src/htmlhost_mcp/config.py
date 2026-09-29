"""Environment-driven configuration.

The container takes no config file, so this module is the only place that reads
``os.environ``. Everything is validated at startup: a misconfiguration should
stop the process with a readable message, not surface later as a confusing
runtime failure (an unroutable Host header, a silent TLS downgrade).
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

STDIO = "stdio"
HTTP = "http"
TRANSPORTS = (STDIO, HTTP)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
DEFAULT_TIMEOUT = 60.0

#: Content ceiling for one tool call, mirroring the API's own limit
#: (api.py::MAX_API_CONTENT_SIZE). Not configurable: it is a property of the
#: tool contract, and the API enforces the same number regardless.
#:
#: The tools check this *before* sending, so that oversized content always comes
#: back as the API's PAYLOAD_TOO_LARGE message rather than as a transport-level
#: rejection. See MAX_REQUEST_BODY_SIZE for why that distinction matters.
MAX_TOOL_CONTENT_SIZE = 3 * 1024 * 1024

# Streamable HTTP request-body ceiling. The MCP SDK's own default is 4 MiB, and
# it rejects an oversized body inside the transport - before any tool argument
# exists - with an error that says nothing about the real limit. 4 MiB is not
# enough: content is capped at 3 MiB per call (MAX_TOOL_CONTENT_SIZE above), and
# JSON escaping can double that (a document that is almost entirely newlines),
# so a legitimate 3 MiB publish can arrive as a 6 MiB body. 8 MiB leaves ~33%
# headroom over that worst case, which the tool-side check keeps reachable: with
# content bounded at 3 MiB the body cannot exceed ~6 MiB plus its JSON envelope.
# See the requirements' §7 and the implementation plan §0.3.
MAX_REQUEST_BODY_SIZE = 8 * 1024 * 1024

#: Scheme-less names that mean "this machine". Kept as literals because
#: ``ipaddress`` cannot parse them.
_LOOPBACK_NAMES = frozenset({"localhost"})


class ConfigError(Exception):
    """Raised at startup when the environment cannot produce a usable config."""


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _is_loopback(hostname):
    if hostname is None:
        return False
    if hostname.lower() in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _has_explicit_port(entry):
    """Whether ``entry`` already pins a port.

    A bracketed IPv6 literal contains colons of its own, so the presence of ":"
    alone cannot answer this - only a colon after the closing bracket counts.
    """
    if entry.startswith("["):
        return "]:" in entry
    return ":" in entry


def _host_forms(entry):
    """Every Host header value that should match ``entry``.

    The SDK compares the *whole* Host header, port included, and its ``:*``
    wildcard matches only values that actually carry a port. Both forms reach a
    server in practice - nginx's ``$host`` has no port, ``$http_host`` does -
    so a bare hostname has to expand to both. Expanding to ``:*`` alone looks
    equivalent and is not: it accepts ``html.example.com:443`` while refusing
    ``html.example.com``, which is the form a proxy actually sends, and the only
    clue is "Invalid Host header" in the log.
    """
    if not entry or _has_explicit_port(entry):
        return [entry]
    return [entry, f"{entry}:*"]


def _allowed_hosts(htmlhost_url):
    """Host header values the DNS-rebinding check will accept.

    Defaults to the hostname of ``HTMLHOST_URL``, which is correct for the
    recommended deployment where the MCP endpoint lives on the same domain as
    HTMLHost (``https://html.example.com/mcp``). This default exists because a
    wrong allowlist is invisible: every request is refused with a bare
    "Invalid Host header" in the log, which reads like a network problem.
    """
    configured = [entry.strip() for entry in _env("MCP_ALLOWED_HOSTS").split(",")]
    configured = [entry for entry in configured if entry]
    if configured:
        hosts = [form for entry in configured for form in _host_forms(entry)]
        return hosts, "MCP_ALLOWED_HOSTS"

    hostname = urlsplit(htmlhost_url).hostname
    if not hostname:
        return [], "HTMLHOST_URL (no hostname)"
    return _host_forms(hostname), f"HTMLHOST_URL hostname ({hostname})"


@dataclass(frozen=True)
class Config:
    """A validated configuration. Construct with :meth:`from_env`."""

    htmlhost_url: str
    htmlhost_pat: str
    ca_bundle: str
    transport: str
    host: str
    port: int
    allowed_hosts: tuple
    allowed_hosts_source: str
    timeout: float

    @classmethod
    def from_env(cls, transport=None):
        """Build a Config from the environment, or raise :class:`ConfigError`.

        ``transport`` overrides ``MCP_TRANSPORT`` so the CLI flag wins over the
        environment without this module having to know about argv.
        """
        htmlhost_url = _validate_htmlhost_url(_env("HTMLHOST_URL"))

        pat = _env("HTMLHOST_PAT")
        ca_bundle = _env("HTMLHOST_CA_BUNDLE")
        if ca_bundle and not os.path.exists(ca_bundle):
            raise ConfigError(
                f"HTMLHOST_CA_BUNDLE points at {ca_bundle!r}, which does not "
                "exist. Mount the CA certificate into the container."
            )

        transport = (transport or _env("MCP_TRANSPORT") or STDIO).lower()
        if transport not in TRANSPORTS:
            raise ConfigError(
                f"MCP_TRANSPORT must be one of {', '.join(TRANSPORTS)}; "
                f"got {transport!r}."
            )

        host = _env("MCP_HOST") or DEFAULT_HOST
        try:
            port = int(_env("MCP_PORT") or DEFAULT_PORT)
        except ValueError:
            raise ConfigError(f"MCP_PORT must be an integer; got {_env('MCP_PORT')!r}.")

        try:
            timeout = float(_env("HTMLHOST_TIMEOUT") or DEFAULT_TIMEOUT)
        except ValueError:
            raise ConfigError(
                f"HTMLHOST_TIMEOUT must be a number of seconds; "
                f"got {_env('HTMLHOST_TIMEOUT')!r}."
            )

        allowed_hosts, source = _allowed_hosts(htmlhost_url)
        if transport == HTTP and not allowed_hosts:
            raise ConfigError(
                "Cannot determine allowed Host headers for HTTP mode. Set "
                "MCP_ALLOWED_HOSTS to the public hostname (for example "
                "'mcp.example.com'); without it every request is refused by "
                "the DNS-rebinding check."
            )
        # Loopback is always allowed, so a container health check or a local
        # probe can reach the server without knowing the public name. Both
        # forms, for the reason given on _host_forms.
        loopback = ["127.0.0.1", "localhost", "[::1]"]
        allowed_hosts = _dedupe(
            allowed_hosts + [form for entry in loopback for form in _host_forms(entry)]
        )

        if not pat and transport == STDIO:
            logger.warning(
                "HTMLHOST_PAT is not set. In stdio mode there are no request "
                "headers to forward, so there is no credential to send at all "
                "and every tool call will fail with UNAUTHORIZED."
            )

        return cls(
            htmlhost_url=htmlhost_url,
            htmlhost_pat=pat,
            ca_bundle=ca_bundle,
            transport=transport,
            host=host,
            port=port,
            allowed_hosts=tuple(allowed_hosts),
            allowed_hosts_source=source,
            timeout=timeout,
        )

    def describe(self):
        """A one-line summary for the startup log. Never includes the PAT."""
        return (
            f"transport={self.transport} htmlhost={self.htmlhost_url} "
            f"auth={'pat' if self.htmlhost_pat else 'forwarded'}"
        )


def _dedupe(values):
    seen = set()
    ordered = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _validate_htmlhost_url(raw):
    if not raw:
        raise ConfigError(
            "HTMLHOST_URL is required and must be the base address of the "
            "HTMLHost service, for example https://html.example.com"
        )
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(
            f"HTMLHOST_URL must be an absolute http(s) URL; got {raw!r}."
        )
    # Credentials this server forwards (API tokens, the shared secret, user
    # email addresses) must never travel in clear text, so plain HTTP is
    # refused everywhere except the loopback interface, where it cannot leave
    # the machine and is occasionally needed to point at a local dev server.
    # This is a transport restriction, not a certificate-check bypass: TLS
    # verification against HTTPS hosts is never relaxed.
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        raise ConfigError(
            f"HTMLHOST_URL must use https:// (got {raw!r}). The API tokens, "
            "shared secret and user email addresses this server forwards "
            "would travel unencrypted. http:// is accepted only for a "
            "loopback address, for local testing."
        )
    return raw.rstrip("/")
