"""The MCP server: one tool, `show_card`, and the UI resource it names.

`show_card` returns a text block for the model, the card page as an embedded resource, and
`structuredContent` with the title and body for an MCP Apps host to forward to the page.
The tool definition and the result both carry `_meta.ui.resourceUri`, the MCP Apps key
that tells a host which `ui://` resource renders the result. The resource itself is also
served by `resources/list` and `resources/read`, with the template values baked in.
"""

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.resources import TextResource
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import CallToolResult, EmbeddedResource, TextContent, TextResourceContents

from mcp_app_server.card import APP_MIME_TYPE, CARD_URI, card_html

HOST = "0.0.0.0"
PORT = 8000
PATH = "/mcp"

UI_META = {"ui": {"resourceUri": CARD_URI}}

mcp = MCPServer(
    "mcp-app",
    instructions="show_card shows the user a card with a title and a body.",
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
