"""The static page resource (experiment E5): a UI resource whose content is a URL.

The MCP Apps extension lists `externalUrl` content (`text/uri-list`) as deferred from its
first version (spec revision 2026-01-26, "Extensibility"); this resource tries it anyway.
Its text is one URI in `text/uri-list` form, the page this repository publishes beside
its manifest (`web/app/index.html`, synced to `projects/mcp-app/app/`).
"""

STATIC_PAGE_URI = "ui://mcp-app/static-page"
URI_LIST_MIME_TYPE = "text/uri-list"
STATIC_PAGE_URL = "https://chat.dengler.io/projects/mcp-app/app/index.html"


def static_page_uri_list() -> str:
    """The resource text: one URI, CRLF-terminated, as RFC 2483 writes a uri-list."""
    return f"{STATIC_PAGE_URL}\r\n"
