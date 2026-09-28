FROM python:3.13-slim

# Release version, injected by CI (.github/workflows/mcp-build.yml) the same way
# the main image gets it. Baked into the runtime environment so the value the
# MCP handshake reports matches the tag the image carries. A plain local build
# leaves it empty and the package reports 0.0.0-dev.
ARG APP_VERSION=""

WORKDIR /opt/htmlhost-mcp

# Everything the wheel needs is copied first so one layer holds the install and
# a source edit does not re-resolve dependencies. Nothing here compiles: the
# whole dependency tree is wheels, so no toolchain is installed.
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --no-cache-dir .

# Non-root, matching the main image's convention. Port 8000 is above 1024, so
# an unprivileged user can bind it.
RUN groupadd -g 1000 mcpserver && \
    useradd -u 1000 -g mcpserver -M -s /usr/sbin/nologin mcpserver
USER 1000

# A container is deployed to serve the shared HTTP endpoint, so that is the
# default here. stdio is a local install (uvx / pipx), not a container.
#
# MCP_HOST=0.0.0.0 matters: a container's loopback is not the host's, so a
# published port cannot reach a server bound to 127.0.0.1. Getting this wrong
# shows up as a 502 from whatever proxies to this container, with nothing in
# this container's log to explain it.
ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    APP_VERSION="${APP_VERSION}"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import socket; socket.create_connection(('127.0.0.1', 8000), 3).close()" || exit 1

CMD ["htmlhost-mcp"]
