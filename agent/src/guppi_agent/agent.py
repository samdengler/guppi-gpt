"""The Strands agent behind one run.

Everything here is built per request: the MCP client carries the caller's bearer token to
the tools gateway, so it cannot outlive the request, and the AG-UI adapter caches one
Strands agent per thread, so a fresh adapter per request keeps the service stateless.
`build_strands_agent` is the seam the tests replace.

A run from a tools-only project page carries `forwardedProps.project`; the agent then also
offers the tools of that project's gateway target (`<project>___*`, hyphens as
underscores) beside the retrieve tool (docs/proposals/platform.md, "Project tiers").
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator
from typing import Any

from ag_ui.core import BaseEvent, RunAgentInput

from guppi_agent import conversation_log

log = logging.getLogger("guppi_agent")

DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_RETRIEVE_TOOL = "docs___Retrieve"
DEFAULT_REGION = "us-east-1"

MEMORY_SENTENCE = "Nothing is saved between page loads, and you cannot recall earlier sessions."
LOGGED_MEMORY_SENTENCE = (
    "Conversations are logged for troubleshooting; nothing is saved between page loads, "
    "and you cannot recall earlier sessions."
)

SYSTEM_PROMPT_TEMPLATE = """You are Guppi, the assistant behind GuppiGPT.

You have no memory beyond the conversation on the current page. {memory}

You have one tool, a search over the documentation of the Model Context Protocol (MCP),
Strands Agents, and the AG-UI protocol. When a question concerns any of those subjects,
search first and answer once from what the search returns, saying so when the passages do
not settle the question. Do not narrate the search or revise an earlier draft. Answer from
general knowledge otherwise, without searching.

Reply in concise plain text. The page renders no Markdown, so write no headings, bullet
markers, bold, code fences, or links; use short paragraphs and plain sentences instead, and
indent code by four spaces. In examples write server addresses as <server-url>, never as
localhost or a loopback address: the gateway in front of this page rejects any message
that contains one, which would end the conversation.
"""


# The same rule as a manifest's `name` and the page's /p/<name>/ path.
PROJECT_NAME = re.compile(r"[a-z0-9-]+")


def system_prompt(project: str | None = None, project_tools: list[str] | None = None) -> str:
    """The prompt for one run. The claim about saving follows the logging switch; a
    project page's run adds one sentence naming the project's tools."""
    logged = conversation_log.enabled()
    prompt = SYSTEM_PROMPT_TEMPLATE.format(
        memory=LOGGED_MEMORY_SENTENCE if logged else MEMORY_SENTENCE
    )
    if project and project_tools:
        prompt += (
            f"\nThis page belongs to the {project} project, which adds these tools: "
            f"{', '.join(project_tools)}; use them for questions about that project.\n"
        )
    return prompt


def run_project(run_input: RunAgentInput) -> str | None:
    """The project named in the run's forwardedProps, or None for the default project."""
    props = run_input.forwarded_props
    project = props.get("project") if isinstance(props, dict) else None
    if isinstance(project, str) and PROJECT_NAME.fullmatch(project):
        return project
    return None


def project_tool_prefix(project: str) -> str:
    """Gateway tool names are <target>___<tool>; the target is the project name with
    hyphens as underscores."""
    return f"{project.replace('-', '_')}___"


def select_tools(tools: list[Any], retrieve_tool: str, project: str | None = None) -> list[Any]:
    """The retrieve tool, plus the project's own tools when the run names a project."""
    prefix = project_tool_prefix(project) if project else None
    return [
        tool
        for tool in tools
        if tool.tool_name == retrieve_tool or (prefix and tool.tool_name.startswith(prefix))
    ]


class Settings:
    """Environment settings read once per request so tests can change them."""

    def __init__(self) -> None:
        self.tools_gateway_url = os.environ.get("TOOLS_GATEWAY_URL", "")
        self.model_id = os.environ.get("MODEL_ID", DEFAULT_MODEL_ID)
        self.retrieve_tool = os.environ.get("RETRIEVE_TOOL", DEFAULT_RETRIEVE_TOOL)
        self.region = os.environ.get("AWS_REGION", DEFAULT_REGION)


class StrandsRun:
    """One run: an MCP client with the user's token, a Strands agent, the AG-UI adapter."""

    def __init__(self, token: str, settings: Settings) -> None:
        if not settings.tools_gateway_url:
            raise RuntimeError("TOOLS_GATEWAY_URL is not set")
        self._token = token
        self._settings = settings
        self._agents_by_thread: dict[str, Any] = {}

    async def run(self, run_input: RunAgentInput) -> AsyncIterator[BaseEvent]:
        from ag_ui_strands import StrandsAgent, StrandsAgentConfig
        from strands import Agent
        from strands.models import BedrockModel
        from strands.tools.mcp import MCPClient

        settings = self._settings
        client = MCPClient(
            url=settings.tools_gateway_url,
            headers={"Authorization": f"Bearer {self._token}"},
        )
        # The client runs its own thread and event loop; start and stop block, so they are
        # kept off the loop that streams the response.
        await asyncio.to_thread(client.start)
        try:
            listed = await asyncio.to_thread(client.list_tools_sync)
            project = run_project(run_input)
            tools = select_tools(listed, settings.retrieve_tool, project)
            if not any(tool.tool_name == settings.retrieve_tool for tool in tools):
                log.warning(
                    "tool %s not offered by the gateway (offered: %s); running without it",
                    settings.retrieve_tool,
                    [tool.tool_name for tool in listed],
                )
            project_tools = [
                tool.tool_name for tool in tools if tool.tool_name != settings.retrieve_tool
            ]
            if project and not project_tools:
                log.warning(
                    "project %s has no %s* tools on the gateway; running with the retrieve tool",
                    project,
                    project_tool_prefix(project),
                )
            template = Agent(
                model=BedrockModel(
                    model_id=settings.model_id,
                    region_name=settings.region,
                    streaming=True,
                    max_tokens=1024,
                    temperature=0.7,
                ),
                system_prompt=system_prompt(project, project_tools),
                tools=tools,
                callback_handler=None,
            )
            adapter = StrandsAgent(
                template,
                name="guppi",
                config=StrandsAgentConfig(emit_messages_snapshot=False),
                agents_by_thread=self._agents_by_thread,
            )
            async for event in adapter.run(run_input):
                yield event
        finally:
            await asyncio.to_thread(client.stop, None, None, None)

    def usage(self) -> dict[str, int]:
        """Input and output token counts from the Strands agent, if a run happened."""
        for agent in self._agents_by_thread.values():
            metrics = getattr(agent, "event_loop_metrics", None)
            accumulated = getattr(metrics, "accumulated_usage", None) or {}
            return {
                "input_tokens": int(accumulated.get("inputTokens", 0)),
                "output_tokens": int(accumulated.get("outputTokens", 0)),
            }
        return {}


def build_strands_agent(token: str) -> StrandsRun:
    """Build the object that runs one request. Tests monkeypatch this name in app.py."""
    return StrandsRun(token, Settings())
