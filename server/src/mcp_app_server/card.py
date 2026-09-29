"""The card page, served as the `ui://mcp-app/card` UI resource.

It renders a title and a body from `ui/notifications/tool-result` (`structuredContent`)
or `ui/notifications/tool-input` (`arguments`), and falls back to the values baked in at
call time after two seconds of host silence (see `bridge.py`).
"""

from mcp_app_server.bridge import APP_MIME_TYPE, APPS_PROTOCOL_VERSION, app_page

__all__ = [
    "APP_MIME_TYPE",
    "APPS_PROTOCOL_VERSION",
    "CARD_URI",
    "TEMPLATE_BODY",
    "TEMPLATE_TITLE",
    "card_html",
]

CARD_URI = "ui://mcp-app/card"

TEMPLATE_TITLE = "MCP App card"
TEMPLATE_BODY = "Waiting for a show_card result from the host."

_STYLE = """\
  .card { margin: 8px; padding: 16px 20px; border: 1px solid #8884; border-radius: 12px; }
  .card h1 { margin: 0 0 8px; font-size: 18px; font-weight: 600; }
  .card p { margin: 0; white-space: pre-wrap; }
"""

_BODY = """\
<article class="card">
  <h1 id="card-title"></h1>
  <p id="card-body"></p>
</article>
"""

_SCRIPT = """\
  var titleEl = document.getElementById("card-title");
  var bodyEl = document.getElementById("card-body");

  function render(values) {
    if (!values || typeof values !== "object") return;
    if (typeof values.title === "string") titleEl.textContent = values.title;
    if (typeof values.body === "string") bodyEl.textContent = values.body;
    app.resize();
  }

  var app = mcpApp({ name: "mcp-app-card", version: "0.1.0" }, {
    toolInput: render,
    toolResult: function (result) { render(result.structuredContent); },
    fallback: function () { render(baked); }
  });
"""


def card_html(title: str = TEMPLATE_TITLE, body: str = TEMPLATE_BODY) -> str:
    """The card page with `title` and `body` baked in as its fallback values."""
    return app_page(
        title="MCP App card",
        style=_STYLE,
        body=_BODY,
        script=_SCRIPT,
        data={"title": title, "body": body},
    )
