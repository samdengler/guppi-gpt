"""show_card and the card resource through an in-process MCP client. `mode="legacy"`
runs the initialize handshake and JSON-RPC framing, as the tools gateway does."""

import json
import logging
import re

from mcp import Client
from mcp_app_server.card import (
    APP_MIME_TYPE,
    CARD_URI,
    TEMPLATE_BODY,
    TEMPLATE_TITLE,
    card_id_for,
)
from mcp_app_server.chart import CHART_URI
from mcp_app_server.server import mcp


def connect() -> Client:
    # A context manager per test: an async generator fixture would exit the client's
    # cancel scope in another task.
    return Client(mcp, mode="legacy", raise_exceptions=True)


def baked_values(html: str) -> dict:
    match = re.search(r'<script type="application/json" id="app-data">(.*?)</script>', html)
    assert match, "the page carries its fallback values in a JSON script element"
    return json.loads(match.group(1))


def assert_self_contained(html: str) -> None:
    assert "<script src" not in html
    assert "<link" not in html
    for call in ("fetch(", "XMLHttpRequest", "WebSocket", "import(", "http://", "https://"):
        assert call not in html
    assert "innerHTML" not in html


async def tools_by_name(client: Client) -> dict:
    return {t.name: t for t in (await client.list_tools()).tools}


async def test_tools_list_names_show_card_with_its_ui_resource():
    async with connect() as client:
        tools = await tools_by_name(client)
        assert set(tools) == {"show_card", "show_chart", "card_clicked", "update_card"}
        tool = tools["show_card"]
        assert set(tool.input_schema["properties"]) == {"title", "body"}
        assert set(tool.input_schema["required"]) == {"title", "body"}
        assert tool.meta == {"ui": {"resourceUri": CARD_URI}}


async def test_show_card_returns_text_embedded_resource_and_meta():
    async with connect() as client:
        result = await client.call_tool("show_card", {"title": "Hello", "body": "It works"})
        assert not result.is_error
        text, embedded = result.content
        assert text.type == "text"
        assert "Hello" in text.text and "'hello'" in text.text
        assert embedded.type == "resource"
        assert str(embedded.resource.uri) == CARD_URI
        assert embedded.resource.mime_type == APP_MIME_TYPE
        card = {"card_id": "hello", "title": "Hello", "body": "It works"}
        assert baked_values(embedded.resource.text) == card
        assert result.structured_content == card
        assert result.meta == {"ui": {"resourceUri": CARD_URI}}


async def test_resources_list_and_read_serve_the_card():
    async with connect() as client:
        resources = {str(r.uri): r for r in (await client.list_resources()).resources}
        assert set(resources) == {CARD_URI, CHART_URI}
        assert resources[CARD_URI].mime_type == APP_MIME_TYPE

        contents = (await client.read_resource(CARD_URI)).contents
        assert len(contents) == 1
        assert str(contents[0].uri) == CARD_URI
        assert contents[0].mime_type == APP_MIME_TYPE
        assert baked_values(contents[0].text) == {
            "card_id": "mcp-app-card",
            "title": TEMPLATE_TITLE,
            "body": TEMPLATE_BODY,
        }


async def test_card_values_cannot_close_the_script_element():
    async with connect() as client:
        hostile = "</script><script>alert(1)</script><!--"
        result = await client.call_tool("show_card", {"title": hostile, "body": "&amp; <b>"})
        html = result.content[1].resource.text
        assert html.count("</script>") == 2  # the data element and the page script, no more
        assert "<!--" not in html
        assert baked_values(html)["title"] == hostile
        assert baked_values(html)["body"] == "&amp; <b>"


async def test_card_page_is_self_contained():
    async with connect() as client:
        html = (await client.read_resource(CARD_URI)).contents[0].text
        assert_self_contained(html)
        for method in (
            "ui/initialize",
            "ui/notifications/initialized",
            "ui/notifications/tool-input",
            "ui/notifications/tool-result",
        ):
            assert method in html
        assert "setTimeout" in html and "2000" in html


