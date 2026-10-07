"""The twelve MCP tools, each a thin translation of one /api/v1 call.

The tools validate nothing the API already validates and decide nothing the API
already decides: ownership, quotas and expiry all belong to HTMLHost, which is
also the only place that resolves credentials.

The descriptions below are not documentation for a human reader - they are the
entire interface the model sees. They therefore carry the expiry vocabulary,
the default visibility, and the rule about relaying error messages unchanged.
"""

from __future__ import annotations

import logging
import re
from typing import Literal, Optional

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from . import __version__
from .client import HtmlHostClient, resolve_auth
from .config import (
    DEFAULT_READ_LINES,
    MAX_READ_BYTES,
    MAX_READ_LINES,
    MAX_TOOL_CONTENT_SIZE,
)
from .errors import HtmlHostError

logger = logging.getLogger(__name__)

#: Appended to every tool description. Error messages are written for the person
#: who asked: USER_NOT_REGISTERED, for instance, carries a sign-in URL that
#: creates their account on first use, so paraphrasing it away removes the one
#: thing that fixes the problem.
_RELAY_NOTE = (
    "If this fails, pass the error message to the user unchanged. Some messages "
    "contain a URL they have to open, and rewriting or dropping it leaves them "
    "with no way forward."
)

#: Appended to every answer that is not a whole file. A tool description can ask
#: the model not to write a fragment back, but nothing enforces it: the one
#: irreversible mistake this tool makes possible is replacing a document with
#: the part of it that happened to be read, so the answer says so too.
_PARTIAL_NOTE = (
    "Do not pass a partial window to update_content: it would replace the "
    "document with the fragment above."
)

#: The expiry vocabulary. An enum rather than a free string because a typo here
#: costs a round-trip and HTMLHost accepts exactly these four. Anything else
#: belongs in expires_at, which takes an ISO-8601 timestamp.
ExpiresIn = Literal["30m", "24h", "7d", "never"]

TargetType = Literal["file", "project"]

INSTRUCTIONS = """\
HTMLHost publishes HTML and serves it at a shareable URL. Everything you do
through these tools is filed under the account of the person you are acting
for, and you can only see that account's content.

Usual flow: publish_html (or publish_project) to get a URL, get_file_content to
read a document back, update_content to revise it while the URL stays the same,
revoke_share to stop sharing it.

- Expiry: pass expires_in rather than computing a date yourself. It accepts
  "30m", "24h", "7d" or "never". Use expires_at only when an exact timestamp is
  genuinely required.
- Visibility: require_login defaults to false, meaning anyone with the link can
  open it. Pass true to require signing in to this HTMLHost first.
- Share ids look like "file:12" or "project:3". The type prefix is part of the
  id, not decoration, and you need the whole thing.
- The list tools are paginated and report the total, so you can tell whether
  more remain.
- Content is limited to 3 MiB per call. Larger documents have to be published
  through the HTMLHost web UI, and the error says so when that limit is hit.
- Content can be published, updated and unshared, but never deleted - there is
  no delete tool, by design.
"""


class FileEntry(BaseModel):
    """One file in a project."""

    path: str = Field(
        description=(
            "Path of the file inside the project, e.g. 'index.html' or "
            "'assets/site.css'. Relative, never absolute, and no '..'."
        )
    )
    content: str = Field(description="The file's text content.")


def _tool(server):
    """Register a tool, appending the error-relay rule to its description."""

    def decorate(fn):
        description = (fn.__doc__ or "").strip()
        fn.__doc__ = f"{description}\n\n{_RELAY_NOTE}"
        return server.tool()(fn)

    return decorate


# ---------------------------------------------------------------------------
# One tool invocation's worth of state
# ---------------------------------------------------------------------------


