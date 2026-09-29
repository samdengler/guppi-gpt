"""The MCP server: `show_card` and the phase 4 experiments, and the UI resources they name.

`show_card` returns a text block for the model, the card page as an embedded resource, and
`structuredContent` with the title and body for an MCP Apps host to forward to the page.
The tool definition and the result both carry `_meta.ui.resourceUri`, the MCP Apps key
that tells a host which `ui://` resource renders the result. The resource itself is also
served by `resources/list` and `resources/read`, with the template values baked in.

The experiments (`docs/experiments.md`) each add one tool: `show_chart` (E2) names its
resource without embedding it; `card_clicked` (E3) is the tool the card's button calls
through the host, visible to apps only, and it writes one audit line per call;
`update_card` (E4) returns the card resource again for an existing card id;
`show_static_page` (E5) names a resource whose content is a URL; `ask_preferences` (E6)
shows a form whose answers go back to the host as model context.
"""

import json
import logging
import sys
from datetime import UTC, datetime

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import TextResource
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, EmbeddedResource, TextContent, TextResourceContents

from mcp_app_server.card import APP_MIME_TYPE, CARD_URI, card_html, card_id_for
from mcp_app_server.chart import CHART_URI, chart_html
from mcp_app_server.preferences import PREFERENCES_URI, preferences_html
from mcp_app_server.static_page import (
    STATIC_PAGE_URI,
    STATIC_PAGE_URL,
    URI_LIST_MIME_TYPE,
    static_page_uri_list,
)

HOST = "0.0.0.0"
PORT = 8000
PATH = "/mcp"

UI_META = {"ui": {"resourceUri": CARD_URI}}
CHART_META = {"ui": {"resourceUri": CHART_URI}}
STATIC_PAGE_META = {"ui": {"resourceUri": STATIC_PAGE_URI}}
PREFERENCES_META = {"ui": {"resourceUri": PREFERENCES_URI}}
# An app-only tool: a host following the extension leaves it out of the model's tool list
# and lets only this server's apps call it.
APP_ONLY_META = {"ui": {"visibility": ["app"]}}

# One JSON line per app-initiated tool call, on stdout, so it lands in the Runtime's logs.
audit = logging.getLogger("mcp_app_server.audit")

mcp = MCPServer(
    "mcp-app",
    instructions=(
        "show_card shows the user a card with a title and a body. "
        "update_card changes the body of a card shown earlier. "
        "show_chart shows the user a bar chart of a list of numbers. "
        "show_static_page shows the user the MCP App Lab static page. "
        "ask_preferences shows the user a form for their display name and reply style."
    ),
    version="0.1.0",
)

mcp.add_resource(
    TextResource(
        uri=CARD_URI,
        name="card",
        title="Card",
        description="A card with a title and a body, rendered from a show_card result.",
        mime_type=APP_MIME_TYPE,
        text=card_html(),
    )
)

mcp.add_resource(
    TextResource(
        uri=CHART_URI,
        name="chart",
        title="Chart",
        description="A bar chart, rendered from a show_chart result the host pushes to it.",
        mime_type=APP_MIME_TYPE,
        text=chart_html(),
    )
)

mcp.add_resource(
    TextResource(
        uri=STATIC_PAGE_URI,
        name="static-page",
        title="Static page",
        description="The MCP App Lab static page, named by its URL.",
        mime_type=URI_LIST_MIME_TYPE,
        text=static_page_uri_list(),
    )
)

mcp.add_resource(
    TextResource(
        uri=PREFERENCES_URI,
        name="preferences",
        title="Preferences",
        description="A form for the user's display name and reply style.",
        mime_type=APP_MIME_TYPE,
        text=preferences_html(),
    )
)


@mcp.tool(
    description=(
        "Show the user a card with a title and a body. Use it when the user asks for a card."
    ),
    meta=UI_META,
)
def show_card(title: str, body: str) -> CallToolResult:
    card_id = card_id_for(title)
    return CallToolResult(
        content=[
            TextContent(
                type="text", text=f"Showed the user a card titled {title!r}, card id {card_id!r}."
            ),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=CARD_URI, mime_type=APP_MIME_TYPE, text=card_html(title, body)
                ),
            ),
        ],
        structured_content={"card_id": card_id, "title": title, "body": body},
        meta=UI_META,
    )


