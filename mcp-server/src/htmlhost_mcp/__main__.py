"""Console entry point: ``htmlhost-mcp [--transport stdio|http]``."""

from __future__ import annotations

import argparse
import logging
import os
import sys

from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .config import HTTP, MAX_REQUEST_BODY_SIZE, Config, ConfigError
from .tools import build_server

LOGGER_NAME = "htmlhost_mcp"


def _parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="htmlhost-mcp",
        description=(
            "MCP server for HTMLHost. Publishes HTML and manages share links "
            "on behalf of the caller."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default=None,
        help="Transport to serve. Overrides the MCP_TRANSPORT environment variable.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser.parse_args(argv)


def _in_container():
    return os.path.exists("/.dockerenv")


def main(argv=None):
    args = _parse_args(argv)

    # Logs go to stderr, always. In stdio mode stdout *is* the JSON-RPC channel:
    # a single stray line written to it corrupts the protocol stream, and the
    # client reports only a parse error.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logger = logging.getLogger(LOGGER_NAME)

    try:
        config = Config.from_env(transport=args.transport)
    except ConfigError as exc:
        logger.error("configuration error: %s", exc)
        return 2

    server = build_server(config)
    logger.info("htmlhost-mcp %s: %s", __version__, config.describe())

    if config.transport != HTTP:
        server.run(transport="stdio")
        return 0

    if _in_container() and config.host in ("127.0.0.1", "localhost", "::1"):
        # The container's loopback is not the host's, so a published port
        # cannot reach a server bound this way. The symptom is a gateway error
        # from whatever proxies to it, with nothing in this log to explain it.
        logger.warning(
            "MCP_HOST is %s inside a container: nothing outside the container "
            "can connect. Set MCP_HOST=0.0.0.0 so a published port works.",
            config.host,
        )
    logger.info(
        "listening on %s:%s/mcp; allowed Host headers: %s (from %s)",
        config.host,
        config.port,
        ", ".join(config.allowed_hosts),
        config.allowed_hosts_source,
    )

    server.run(
        transport="streamable-http",
        host=config.host,
        port=config.port,
        streamable_http_path="/mcp",
        # The SDK's own default (4 MiB) is smaller than a worst-case 3 MiB
        # publish once JSON escaping has doubled it, and it rejects the body
        # inside the transport, before any tool sees it. See config.py.
        max_request_body_size=MAX_REQUEST_BODY_SIZE,
        # One endpoint serves every user of the gateway. Stateless operation
        # removes the possibility of one user's session state being seen by
        # another, and nothing here needs server push.
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            # Rejects requests whose Host header is not one we serve. A wrong
            # allowlist fails closed and silently - the log says only "Invalid
            # Host header" - so the effective list is logged above.
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(config.allowed_hosts),
        ),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