class _Api:
    """The API calls made during a single tool invocation.

    Auth is resolved lazily and once per invocation. It must never be resolved
    per *client*: a header set cached on a shared client would send one user's
    requests as another user, which is why the credentials live here, next to
    the one request they belong to.
    """

    def __init__(self, client, config, ctx):
        self._client = client
        self._config = config
        self._ctx = ctx
        self._auth = None

    def _headers(self):
        if self._auth is None:
            self._auth = resolve_auth(self._ctx, self._config)
        return self._auth

    async def raw(self, method, path, **kwargs):
        """Call the API, letting :class:`HtmlHostError` through to the caller."""
        return await self._client.request(method, path, auth=self._headers(), **kwargs)

    async def __call__(self, method, path, **kwargs):
        """Call the API, raising a ToolError that keeps HTMLHost's code and message."""
        try:
            return await self.raw(method, path, **kwargs)
        except HtmlHostError as exc:
            logger.info("api error: %s %s -> %s", method, path, exc.code)
            raise ToolError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _human_size(size):
    if size is None:
        return "size unknown"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.2f} MB"


def _visibility(link):
    return (
        "sign-in required"
        if link.get("require_login")
        else "public - anyone with the link"
    )


def _expiry(link):
    if not link.get("expires_at"):
        return "none - valid until revoked"
    prefix = "expired at" if link.get("expired") else "expires at"
    return f"{prefix} {link['expires_at']}"


def _share_line(link):
    bits = [
        f"`{link['id']}`",
        link["url"],
        "sign-in required" if link.get("require_login") else "public",
        _expiry(link),
    ]
    if not link.get("is_active"):
        bits.append("revoked")
    return "- " + " | ".join(bits)


def _share_detail(link):
    return "\n".join(
        [
            f"**URL:** {link['url']}",
            f"**Share id:** `{link['id']}` (points at {link['target_type']} "
            f"{link['target_id']})",
            f"**Visibility:** {_visibility(link)}",
            f"**Status:** {'active' if link.get('is_active') else 'revoked'}",
            f"**Expiry:** {_expiry(link)}",
            f"**Created:** {link.get('created_at')}",
        ]
    )


def _share_block(links, empty="No share links yet."):
    if not links:
        return empty
    return "\n".join(_share_line(link) for link in links)


def _file_line(item):
    return (
        f"- **{item['name']}** | id {item['id']} | "
        f"{_human_size(item.get('size'))} | uploaded {item.get('uploaded_at')}"
    )


def _project_line(item):
    return (
        f"- **{item['name']}** | id {item['id']} | "
        f"{item.get('file_count', 0)} file(s) | "
        f"{_human_size(item.get('total_size'))} | created {item.get('created_at')}"
    )


def _page_note(data, noun):
    if not data.get("has_more"):
        return ""
    return (
        f"\n\nShowing page {data['page']} of {data['total']} {noun}(s), "
        f"{data['limit']} per page. Call again with page={data['page'] + 1} "
        "for the rest."
    )


