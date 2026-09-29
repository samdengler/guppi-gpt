"""The MCP server: `show_card` and the phase 4 experiments, and the UI resources they name.

`show_card` returns a text block for the model, the card page as an embedded resource, and
`structuredContent` with the title and body for an MCP Apps host to forward to the page.
The tool definition and the result both carry `_meta.ui.resourceUri`, the MCP Apps key
that tells a host which `ui://` resource renders the result. The resource itself is also
served by `resources/list` and `resources/read`, with the template values baked in.

The experiments (`docs/experiments.md`) each add one tool: `show_chart` (E2) names its
resource without embedding it; `card_clicked` (E3) is the tool the card's button calls
through the host, visible to apps only, and it writes one audit line per call.
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

HOST = "0.0.0.0"
PORT = 8000
PATH = "/mcp"

UI_META = {"ui": {"resourceUri": CARD_URI}}
CHART_META = {"ui": {"resourceUri": CHART_URI}}
# An app-only tool: a host following the extension leaves it out of the model's tool list
# and lets only this server's apps call it.
APP_ONLY_META = {"ui": {"visibility": ["app"]}}

# One JSON line per app-initiated tool call, on stdout, so it lands in the Runtime's logs.
audit = logging.getLogger("mcp_app_server.audit")

mcp = MCPServer(
    "mcp-app",
    instructions=(
        "show_card shows the user a card with a title and a body. "
        "show_chart shows the user a bar chart of a list of numbers."
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
