"""The MCP Apps UI resource: taken from a fake MCP tool result and relayed as an
`mcp-app/resource` CUSTOM event after the adapter's TOOL_CALL_RESULT. Nothing here
reaches Bedrock or the gateway."""

from types import SimpleNamespace

import ag_ui_strands
import pytest
import strands
import strands.models
import strands.tools.mcp
from ag_ui.core import EventType, RunAgentInput, TextMessageStartEvent, ToolCallResultEvent
from guppi_agent import agent as agent_module
from mcp.types import CallToolResult

HTML = "<!DOCTYPE html><html><body><p>card</p></body></html>"


def card_result(**overrides) -> CallToolResult:
    """A show_card result as guppi-mcp-app's server returns it."""
    result = {
        "content": [
            {"type": "text", "text": "Showed the user a card titled 'Hello'."},
            {
                "type": "resource",
                "resource": {
                    "uri": "ui://mcp-app/card",
                    "mimeType": "text/html;profile=mcp-app",
                    "text": HTML,
                },
            },
        ],
        "structuredContent": {"title": "Hello", "body": "It works"},
        "_meta": {"ui": {"resourceUri": "ui://mcp-app/card"}},
    }
    result.update(overrides)
    return CallToolResult.model_validate(result)


def test_the_embedded_ui_resource_becomes_the_event_value():
    assert agent_module.app_resource("tool-1", card_result()) == {
        "toolCallId": "tool-1",
        "uri": "ui://mcp-app/card",
        "mimeType": "text/html;profile=mcp-app",
        "text": HTML,
        "toolResult": {
            "content": [{"type": "text", "text": "Showed the user a card titled 'Hello'."}],
            "structuredContent": {"title": "Hello", "body": "It works"},
            "_meta": {"ui": {"resourceUri": "ui://mcp-app/card"}},
        },
    }


def test_a_result_without_structured_content_or_meta_still_carries_the_resource():
    value = agent_module.app_resource("tool-1", card_result(structuredContent=None, _meta=None))
    assert value["text"] == HTML
    assert set(value["toolResult"]) == {"content"}


@pytest.mark.parametrize(
    "resource",
    [
        # Not a ui:// resource.
        {"uri": "https://example.com/card", "mimeType": "text/html;profile=mcp-app", "text": HTML},
        # Plain HTML, not the MCP Apps profile.
        {"uri": "ui://mcp-app/card", "mimeType": "text/html", "text": HTML},
        # A blob, not text.
        {"uri": "ui://mcp-app/card", "mimeType": "text/html;profile=mcp-app", "blob": "PGh0bWw+"},
    ],
)
def test_other_embedded_resources_are_not_ui_resources(resource):
    result = card_result(content=[{"type": "resource", "resource": resource}])
    assert agent_module.app_resource("tool-1", result) is None


def test_a_text_only_result_has_no_ui_resource():
    result = CallToolResult.model_validate({"content": [{"type": "text", "text": "passages"}]})
    assert agent_module.app_resource("tool-1", result) is None


def test_the_meta_resource_uri_picks_among_several_resources():
    other = {
        "type": "resource",
        "resource": {
            "uri": "ui://mcp-app/other",
            "mimeType": "text/html;profile=mcp-app",
            "text": "<p>other</p>",
        },
    }
    card_blocks = card_result().model_dump(by_alias=True)["content"]
    result = card_result(content=[other, *card_blocks])
    assert agent_module.app_resource("tool-1", result)["uri"] == "ui://mcp-app/card"


def test_the_mime_type_profile_matches_with_a_space_and_any_case():
    assert agent_module.is_app_mime_type("text/html; profile=mcp-app")
    assert agent_module.is_app_mime_type("Text/HTML;Profile=MCP-App")
    assert not agent_module.is_app_mime_type("text/html")
    assert not agent_module.is_app_mime_type(None)


def test_the_installed_strands_client_records_the_resource_and_maps_as_before():
    """Calls the private Strands hook the subclass overrides, on the installed version,
    without starting the client."""
    resources = {}
    cls = agent_module.app_resource_client(strands.tools.mcp.MCPClient, resources)
    client = cls(url="https://tools.example/mcp", headers={})
    mapped = client._handle_tool_result("tool-1", card_result())
    assert resources["tool-1"]["uri"] == "ui://mcp-app/card"
    # What the model and the adapter see is unchanged: two text items, the HTML last.
    assert mapped["content"] == [
        {"text": "Showed the user a card titled 'Hello'."},
        {"text": HTML},
    ]


def tool_result_event(tool_call_id: str) -> ToolCallResultEvent:
    return ToolCallResultEvent(
        type=EventType.TOOL_CALL_RESULT,
        tool_call_id=tool_call_id,
        message_id="m-result",
        content='"<!DOCTYPE html>"',
    )


async def collect(events, resources):
    return [event async for event in agent_module.with_app_resources(events, resources)]


async def as_stream(events):
    for event in events:
        yield event


async def test_the_custom_event_follows_the_matching_tool_call_result():
    value = agent_module.app_resource("tool-1", card_result())
    resources = {"tool-1": value}
    start = TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id="m1")
    events = await collect(
        as_stream([tool_result_event("tool-0"), tool_result_event("tool-1"), start]), resources
    )
    assert [event.type for event in events] == [
        EventType.TOOL_CALL_RESULT,
        EventType.TOOL_CALL_RESULT,
        EventType.CUSTOM,
        EventType.TEXT_MESSAGE_START,
    ]
    custom = events[2]
    assert custom.name == "mcp-app/resource"
    assert custom.value == value
    assert resources == {}


async def test_no_resource_means_no_custom_event():
    events = await collect(as_stream([tool_result_event("tool-1")]), {})
    assert [event.type for event in events] == [EventType.TOOL_CALL_RESULT]


# The whole run, with the Strands and adapter classes replaced: the fake adapter makes the
# client handle one tool result, as the Strands tool would, then emits its TOOL_CALL_RESULT.


class FakeMCPClient:
    latest = None

    def __init__(self, url, headers):
        FakeMCPClient.latest = self

    def start(self):
        pass

    def stop(self, *args):
        pass

    def list_tools_sync(self):
        return [SimpleNamespace(tool_name="docs___Retrieve")]

    def _handle_tool_result(self, tool_use_id, call_tool_result):
        return {"toolUseId": tool_use_id}


class FakeAdapter:
    def __init__(self, template, **kwargs):
        pass

    async def run(self, run_input):
        FakeMCPClient.latest._handle_tool_result("tool-1", card_result())
        yield tool_result_event("tool-1")


async def test_a_run_emits_the_resource_event_after_the_tool_result(monkeypatch):
    monkeypatch.setattr(strands.tools.mcp, "MCPClient", FakeMCPClient)
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: None)
    monkeypatch.setattr(strands.models, "BedrockModel", lambda **kwargs: None)
    monkeypatch.setattr(ag_ui_strands, "StrandsAgent", FakeAdapter)
    monkeypatch.setenv("TOOLS_GATEWAY_URL", "https://tools.example/mcp")
    run = agent_module.StrandsRun("token", agent_module.Settings())
    run_input = RunAgentInput(
        thread_id="t1",
        run_id="r1",
        messages=[{"id": "m1", "role": "user", "content": "show a card"}],
        tools=[],
        context=[],
        state={},
        forwarded_props={"project": "mcp-app"},
    )
    events = [event async for event in run.run(run_input)]
    assert [event.type for event in events] == [EventType.TOOL_CALL_RESULT, EventType.CUSTOM]
    assert events[1].value["toolCallId"] == "tool-1"
    assert events[1].value["text"] == HTML