def _fenced(text):
    """``text`` in a code fence that its own contents cannot close.

    A page that documents HTML contains fenced code blocks of its own, and the
    usual three backticks would end at the first of them - leaving the model to
    guess where the file it just read actually stops.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text}\n{fence}"


def _prefix_within(text, max_bytes):
    """How many characters of ``text`` fit inside ``max_bytes`` of UTF-8.

    The window's budget is bytes (see config.MAX_READ_BYTES), but the cut is
    made between characters, so it can never land inside one and leave a
    fragment that is not text at all. A bisection rather than a decode with
    errors ignored: the answer is the same and nothing here has to decide what
    to do with bytes it cannot read.
    """
    if len(text.encode("utf-8")) <= max_bytes:
        return len(text)
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if len(text[:middle].encode("utf-8")) <= max_bytes:
            low = middle
        else:
            high = middle - 1
    return low


def _expiry_fields(expires_in, expires_at=None):
    """Build the expiry half of a share payload.

    ``expires_in`` wins when both are given - it is the one callers are told to
    use, since a relative duration cannot be miscalculated.
    """
    if expires_in is not None:
        return {"expires_in": expires_in}
    if expires_at is not None:
        return {"expires_at": expires_at}
    return {}


def _guard_content_size(contents):
    """Refuse content the API would refuse, before it becomes a request body.

    A pre-check, not a second authority: HTMLHost checks the same limit and
    remains the one place that decides. Its purpose is the error the caller
    sees. The transport caps request bodies at MAX_REQUEST_BODY_SIZE and JSON
    escaping doubles a pathological payload, so without this check a large
    enough argument is rejected inside the transport, with a message that says
    nothing about the real limit or what to do instead.
    """
    total = sum(len(content.encode("utf-8")) for content in contents)
    if total > MAX_TOOL_CONTENT_SIZE:
        raise ToolError(
            f"[PAYLOAD_TOO_LARGE] Content is {total} bytes, over the "
            f"{MAX_TOOL_CONTENT_SIZE} byte limit for a single MCP call (3 MiB). "
            "Publish larger content through the HTMLHost web UI."
        )


def _created_output(label, link):
    """Report a successful publish together with the link just minted for it.

    The URL leads, because it is the one thing the caller is going to use; the
    rest of the link's details follow from _share_detail.
    """
    return "\n".join([label, "", _share_detail(link)])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_tools(server, config):
    """Attach the twelve tools to ``server``, wired to ``config``."""
    client = HtmlHostClient(config)

    # -- identity ----------------------------------------------------------

    @_tool(server)
    async def whoami(ctx: Context) -> str:
        """Report which HTMLHost account this session is acting as.

        Useful when something behaves unexpectedly: it is the quickest way to
        confirm the identity mapping and see that a request has not landed on
        the wrong account.
        """
        me = await _Api(client, config, ctx)("GET", "/me")
        admin = "yes" if me.get("is_admin") else "no"
        return "\n".join(
            [
                f"You are **{me.get('display_name')}** <{me['email']}> "
                f"(user id {me['id']}).",
                "",
                f"- Administrator: {admin}",
                f"- MCP access: {'enabled' if me.get('mcp_enabled') else 'disabled'}",
                "",
                "Anything you publish is filed under this account, and only this "
                "account's content is visible to you.",
            ]
        )

    # -- publishing --------------------------------------------------------

    @_tool(server)
    async def publish_html(
        ctx: Context,
        name: str,
        content: str,
        expires_in: Optional[ExpiresIn] = None,
        require_login: bool = False,
        folder_id: Optional[int] = None,
    ) -> str:
        """Publish a single HTML document and return a URL for it.

        This is the usual way to put a page online: it publishes the document
        and mints its share link in one step. The document must be named with a
        .html or .htm extension.

        Content is limited to 3 MiB per call; a larger document has to be
        published through the HTMLHost web UI.
        """
        api = _Api(client, config, ctx)
        _guard_content_size([content])
        payload = {"name": name, "content": content}
        if folder_id is not None:
            payload["folder_id"] = folder_id
        created = await api("POST", "/files", json=payload)

        share_payload = {
            "target_type": "file",
            "target_id": created["id"],
            "require_login": require_login,
            **_expiry_fields(expires_in),
        }
        try:
            # .raw, not the converting call: this branch needs the original
            # error, because the document is already stored and reporting a
            # plain failure would invite a retry that publishes it twice.
            link = await api.raw("POST", "/shares", json=share_payload)
        except HtmlHostError as exc:
            logger.warning("file %s published but share failed: %s", created["id"], exc)
            return (
                f"**{created['name']}** was published as file id {created['id']}, "
                "but creating its share link failed, so there is no URL for it "
                "yet.\n\n"
                f"HTMLHost said: {exc}\n\n"
                'Once that is resolved, call create_share with '
                f'target_type="file" and target_id={created["id"]}.'
            )

        return _created_output(
            f"Published **{created['name']}** as file id {created['id']} "
            f"({_human_size(created.get('size'))}).",
            link,
        )

    @_tool(server)
    async def publish_project(
        ctx: Context,
        name: str,
        files: list[FileEntry],
        expires_in: Optional[ExpiresIn] = None,
        require_login: bool = False,
    ) -> str:
        """Publish a multi-file project and return a URL for it.

        Use this instead of publish_html when the work spans several files -
        HTML plus CSS, JavaScript, images - because the files are then served
        over real URLs and relative links between them work.

        Paths are relative to the project root ('index.html', 'assets/app.js').
        HTML entries must use .html or .htm; other files must use an extension
        HTMLHost allows there.

        The combined content of the files is limited to 3 MiB per call; a larger
        project has to be published through the HTMLHost web UI.
        """
        api = _Api(client, config, ctx)
        _guard_content_size(entry.content for entry in files)
        created = await api(
            "POST",
            "/projects",
            json={
                "name": name,
                "files": [entry.model_dump() for entry in files],
            },
        )

        share_payload = {
            "target_type": "project",
            "target_id": created["id"],
            "require_login": require_login,
            **_expiry_fields(expires_in),
        }
        try:
            # .raw, for the reason given on the file branch above.
            link = await api.raw("POST", "/shares", json=share_payload)
        except HtmlHostError as exc:
            logger.warning(
                "project %s published but share failed: %s", created["id"], exc
            )
            return (
                f"**{created['name']}** was published as project id "
                f"{created['id']}, but creating its share link failed, so there "
                "is no URL for it yet.\n\n"
                f"HTMLHost said: {exc}\n\n"
                "Once that is resolved, call create_share with "
                f'target_type="project" and target_id={created["id"]}.'
            )

        return _created_output(
            f"Published **{created['name']}** as project id {created['id']} "
            f"({created.get('file_count', 0)} file(s), "
            f"{_human_size(created.get('total_size'))}).",
            link,
        )

    @_tool(server)
    async def update_content(
        ctx: Context,
        target_type: TargetType,
        target_id: int,
        content: Optional[str] = None,
        files: Optional[list[FileEntry]] = None,
    ) -> str:
        """Replace a published document's or project's content.

        **The share URLs do not change**, so anyone holding a link keeps working
        with the updated version. This is the right tool for revising work.

        For a file, pass ``content``. For a project, pass ``files``; each entry
        is added or overwritten by path, and paths you leave out are left alone.

        Content is limited to 3 MiB per call; anything larger has to go through
        the HTMLHost web UI.
        """
        api = _Api(client, config, ctx)

        if target_type == "file":
            if content is None:
                raise ToolError(
                    "update_content needs 'content' when target_type is 'file'. "
                    "A project is updated with 'files' instead."
                )
            if files:
                raise ToolError(
                    "Pass either 'content' (for a file) or 'files' (for a "
                    "project), not both."
                )
            _guard_content_size([content])
            updated = await api("PUT", f"/files/{target_id}/content", json={"content": content})
            heading = (
                f"Updated **{updated['name']}** (file id {updated['id']}, "
                f"{_human_size(updated.get('size'))})."
            )
        else:
            if not files:
                raise ToolError(
                    "update_content needs 'files' when target_type is 'project'. "
                    "A single document is updated with 'content' instead."
                )
            if content is not None:
                raise ToolError(
                    "Pass either 'content' (for a file) or 'files' (for a "
                    "project), not both."
                )
            _guard_content_size(entry.content for entry in files)
            updated = await api(
                "PUT",
                f"/projects/{target_id}/files",
                json={"files": [entry.model_dump() for entry in files]},
            )
            heading = (
                f"Updated **{updated['name']}** (project id {updated['id']}, now "
                f"{updated.get('file_count', 0)} file(s), "
                f"{_human_size(updated.get('total_size'))})."
            )

        body = [heading, "", "The URL is unchanged. Existing share links:"]
        body.append(
            _share_block(
                updated.get("share_links", []),
                "none - this content is not shared yet. Use create_share.",
            )
        )
        if updated.get("files") is not None:
            body += ["", "Files in the project:"]
            body += [
                f"- `{entry['path']}` ({_human_size(entry.get('size'))})"
                for entry in updated["files"]
            ]
        return "\n".join(body)

    # -- reading -----------------------------------------------------------

    @_tool(server)
    async def list_contents(
        ctx: Context, kind: Literal["all", "file", "project"] = "all", page: int = 1, limit: int = 20
    ) -> str:
        """List what this account has published, newest first.

        Use ``kind`` to narrow to single documents or multi-file projects. Both
        are paginated: the answer says how many there are in total and how to
        get the next page.
        """
        api = _Api(client, config, ctx)
        params = {"page": page, "limit": limit}
        sections = []

        if kind in ("all", "file"):
            data = await api("GET", "/files", params=params)
            lines = [
                f"## Documents ({data['total']})",
                *(_file_line(item) for item in data["files"]),
            ]
            if not data["files"]:
                lines.append("None.")
            sections.append("\n".join(lines) + _page_note(data, "document"))

        if kind in ("all", "project"):
            data = await api("GET", "/projects", params=params)
            lines = [
                f"## Projects ({data['total']})",
                *(_project_line(item) for item in data["projects"]),
            ]
            if not data["projects"]:
                lines.append("None.")
            sections.append("\n".join(lines) + _page_note(data, "project"))

        if not sections:
            # Unreachable through the schema's enum, but a clear answer beats an
            # empty string if a client ever sends something else.
            return 'Nothing matched. kind must be "all", "file" or "project".'
        return "\n\n".join(sections)

    @_tool(server)
    async def get_content(ctx: Context, target_type: TargetType, target_id: int) -> str:
        """Show one document or project: its details, its files, and every share
        link minted for it.

        Use this to find out whether something is already shared before minting
        another link for it. It carries no document text; to read a document's
        text, use get_file_content.
        """
        api = _Api(client, config, ctx)
        if target_type == "file":
            item = await api("GET", f"/files/{target_id}")
            lines = [
                f"**{item['name']}** (file id {item['id']})",
                "",
                f"- Size: {_human_size(item.get('size'))}",
                f"- Uploaded: {item.get('uploaded_at')}",
                f"- Last updated: {item.get('updated_at')}",
                f"- Folder id: {item.get('folder_id')}",
            ]
        else:
            item = await api("GET", f"/projects/{target_id}")
            lines = [
                f"**{item['name']}** (project id {item['id']})",
                "",
                f"- Files: {item.get('file_count', 0)}",
                f"- Total size: {_human_size(item.get('total_size'))}",
                f"- Created: {item.get('created_at')}",
                f"- Last updated: {item.get('updated_at')}",
                "",
                "Files:",
            ]
            lines += [
                f"- `{entry['path']}` ({_human_size(entry.get('size'))})"
                for entry in item.get("files", [])
            ]

        lines += ["", "Share links:", _share_block(item.get("share_links", []))]
        return "\n".join(lines)

    @_tool(server)
    async def get_file_content(
        ctx: Context,
        file_id: int,
        offset: int = 1,
        limit: int = DEFAULT_READ_LINES,
        offset_chars: Optional[int] = None,
    ) -> str:
        # A plain string, not an f-string: an f-string is not a docstring, so the
        # whole description below would silently vanish and the model would be
        # left with nothing but the relay note _tool appends. The three numbers
        # quoted here are therefore written out - keep them in step with
        # config.MAX_READ_BYTES, DEFAULT_READ_LINES and MAX_READ_LINES.
        """Read a published document's text back, one window at a time.

        Use this to see a document as it stands now - after publishing it, or
        before revising one that a person may have edited in the HTMLHost web
        UI. Only single documents can be read this way; a project's files cannot
        be read through these tools.

        A large document is never returned all at once. One of them can cost
        hundreds of thousands of tokens, which ends the conversation, so the
        answer is one window of it. ``offset`` is the first line to return (lines
        are numbered from 1) and ``limit`` is how many lines to ask for (default
        200, at most 2,000); a window also stops at 40,000 bytes, whichever comes
        first. Every answer states the lines it covers and the document's total,
        so you can always tell whether you are holding the whole file.

        **When the answer says it is not the whole file, do not pass what you
        read to update_content.** That would replace the document with the
        fragment you happened to see. Read the remaining windows first.

        The text is byte for byte what is stored, so a window covering the whole
        file can be edited and written back unchanged. The revision reported
        with it is the same value the HTMLHost web editor shows for this
        document, which is also how to check that you are looking at the version
        you expected.

        ``offset_chars`` continues a single line that is too long for one
        window; when that happens the answer says which value to pass. Leave it
        out otherwise.
        """
        api = _Api(client, config, ctx)
        if offset < 1:
            raise ToolError("offset is a line number and starts at 1.")
        if limit < 1:
            raise ToolError("limit is a number of lines and must be at least 1.")
        if offset_chars is not None and offset_chars < 0:
            raise ToolError(
                "offset_chars is a character position within a line and cannot "
                "be negative. Leave it out to start at the line's beginning."
            )

        # The name comes from the details call: the content endpoint answers with
        # the text, the revision and the timestamp only, and an answer that names
        # the document is easier to trust than one showing a bare id.
        item = await api("GET", f"/files/{file_id}")
        data = await api("GET", f"/files/{file_id}/content")

        text = data["content"]
        lines = text.split("\n")
        total_lines = len(lines)
        size = len(text.encode("utf-8"))

        if offset > total_lines:
            raise ToolError(
                f"offset {offset} is past the end: file {file_id} has "
                f"{total_lines:,} line(s). Start again at offset=1 - the "
                "document may have changed since you last read it."
            )

        limit = min(limit, MAX_READ_LINES)
        start = offset - 1
        line = lines[start]
        line_bytes = len(line.encode("utf-8"))

        header = [
            f"**{item['name']}** (file id {item['id']}) - {total_lines:,} line(s), "
            f"{size:,} bytes, updated {data.get('updated_at')}",
            f"Revision `{data['revision']}` - the same value the HTMLHost web "
            "editor shows for this document.",
        ]

        if offset_chars:
            # Continuing a line too long for one window. Only that line is ever
            # carried here: a window that mixed the tail of one line into a run
            # of the lines after it would make its own line range untrue.
            if offset_chars >= len(line):
                raise ToolError(
                    f"offset_chars {offset_chars:,} is past the end of line "
                    f"{offset}, which is {len(line):,} character(s) long."
                )
            rest = line[offset_chars:]
            if len(rest.encode("utf-8")) > MAX_READ_BYTES:
                kept = _prefix_within(rest, MAX_READ_BYTES)
                return "\n".join(
                    header
                    + [
                        "",
                        f"Line {offset} is {line_bytes:,} bytes long, more than "
                        f"one window holds ({MAX_READ_BYTES:,} bytes), so it has "
                        "to be read in pieces.",
                        f"This window is line {offset} from character "
                        f"{offset_chars:,} onward: its next {kept:,} characters. "
                        "The line is not finished.",
                        "",
                        _fenced(rest[:kept]),
                        "",
                        f"Call get_file_content again with file_id={item['id']}, "
                        f"offset={offset}, offset_chars={offset_chars + kept} to "
                        f"continue inside this line. {_PARTIAL_NOTE}",
                    ]
                )
            after = (
                f"Continue with offset={offset + 1}."
                if offset < total_lines
                else "That was the document's last line."
            )
            return "\n".join(
                header
                + [
                    "",
                    f"The rest of line {offset}, from character {offset_chars:,} "
                    f"onward ({len(rest):,} characters). Line {offset} is now "
                    "complete.",
                    "",
                    _fenced(rest),
                    "",
                    f"{after} {_PARTIAL_NOTE}",
                ]
            )

        if line_bytes > MAX_READ_BYTES:
            # A single line bigger than a whole window - minified markup, usually.
            # Left unhandled, the documents an agent is most likely to have
            # published itself would be unreadable through this tool.
            kept = _prefix_within(line, MAX_READ_BYTES)
            return "\n".join(
                header
                + [
                    "",
                    f"Line {offset} is {line_bytes:,} bytes long, more than one "
                    f"window holds ({MAX_READ_BYTES:,} bytes), so it has to be "
                    "read in pieces.",
                    f"This window is the first {kept:,} characters of line "
                    f"{offset}. The line is not finished.",
                    "",
                    _fenced(line[:kept]),
                    "",
                    f"Call get_file_content again with file_id={item['id']}, "
                    f"offset={offset}, offset_chars={kept} to continue inside this "
                    f"line. {_PARTIAL_NOTE}",
                ]
            )

        # Whole lines only, so consecutive windows can be joined back into the
        # file: the newline between each pair is part of what the window costs. A
        # line that would cross the ceiling ends the window instead of being cut,
        # which is why the two cases above are handled first.
        end = min(start + limit, total_lines)
        window = [line]
        used = line_bytes
        for index in range(start + 1, end):
            cost = len(lines[index].encode("utf-8")) + 1
            if used + cost > MAX_READ_BYTES:
                break
            window.append(lines[index])
            used += cost
        last = start + len(window)  # also the 1-based number of the last line

        if offset == 1 and last == total_lines:
            cover = f"The whole file (lines 1-{total_lines:,}):"
            tail = ""
        else:
            cover = (
                f"Lines {offset:,}-{last:,} of {total_lines:,} - not the whole "
                "file:"
            )
            # The final window of a file read from the front says so, rather
            # than pointing at a line that does not exist.
            if last == total_lines:
                tail = f"\n\nThat is the end of the document. {_PARTIAL_NOTE}"
            else:
                tail = f"\n\nContinue with offset={last + 1}. {_PARTIAL_NOTE}"
        return "\n".join(header + ["", cover, "", _fenced("\n".join(window))]) + tail

    # -- share links -------------------------------------------------------

    @_tool(server)
    async def list_shares(ctx: Context, page: int = 1, limit: int = 20) -> str:
        """List this account's share links across documents and projects.

        Revoked links are included and marked as such, so this is also the way
        to check what used to be shared.
        """
        api = _Api(client, config, ctx)
        data = await api("GET", "/shares", params={"page": page, "limit": limit})
        lines = [f"## Share links ({data['total']})", _share_block(data["shares"])]
        return "\n".join(lines) + _page_note(data, "link")

    @_tool(server)
    async def get_share(ctx: Context, share_id: str) -> str:
        """Show one share link: where it points, who can open it and when it
        expires.

        ``share_id`` is the full id including its type prefix, as in
        ``"file:12"`` or ``"project:3"``.
        """
        api = _Api(client, config, ctx)
        return _share_detail(await api("GET", f"/shares/{share_id}"))

    @_tool(server)
    async def create_share(
        ctx: Context,
        target_type: TargetType,
        target_id: int,
        expires_in: Optional[ExpiresIn] = None,
        expires_at: Optional[str] = None,
        require_login: bool = False,
    ) -> str:
        """Mint an additional share link for content that is already published.

        Use this to share the same content a second time with different rules -
        for example a public link that lasts a day alongside a permanent one -
        or to share something published earlier that has no link yet.

        Prefer ``expires_in`` over ``expires_at``; it cannot be miscalculated.
        """
        api = _Api(client, config, ctx)
        link = await api(
            "POST",
            "/shares",
            json={
                "target_type": target_type,
                "target_id": target_id,
                "require_login": require_login,
                **_expiry_fields(expires_in, expires_at),
            },
        )
        return _share_detail(link)

    @_tool(server)
    async def update_share(
        ctx: Context,
        share_id: str,
        expires_in: Optional[ExpiresIn] = None,
        expires_at: Optional[str] = None,
        require_login: Optional[bool] = None,
        is_active: Optional[bool] = None,
    ) -> str:
        """Change an existing share link, leaving its URL as it is.

        Only the fields you pass are changed:

        - ``expires_in`` / ``expires_at`` set when it stops working. Pass
          ``expires_in="never"`` to remove an expiry altogether.
        - ``require_login`` switches between public and sign-in-required.
        - ``is_active=false`` stops the link working without deleting it, so the
          change can be undone later. Use revoke_share to remove it for good.

        ``share_id`` is the full id including its type prefix, as in
        ``"file:12"``.
        """
        api = _Api(client, config, ctx)
        payload = _expiry_fields(expires_in, expires_at)
        if require_login is not None:
            payload["require_login"] = require_login
        if is_active is not None:
            payload["is_active"] = is_active
        if not payload:
            raise ToolError(
                "Nothing to change. Pass at least one of 'expires_in', "
                "'expires_at', 'require_login' or 'is_active'."
            )
        return _share_detail(await api("PATCH", f"/shares/{share_id}", json=payload))

    @_tool(server)
    async def revoke_share(ctx: Context, share_id: str) -> str:
        """Revoke a share link so its URL stops working.

        This removes the link only. The document or project stays published and
        can be shared again later with create_share; nothing is deleted.

        ``share_id`` is the full id including its type prefix, as in
        ``"file:12"``.
        """
        api = _Api(client, config, ctx)
        result = await api("DELETE", f"/shares/{share_id}")
        return (
            f"Revoked share link `{result['id']}`. It pointed at "
            f"{result['target_type']} {result['target_id']}, which is still "
            "published - only the link is gone."
        )

    return client


def build_server(config):
    """Build the MCPServer with every tool registered.

    The HTTP client the tools share is created inside and lives as long as the
    server does; process exit closes its sockets, so nothing here needs to
    dispose of it.
    """
    server = MCPServer(
        name="htmlhost",
        title="HTMLHost",
        instructions=INSTRUCTIONS,
        version=__version__,
    )
    register_tools(server, config)
    return server
