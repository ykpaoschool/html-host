# htmlhost-mcp

An MCP server that gives an agent a way to publish HTML to HTMLHost and manage
the links it shares.

It is a **stateless thin proxy**. It parses no credentials, stores no user data
and keeps no per-user session state: each call forwards the caller's own auth
headers to HTMLHost's `/api/v1` plane, and HTMLHost makes the only decision
about who the caller is. That is why the same package can be run as one shared
endpoint for a whole organisation or as a single user's local process without
any state to synchronise.

## Contents

- [Install and run](#install-and-run)
- [Configuration](#configuration)
- [Serving over HTTP](#serving-over-http)
- [Reverse proxy](#reverse-proxy)
- [Registering it in open-webui](#registering-it-in-open-webui)
- [Troubleshooting](#troubleshooting)
- [Notes for maintainers](#notes-for-maintainers)

## Install and run

Requires Python 3.11 or newer.

```bash
pip install .          # or: uvx --from . htmlhost-mcp, pipx install .
```

### stdio, for Claude Code and other single-user agents

No network listener, no TLS: the client starts the process and talks over its
standard input and output.

```bash
export HTMLHOST_URL=https://html.example.com
export HTMLHOST_PAT=hh_xxxxxxxx        # Settings > API tokens in the web UI
htmlhost-mcp                           # MCP_TRANSPORT defaults to stdio
```

Claude Code:

```bash
claude mcp add htmlhost \
  --env HTMLHOST_URL=https://html.example.com \
  --env HTMLHOST_PAT=hh_xxxxxxxx \
  -- htmlhost-mcp
```

### HTTP, for a shared endpoint

```bash
export HTMLHOST_URL=https://html.example.com
export MCP_TRANSPORT=http
export MCP_ALLOWED_HOSTS=html.example.com
htmlhost-mcp
```

This mode holds **no credential of its own**. Callers supply one, and HTMLHost
decides whether to trust it. See [Registering it in open-webui](#registering-it-in-open-webui).

## Configuration

Everything comes from the environment; there is no config file.

| Variable | Default | Meaning |
|---|---|---|
| `HTMLHOST_URL` | *required* | Base address of the HTMLHost service. Must be `https://`. |
| `HTMLHOST_PAT` | — | API token, used **only** in stdio mode, which carries no request headers. |
| `HTMLHOST_CA_BUNDLE` | system trust store | Path to a CA certificate, when HTMLHost's certificate is issued by a private CA. There is deliberately **no** option to skip verification. |
| `HTMLHOST_TIMEOUT` | `60` | Seconds to wait for HTMLHost, per call. |
| `MCP_TRANSPORT` | `stdio` (`http` in the image) | `stdio` or `http`. |
| `MCP_HOST` | `127.0.0.1` (`0.0.0.0` in the image) | Interface to bind in HTTP mode. |
| `MCP_PORT` | `8000` | Port to bind in HTTP mode. |
| `MCP_ALLOWED_HOSTS` | the hostname in `HTMLHOST_URL` | Comma-separated `Host` header values to accept. See below. |
| `APP_VERSION` | — | Injected at build time. Reported in the MCP handshake. |

`HTMLHOST_URL` is checked at startup and refused if it is plain `http://` to
anything but a loopback address: API tokens, the shared secret and user email
addresses all travel on that connection. Loopback is exempt so a local
development server can be used.

`MCP_ALLOWED_HOSTS` is the DNS-rebinding allowlist. It defaults to the hostname
of `HTMLHOST_URL`, which is right whenever the MCP endpoint is served on the
same domain as HTMLHost — the recommended layout, and the only case where you
can leave this unset. Because the check compares the whole `Host` header, port
included, a bare hostname is expanded to both `host` and `host:*`, so it matches
whether the proxy sends `Host: html.example.com` (nginx's `$host`) or
`Host: html.example.com:443` (`$http_host`). Set it explicitly only when the two
are on different domains, or when a proxy rewrites `Host`. Whatever you set, the
effective list is printed at startup:

```
allowed Host headers: html.example.com, html.example.com:*, 127.0.0.1, ...
```

## Serving over HTTP

```bash
docker build -t htmlhost-mcp .          # from this directory
docker run -d --name htmlhost-mcp \
  -p 127.0.0.1:8000:8000 \
  -e HTMLHOST_URL=https://html.example.com \
  -e MCP_ALLOWED_HOSTS=html.example.com \
  --restart unless-stopped \
  htmlhost-mcp
```

The image sets `MCP_TRANSPORT=http`, `MCP_HOST=0.0.0.0` and `MCP_PORT=8000`.

Two things worth knowing:

- **`MCP_HOST=0.0.0.0` is required in a container.** A container's loopback is
  not the host's, so a published port cannot reach a server bound to
  `127.0.0.1`. The image sets it for you; the startup log warns if it is ever
  overridden back to loopback from inside a container.
- **`-p 127.0.0.1:8000:8000` publishes on the host's loopback only**, so the
  port is reachable by a proxy on the host and by nothing off the machine. If
  your proxy runs in Docker instead, put this container on the same network and
  drop the port mapping — see the next section.

The endpoint is `POST /mcp` (MCP Streamable HTTP).

## Reverse proxy

TLS terminates in the proxy; this server speaks plain HTTP on a private
interface. Streamable HTTP is a long-lived streaming transport, so a proxy's
defaults will break it in ways that produce no error at either end — the
request connects and then simply never completes.

Whatever the proxy, the `Host` header must reach this server unchanged and
match `MCP_ALLOWED_HOSTS`, and the path must reach it as `/mcp`.

### nginx

Add to the *existing* server block for your HTMLHost domain, so that
`https://html.example.com/mcp` is the endpoint:

```nginx
location /mcp {
    proxy_pass http://127.0.0.1:8000;   # no trailing slash: /mcp must survive
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_set_header Host $host;         # must match MCP_ALLOWED_HOSTS
    proxy_buffering off;                 # the stream must not be buffered
    proxy_cache off;
    proxy_read_timeout 300s;             # the 60s default cuts live calls short
    client_max_body_size 10m;            # see "The 1 MB wall" below
}
```

The `Host` line is required here. In Nginx Proxy Manager it is not — that block
is written for you, and adding a second copy breaks every request; see below.

The trailing slash in `proxy_pass` matters: `proxy_pass http://127.0.0.1:8000/;`
strips `/mcp` and this server then answers 404 to every request.

### Nginx Proxy Manager

NPM's UI has no fields for any of the settings above, so they have to be added
by hand.

1. **Proxy Hosts → your HTMLHost host → Edit → Custom Locations → Add location.**
   - Location: `/mcp`
   - Forward Hostname: `htmlhost-mcp` (the container name, if NPM and this
     container share a Docker network) or `127.0.0.1` if NPM runs on the host
   - Forward Port: `8000`
2. **Open that location's ⚙ Advanced tab** and paste:

   ```nginx
   proxy_http_version 1.1;
   proxy_set_header Connection "";
   proxy_buffering off;
   proxy_cache off;
   proxy_read_timeout 300s;
   client_max_body_size 10m;
   ```

   **Nothing that NPM already writes for this location belongs here**, and that
   includes `proxy_set_header Host $host;`. It is the one line people copy over
   from the nginx snippet above, and it is the one line that breaks the
   deployment: nginx **appends** a repeated header instead of overriding it, so
   the request leaves with two `Host` headers, and uvicorn rejects it with `400`
   and the body `Invalid HTTP request received.` — for every request, `GET`
   included, before any MCP code runs. RFC 7230 §5.4 requires a server to reject
   a request carrying more than one `Host` field, so this server is behaving
   correctly; the proxy is sending an ambiguous request.

   The symptom is easy to misread, because HTMLHost's own pages on the same
   domain keep working (their location sets `Host` once): it looks like an MCP
   fault, and nothing appears in HTMLHost's log.

3. **Save.** NPM reloads nginx itself.

Adding the same block to the host's own *Advanced* tab instead does not work:
that snippet lands in `location /`, which is the HTMLHost backend, not this one.

### The 1 MB wall

nginx's default `client_max_body_size` is **1 MB**, and it applies to the
request body *before* escaping — so an agent publishing a document larger than
roughly 0.9 MB gets an opaque 413 from the proxy and nothing in this server's
log. Every example above sets `client_max_body_size 10m`; if you use a different
proxy, find its equivalent. This is the single most likely reason a publish
fails for no visible reason.

### Checking the endpoint works

The handshake is plain JSON-RPC, so one `curl` covers the whole path — DNS, TLS,
proxy, this server — with no MCP client involved:

```bash
curl -sS -i --max-time 10 -X POST https://html.example.com/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

A healthy endpoint answers `200` with an event stream: `event: message`, then
`data: {...}` naming this server and its version. That SSE framing is the
transport, not a symptom. Ask for both `Accept` types, as an MCP client does —
though `application/json` alone is answered `200` as well, so this header is
rarely the thing that is wrong.

Authentication belongs to HTMLHost, so this call needs none of it: `initialize`
never reaches the API. To exercise the credentials too, keep the headers from
[Registering it in open-webui](#registering-it-in-open-webui) and ask a tool
instead of handshaking — `whoami` is the cheapest one, and it reports the
HTMLHost account the request acts as:

```bash
curl -sS --max-time 15 -X POST https://html.example.com/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H 'X-HtmlHost-Key: <shared secret>' \
  -H 'X-HtmlHost-User: alice@example.com' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"whoami","arguments":{}}}'
```

An authentication failure comes back *in the body*, not as an HTTP status: MCP
reports a tool error inside a `200` response. What it carries is HTMLHost's own
wording — `UNAUTHORIZED`, `MCP_DISABLED`, or `USER_NOT_REGISTERED` with its
sign-in link — so read the payload rather than the status code.

## Registering it in open-webui

Requires **open-webui 0.11.4 or newer** (`{{USER_EMAIL}}` in a tool server's
headers needs 0.9.6; this project's baseline is 0.11.4).

**Settings → Admin Settings → Integrations → External Tool Servers → Add:**

| Field | Value |
|---|---|
| Type | MCP (Streamable HTTP) |
| URL | `https://html.example.com/mcp` |
| Auth | **None** (not Bearer — see below) |
| Headers | `{"X-HtmlHost-Key": "<shared secret>", "X-HtmlHost-User": "{{USER_EMAIL}}"}` |

Both values come from **HTMLHost → /admin/mcp**, which also generates and
rotates the shared secret. The URL there is derived from the configured public
base address, so it is correct as long as you deploy on the HTMLHost domain.

`{{USER_EMAIL}}` is substituted by open-webui **server-side**, so a client
cannot forge it — that is what makes every user land in their own HTMLHost
account with no per-user setup. The security of the mode rests on the shared
secret staying secret, which is why it is rotatable and why it is never held by
this server.

Leave **Auth** as None. Selecting Bearer with an empty token makes open-webui
send `Authorization: Bearer ` with every request; HTMLHost tolerates that, but
there is no reason to send it.

A user whose email has no HTMLHost account is refused with `USER_NOT_REGISTERED`
and told to sign in once at HTMLHost's login page — under SSO that single
sign-in creates the account. Accounts are never created implicitly by this path.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Publish fails with an opaque 413; nothing in the container log | The proxy's body limit. Set `client_max_body_size` (nginx default is 1 MB). |
| Every request answered `400`, body plain text `Invalid HTTP request received.` | The bytes are malformed before any MCP code runs — look for a **duplicated `Host` header** first; see Nginx Proxy Manager above. The same line is in this container's log, once per bad request, and nothing reaches HTMLHost. |
| Every request refused; this container's log says `Invalid Host header` | The `Host` header does not match `MCP_ALLOWED_HOSTS`. This server answers `421` in that case. Compare what the proxy sends with the startup log line listing the accepted values. |
| 502 from the proxy | `MCP_HOST` is not `0.0.0.0` inside the container, or the proxy is looking at the wrong port. |
| Call connects, then hangs or drops at random | Proxy buffering or read timeout. `proxy_buffering off`, `proxy_read_timeout 300s`. |
| `curl` on the published port answers with something that is not JSON-RPC, `{"detail":"Not Found"}` for instance | You reached a different service. `docker ps` shows this container as `8000/tcp` with no `->`, meaning it publishes nothing to the host: the host's port 8000 belongs to something else. Test through the domain, or publish a port on this container. |
| `UPSTREAM_UNREACHABLE` | `HTMLHOST_URL` is wrong, or its certificate is from a private CA and `HTMLHOST_CA_BUNDLE` is unset. |
| `HTTP_502` whose message says the body was not JSON | Something between this server and HTMLHost answered instead — a proxy error page, a VPN portal, an SSO intercept. |
| `UNAUTHORIZED` in stdio mode | `HTMLHOST_PAT` is unset; stdio has no headers to forward. |
| `USER_NOT_REGISTERED` | The acting user has no HTMLHost account yet. The message carries the sign-in link; relay it. |
| `PAYLOAD_TOO_LARGE` | Content over 3 MiB in one call. Publish it through the HTMLHost web UI instead, which accepts up to 10 MB per file. |

When a failure happens inside the transport — a `400` with a plain-text body, a
hang, a 413 — the bytes the proxy actually sends are worth more than any log
line, and they are not hidden. Two containers on one Docker network talk through
the host's bridge, so the host sees their plaintext:

```bash
# one terminal: dump what this container receives
sudo tcpdump -i any -A -s 0 \
  "host $(sudo docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' htmlhost-mcp) and tcp port 8000"
# another: reproduce the failure against the public URL
curl -sS -i --max-time 10 -X POST https://html.example.com/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
```

A doubled `Host`, a lost request body, an unexpected `Content-Length`: all of it
is legible there. This container's log is less help than it looks — it writes one
identical line per malformed request and that line carries no timestamp, so
`docker logs --tail N` cannot tell you whether a new failure just arrived. Use
`--since`, or compare line counts.

Browser-based MCP clients are not supported: the DNS-rebinding check rejects a
request that carries an `Origin` header, and no browser client sends an email
header that HTMLHost could trust anyway.

## Notes for maintainers

Three things about this stack are not what a tutorial would suggest, and
guessing here costs an afternoon each:

1. **The MCP Python SDK is 2.x, and `FastMCP` no longer exists.** It was renamed
   to `MCPServer` and moved: `from mcp.server.mcpserver import MCPServer,
   Context`. `mcp.server.fastmcp` is now a module that raises on import, so
   v1-era examples fail immediately.
2. **`ToolError` is not exported from `mcp.server.mcpserver`.** Import it from
   `mcp.server.mcpserver.exceptions`.
3. **httpx 2.x is installed as `httpx2`, and that is also its import name.**
   There is no `httpx` module alongside it — the 1.x line ends at 0.28.1. Write
   `import httpx2`.

Two limits that look arbitrary but are not:

- `max_request_body_size` is raised to 8 MiB. The SDK's 4 MiB default is
  *smaller* than a worst-case 3 MiB publish once JSON escaping has doubled it,
  and it rejects the body inside the transport, before any tool argument
  exists, with an error that explains nothing.
- The tools check the 3 MiB content ceiling before sending, so oversized content
  comes back as the API's own `PAYLOAD_TOO_LARGE` message rather than as that
  transport error. HTMLHost re-checks the same limit and remains the authority.
