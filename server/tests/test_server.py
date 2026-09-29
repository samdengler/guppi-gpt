"""show_card and the card resource through an in-process MCP client. `mode="legacy"`
runs the initialize handshake and JSON-RPC framing, as the tools gateway does."""

import json
import re

from mcp import Client
from mcp_app_server.card import APP_MIME_TYPE, CARD_URI, TEMPLATE_BODY, TEMPLATE_TITLE
from mcp_app_server.server import mcp


def connect() -> Client:
    # A context manager per test: an async generator fixture would exit the client's
    # cancel scope in another task.
    return Client(mcp, mode="legacy", raise_exceptions=True)


def baked_values(html: str) -> dict:
    match = re.search(r'<script type="application/json" id="card-data">(.*?)</script>', html)
    assert match, "the page carries its fallback values in a JSON script element"
    return json.loads(match.group(1))


async def test_tools_list_names_show_card_with_its_ui_resource():
    async with connect() as client:
        tools = (await client.list_tools()).tools
        assert [t.name for t in tools] == ["show_card"]
        tool = tools[0]
        assert set(tool.input_schema["properties"]) == {"title", "body"}
        assert set(tool.input_schema["required"]) == {"title", "body"}
        assert tool.meta == {"ui": {"resourceUri": CARD_URI}}


async def test_show_card_returns_text_embedded_resource_and_meta():
    async with connect() as client:
        result = await client.call_tool("show_card", {"title": "Hello", "body": "It works"})
        assert not result.is_error
        text, embedded = result.content
        assert text.type == "text"
        assert "Hello" in text.text
        assert embedded.type == "resource"
        assert str(embedded.resource.uri) == CARD_URI
        assert embedded.resource.mime_type == APP_MIME_TYPE
        assert baked_values(embedded.resource.text) == {"title": "Hello", "body": "It works"}
        assert result.structured_content == {"title": "Hello", "body": "It works"}
        assert result.meta == {"ui": {"resourceUri": CARD_URI}}


async def test_resources_list_and_read_serve_the_card():
    async with connect() as client:
        resources = (await client.list_resources()).resources
        assert [str(r.uri) for r in resources] == [CARD_URI]
        assert resources[0].mime_type == APP_MIME_TYPE

        contents = (await client.read_resource(CARD_URI)).contents
        assert len(contents) == 1
        assert str(contents[0].uri) == CARD_URI
        assert contents[0].mime_type == APP_MIME_TYPE
        assert baked_values(contents[0].text) == {"title": TEMPLATE_TITLE, "body": TEMPLATE_BODY}


async def test_card_values_cannot_close_the_script_element():
    async with connect() as client:
        hostile = "</script><script>alert(1)</script><!--"
        result = await client.call_tool("show_card", {"title": hostile, "body": "&amp; <b>"})
        html = result.content[1].resource.text
        assert html.count("</script>") == 2  # the data element and the page script, no more
        assert "<!--" not in html
        assert baked_values(html) == {"title": hostile, "body": "&amp; <b>"}


async def test_card_page_is_self_contained():
    async with connect() as client:
        html = (await client.read_resource(CARD_URI)).contents[0].text
        assert "<script src" not in html
        assert "<link" not in html
        for call in ("fetch(", "XMLHttpRequest", "WebSocket", "import(", "http://", "https://"):
            assert call not in html
        for method in (
            "ui/initialize",
            "ui/notifications/initialized",
            "ui/notifications/tool-input",
            "ui/notifications/tool-result",
        ):
            assert method in html
        assert "setTimeout" in html and "2000" in html
        assert "innerHTML" not in html
