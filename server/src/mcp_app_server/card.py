"""The card page, served as the `ui://mcp-app/card` UI resource.

It renders a title and a body from `ui/notifications/tool-result` (`structuredContent`)
or `ui/notifications/tool-input` (`arguments`), and falls back to the values baked in at
call time after two seconds of host silence (see `bridge.py`).

Each card has an id derived from its title, so the model can name the card again from
the conversation text alone. The card's button (experiment E3) sends `tools/call` for
`card_clicked` with that id to the host and shows what the host answered. A second
`tool-result` in the same frame (experiment E4, `update_card`) re-renders the card and
says so in the status line.
"""

import re

from mcp_app_server.bridge import APP_MIME_TYPE, APPS_PROTOCOL_VERSION, app_page

__all__ = [
    "APP_MIME_TYPE",
    "APPS_PROTOCOL_VERSION",
    "CARD_URI",
    "TEMPLATE_BODY",
    "TEMPLATE_TITLE",
    "card_html",
    "card_id_for",
]

CARD_URI = "ui://mcp-app/card"

TEMPLATE_TITLE = "MCP App card"
TEMPLATE_BODY = "Waiting for a show_card result from the host."

_STYLE = """\
  .card { margin: 8px; padding: 16px 20px; border: 1px solid #8884; border-radius: 12px; }
  .card h1 { margin: 0 0 8px; font-size: 18px; font-weight: 600; }
  .card p { margin: 0; white-space: pre-wrap; }
  .card footer { display: flex; align-items: center; gap: 10px; margin-top: 12px; }
  .card button { font: inherit; font-size: 13px; padding: 4px 10px; border-radius: 6px; }
  .card .status { font-size: 13px; opacity: 0.8; }
"""

_BODY = """\
<article class="card">
  <h1 id="card-title"></h1>
  <p id="card-body"></p>
  <footer>
    <button type="button" id="card-click">Record a click</button>
    <span class="status" id="card-status" role="status"></span>
  </footer>
</article>
"""

_SCRIPT = """\
  var titleEl = document.getElementById("card-title");
  var bodyEl = document.getElementById("card-body");
  var statusEl = document.getElementById("card-status");
  var cardId = baked.card_id;

  function render(values) {
    if (!values || typeof values !== "object") return;
    if (typeof values.card_id === "string" && values.card_id) cardId = values.card_id;
    if (typeof values.title === "string" && values.title) titleEl.textContent = values.title;
    if (typeof values.body === "string") bodyEl.textContent = values.body;
    if (!titleEl.textContent && cardId) titleEl.textContent = "Card " + cardId;
    app.resize();
  }

  function showStatus(text) {
    statusEl.textContent = text;
    app.resize();
  }

  // E3: the app calls a tool on its own server through the host, by the server's name.
  document.getElementById("card-click").addEventListener("click", function () {
    showStatus("Sending tools/call card_clicked...");
    app.request("tools/call", { name: "card_clicked", arguments: { card_id: cardId } }).then(
      function (result) { showStatus("Host answered: " + firstText(result)); },
      function (error) { showStatus("Host refused tools/call: " + errorText(error)); }
    );
  });

  var results = 0;

  var app = mcpApp({ name: "mcp-app-card", version: "0.1.0" }, {
    toolInput: render,
    toolResult: function (result) {
      results += 1;
      render(result.structuredContent);
      if (results > 1) showStatus("Updated by a later tool result (" + results + ")");
    },
    fallback: function () { render(baked); }
  });
"""


def card_id_for(title: str) -> str:
    """The card's id: its title in lowercase, runs of other characters as one hyphen, at
    most 40 characters. The card titled "Hello World" is `hello-world`."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return slug or "card"


def card_html(
    title: str = TEMPLATE_TITLE, body: str = TEMPLATE_BODY, card_id: str | None = None
) -> str:
    """The card page with its id, `title` and `body` baked in as its fallback values."""
    return app_page(
        title="MCP App card",
        style=_STYLE,
        body=_BODY,
        script=_SCRIPT,
        data={
            "card_id": card_id if card_id is not None else card_id_for(title),
            "title": title,
            "body": body,
        },
    )