@mcp.tool(
    description=(
        "Change the body of a card shown earlier with show_card. card_id is the card's id: "
        "its title in lowercase with runs of other characters as one hyphen, so the card "
        "titled 'Hello World' is 'hello-world'."
    ),
    meta=UI_META,
)
def update_card(card_id: str, body: str) -> CallToolResult:
    # E4: the same ui://mcp-app/card resource and the same card id as the show_card call,
    # so a host that routes a later result into the existing frame can. The server keeps
    # no state; the title is whatever the frame already shows.
    return CallToolResult(
        content=[
            TextContent(type="text", text=f"Sent a new body for card {card_id!r}."),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=CARD_URI,
                    mime_type=APP_MIME_TYPE,
                    text=card_html(title="", body=body, card_id=card_id),
                ),
            ),
        ],
        structured_content={"card_id": card_id, "body": body},
        meta=UI_META,
    )


@mcp.tool(
    description=(
        "Show the user a bar chart of a list of numbers. Use it when the user asks for a chart."
    ),
    meta=CHART_META,
)
def show_chart(values: list[float]) -> CallToolResult:
    # E2: the result names the chart resource and does not embed it, so an MCP Apps host
    # has to fetch ui://mcp-app/chart with resources/read and push the values to it.
    return CallToolResult(
        content=[
            TextContent(type="text", text=f"Showed the user a bar chart of {len(values)} values.")
        ],
        structured_content={"values": values},
        meta=CHART_META,
    )


@mcp.tool(
    description="Show the user the MCP App Lab static page. Use it when the user asks for it.",
    meta=STATIC_PAGE_META,
)
def show_static_page() -> CallToolResult:
    # E5: the embedded resource is a URL in text/uri-list form, not HTML.
    return CallToolResult(
        content=[
            TextContent(type="text", text="Showed the user the MCP App Lab static page."),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=STATIC_PAGE_URI,
                    mime_type=URI_LIST_MIME_TYPE,
                    text=static_page_uri_list(),
                ),
            ),
        ],
        structured_content={"url": STATIC_PAGE_URL},
        meta=STATIC_PAGE_META,
    )


@mcp.tool(
    description=(
        "Ask the user for their preferences (display name and reply style) with a form. "
        "Use it when the user asks to set their preferences."
    ),
    meta=PREFERENCES_META,
)
def ask_preferences() -> CallToolResult:
    # E6: the answers do not come back through this tool; the form sends them to the host
    # as ui/update-model-context, for the host to add to the next turn.
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=(
                    "Showed the user a preferences form. Their answers reach the conversation "
                    "only if the host passes them on with a later turn."
                ),
            ),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=PREFERENCES_URI, mime_type=APP_MIME_TYPE, text=preferences_html()
                ),
            ),
        ],
        structured_content={"fields": ["display_name", "reply_style"]},
        meta=PREFERENCES_META,
    )


@mcp.tool(
    description="Record that the user pressed the button on a card. Called by the card itself.",
    meta=APP_ONLY_META,
)
def card_clicked(card_id: str) -> CallToolResult:
    # E3: the target of the card's button. It reaches this server only if the host relays
    # the app's tools/call.
    clicked_at = datetime.now(UTC).isoformat(timespec="seconds")
    audit.info(json.dumps({"event": "card_clicked", "card_id": card_id, "at": clicked_at}))
    return CallToolResult(
        content=[TextContent(type="text", text=f"Recorded a click on card {card_id!r}.")],
        structured_content={"card_id": card_id, "clicked_at": clicked_at},
    )


def configure_audit_log() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("audit %(message)s"))
    audit.addHandler(handler)
    audit.setLevel(logging.INFO)
    audit.propagate = False


def main() -> None:
    configure_audit_log()
    # Stateless streamable HTTP on 0.0.0.0:8000/mcp is the AgentCore Runtime contract for
    # protocol MCP. The runtime sits in front and sets its own Host header, so the SDK's
    # DNS rebinding check (meant for servers on a developer's loopback) is off.
    mcp.run(
        transport="streamable-http",
        host=HOST,
        port=PORT,
        streamable_http_path=PATH,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
