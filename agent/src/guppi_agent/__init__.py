"""GuppiGPT agent package, and the kit a project agent builds on.

A project agent's whole HTTP surface is `app = create_app(build_agent)`; the rest is here
for projects that need the same pieces (docs/proposals/platform.md).
"""

from guppi_agent import conversation_log
from guppi_agent.agent import app_resource, with_app_resources
from guppi_agent.app import create_app
from guppi_agent.keepalive import with_keepalive
from guppi_agent.validation import trim_messages, validate_run

__all__ = [
    "app_resource",
    "conversation_log",
    "create_app",
    "trim_messages",
    "validate_run",
    "with_app_resources",
    "with_keepalive",
]
