"""The project prefix filter: a run carrying forwardedProps.project also gets that
project's gateway tools. Fake tool lists only; nothing here reaches the gateway."""

from types import SimpleNamespace

import ag_ui_strands
import pytest
import strands
import strands.models
import strands.tools.mcp
from ag_ui.core import RunAgentInput
from guppi_agent import agent as agent_module

RETRIEVE = "docs___Retrieve"
LISTED = [
    SimpleNamespace(tool_name=name)
    for name in (
        "docs___Retrieve",
        "docs___AgenticRetrieveStream",
        "demo___chart",
        "demo___table",
        "mcp_app___open",
        "mcp-app___close",
        "other___thing",
        "demochart",
    )
]


def run_input(forwarded_props) -> RunAgentInput:
    return RunAgentInput(
        thread_id="t1",
        run_id="r1",
        messages=[{"id": "m1", "role": "user", "content": "hello"}],
        tools=[],
        context=[],
        state={},
        forwarded_props=forwarded_props,
    )


def names(tools) -> list[str]:
    return [tool.tool_name for tool in tools]


def test_without_a_project_only_the_retrieve_tool_is_kept():
    assert names(agent_module.select_tools(LISTED, RETRIEVE)) == [RETRIEVE]
    assert names(agent_module.select_tools(LISTED, RETRIEVE, None)) == [RETRIEVE]


def test_a_project_adds_its_own_prefixed_tools_beside_retrieve():
    selected = agent_module.select_tools(LISTED, RETRIEVE, "demo")
    assert names(selected) == [RETRIEVE, "demo___chart", "demo___table"]


def test_hyphens_in_the_project_name_become_underscores_in_the_prefix():
    assert agent_module.project_tool_prefix("mcp-app") == "mcp_app___"
    selected = agent_module.select_tools(LISTED, RETRIEVE, "mcp-app")
    # The hyphen-kept form is accepted too, in case the gateway keeps it.
    assert names(selected) == [RETRIEVE, "mcp_app___open", "mcp-app___close"]


def test_a_project_with_no_tools_on_the_gateway_keeps_retrieve():
    assert names(agent_module.select_tools(LISTED, RETRIEVE, "nothing")) == [RETRIEVE]


@pytest.mark.parametrize(
    "forwarded_props, expected",
    [
        ({"project": "demo"}, "demo"),
        ({"project": "mcp-app"}, "mcp-app"),
        ({}, None),
        (None, None),
        ("demo", None),
        ({"project": ""}, None),
        ({"project": "Demo"}, None),
        ({"project": "demo___"}, None),
        ({"project": "de mo"}, None),
        ({"project": 7}, None),
    ],
)
def test_run_project_reads_only_a_well_formed_name(forwarded_props, expected):
    assert agent_module.run_project(run_input(forwarded_props)) == expected


def test_the_prompt_names_the_project_tools_only_when_there_are_some():
    base = agent_module.system_prompt()
    assert agent_module.system_prompt("demo", []) == base
    assert agent_module.system_prompt(None, ["demo___chart"]) == base
    prompt = agent_module.system_prompt("demo", ["demo___chart", "demo___table"])
    assert prompt.startswith(base)
    added = prompt[len(base) :].strip()
    assert added == (
        "This page belongs to the demo project, which adds these tools: "
        "demo___chart, demo___table; use them for questions about that project."
    )


class FakeMCPClient:
    def __init__(self, url, headers):
        self.url = url

    def start(self):
        pass

    def stop(self, *args):
        pass

    def list_tools_sync(self, pagination_token=None):
        return LISTED


class FakeAgent:
    built: list[dict] = []

    def __init__(self, **kwargs):
        FakeAgent.built.append(kwargs)


class FakeAdapter:
    def __init__(self, template, **kwargs):
        pass

    async def run(self, run_input):
        yield "event"


@pytest.fixture
def strands_fakes(monkeypatch):
    FakeAgent.built = []
    monkeypatch.setattr(strands.tools.mcp, "MCPClient", FakeMCPClient)
    monkeypatch.setattr(strands, "Agent", FakeAgent)
    monkeypatch.setattr(strands.models, "BedrockModel", lambda **kwargs: None)
    monkeypatch.setattr(ag_ui_strands, "StrandsAgent", FakeAdapter)
    monkeypatch.setenv("TOOLS_GATEWAY_URL", "https://tools.example/mcp")
    monkeypatch.setenv("RETRIEVE_TOOL", RETRIEVE)
    return FakeAgent


async def run_once(forwarded_props) -> dict:
    run = agent_module.StrandsRun("token", agent_module.Settings())
    events = [event async for event in run.run(run_input(forwarded_props))]
    assert events == ["event"]
    (built,) = FakeAgent.built
    return built


async def test_a_project_run_builds_the_agent_with_the_project_tools(strands_fakes):
    built = await run_once({"project": "demo"})
    assert names(built["tools"]) == [RETRIEVE, "demo___chart", "demo___table"]
    assert "demo___chart, demo___table" in built["system_prompt"]


async def test_a_default_run_builds_the_agent_with_retrieve_only(strands_fakes):
    built = await run_once({})
    assert names(built["tools"]) == [RETRIEVE]
    assert built["system_prompt"] == agent_module.system_prompt()


class PagedClient:
    """A fake MCP client whose tools/list answers in pages, like a DYNAMIC gateway target."""

    def __init__(self, pages: list[list[str]]) -> None:
        self.pages = pages
        self.tokens_seen: list[str | None] = []

    def list_tools_sync(self, pagination_token=None):
        from strands.types.collections import PaginatedList

        self.tokens_seen.append(pagination_token)
        index = 0 if pagination_token is None else int(pagination_token)
        next_token = str(index + 1) if index + 1 < len(self.pages) else None
        tools = [SimpleNamespace(tool_name=n) for n in self.pages[index]]
        return PaginatedList(tools, token=next_token)


def test_every_tools_list_page_is_read():
    client = PagedClient(
        [
            ["docs___Retrieve", "docs___AgenticRetrieveStream"],
            ["mcp-app___show_card"],
            ["mcp-app___card_clicked"],
        ]
    )
    listed = agent_module.list_all_tools(client)
    assert names(listed) == [
        "docs___Retrieve",
        "docs___AgenticRetrieveStream",
        "mcp-app___show_card",
        "mcp-app___card_clicked",
    ]
    assert client.tokens_seen == [None, "1", "2"]
    selected = agent_module.select_tools(listed, RETRIEVE, "mcp-app")
    assert names(selected) == [RETRIEVE, "mcp-app___show_card", "mcp-app___card_clicked"]


def test_a_single_page_is_read_once():
    client = PagedClient([["docs___Retrieve"]])
    assert names(agent_module.list_all_tools(client)) == ["docs___Retrieve"]
    assert client.tokens_seen == [None]


def test_pagination_stops_at_the_page_limit(monkeypatch):
    monkeypatch.setattr(agent_module, "MAX_TOOL_PAGES", 3)

    class Endless:
        def list_tools_sync(self, pagination_token=None):
            from strands.types.collections import PaginatedList

            return PaginatedList([SimpleNamespace(tool_name="x___y")], token="again")

    assert len(agent_module.list_all_tools(Endless())) == 3
