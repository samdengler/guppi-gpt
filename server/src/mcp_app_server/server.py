"""The MCP server: `show_card` and the phase 4 experiments, and the UI resources they name.

`show_card` returns a text block for the model, the card page as an embedded resource, and
`structuredContent` with the title and body for an MCP Apps host to forward to the page.
The tool definition and the result both carry `_meta.ui.resourceUri`, the MCP Apps key
that tells a host which `ui://` resource renders the result. The resource itself is also
served by `resources/list` and `resources/read`, with the template values baked in.

The experiments (`docs/experiments.md`) each add one tool: `show_chart` (E2) names its
resource without embedding it.
"""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import TextResource
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, EmbeddedResource, TextContent, TextResourceContents

from mcp_app_server.card import APP_MIME_TYPE, CARD_URI, card_html
from mcp_app_server.chart import CHART_URI, chart_html

HOST = "0.0.0.0"
PORT = 8000
PATH = "/mcp"

UI_META = {"ui": {"resourceUri": CARD_URI}}
CHART_META = {"ui": {"resourceUri": CHART_URI}}

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
    return CallToolResult(
        content=[
            TextContent(type="text", text=f"Showed the user a card titled {title!r}."),
            EmbeddedResource(
                type="resource",
                resource=TextResourceContents(
                    uri=CARD_URI, mime_type=APP_MIME_TYPE, text=card_html(title, body)
                ),
            ),
        ],
        structured_content={"title": title, "body": body},
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


def main() -> None:
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