def test_card_id_is_the_title_as_a_slug():
    assert card_id_for("Hello") == "hello"
    assert card_id_for("Hello World!") == "hello-world"
    assert card_id_for("  Q3 -- plan  ") == "q3-plan"
    assert card_id_for("***") == "card"
    assert len(card_id_for("x" * 100)) == 40


async def test_card_button_calls_card_clicked_through_the_host():
    async with connect() as client:
        html = (await client.read_resource(CARD_URI)).contents[0].text
        assert 'id="card-click"' in html
        assert '"tools/call", { name: "card_clicked"' in html
        assert "Host refused tools/call" in html


async def test_card_clicked_is_app_only_and_writes_an_audit_line(caplog):
    async with connect() as client:
        tool = (await tools_by_name(client))["card_clicked"]
        assert tool.meta == {"ui": {"visibility": ["app"]}}
        assert set(tool.input_schema["required"]) == {"card_id"}

        with caplog.at_level(logging.INFO, logger="mcp_app_server.audit"):
            result = await client.call_tool("card_clicked", {"card_id": "hello"})
        assert not result.is_error
        assert "'hello'" in result.content[0].text
        assert result.structured_content["card_id"] == "hello"
        lines = [r.getMessage() for r in caplog.records if r.name == "mcp_app_server.audit"]
        assert len(lines) == 1
        line = json.loads(lines[0])
        assert line["event"] == "card_clicked" and line["card_id"] == "hello"
        assert line["at"] == result.structured_content["clicked_at"]


async def test_show_chart_names_its_resource_and_embeds_nothing():
    async with connect() as client:
        tool = (await tools_by_name(client))["show_chart"]
        assert tool.meta == {"ui": {"resourceUri": CHART_URI}}
        assert tool.input_schema["properties"]["values"]["type"] == "array"

        result = await client.call_tool("show_chart", {"values": [3, 1, 4.5]})
        assert not result.is_error
        assert [block.type for block in result.content] == ["text"]
        assert "3 values" in result.content[0].text
        assert result.structured_content == {"values": [3, 1, 4.5]}
        assert result.meta == {"ui": {"resourceUri": CHART_URI}}


async def test_chart_resource_is_read_by_uri():
    async with connect() as client:
        contents = (await client.read_resource(CHART_URI)).contents
        assert len(contents) == 1
        assert str(contents[0].uri) == CHART_URI
        assert contents[0].mime_type == APP_MIME_TYPE
        html = contents[0].text
        assert_self_contained(html)
        assert baked_values(html) == {"values": []}
        assert "structuredContent" in html and "ui/initialize" in html


async def test_update_card_returns_the_same_resource_for_the_same_card():
    async with connect() as client:
        tool = (await tools_by_name(client))["update_card"]
        assert tool.meta == {"ui": {"resourceUri": CARD_URI}}
        assert set(tool.input_schema["required"]) == {"card_id", "body"}

        shown = await client.call_tool("show_card", {"title": "Draft", "body": "First"})
        updated = await client.call_tool("update_card", {"card_id": "draft", "body": "Second"})
        assert not updated.is_error
        assert updated.meta == shown.meta == {"ui": {"resourceUri": CARD_URI}}
        text, embedded = updated.content
        assert "'draft'" in text.text
        assert str(embedded.resource.uri) == str(shown.content[1].resource.uri) == CARD_URI
        assert embedded.resource.mime_type == APP_MIME_TYPE
        assert updated.structured_content == {"card_id": "draft", "body": "Second"}
        assert shown.structured_content["card_id"] == "draft"
        assert baked_values(embedded.resource.text) == {
            "card_id": "draft",
            "title": "",
            "body": "Second",
        }
        assert "Updated by a later tool result" in embedded.resource.text
