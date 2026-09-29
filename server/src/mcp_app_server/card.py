"""The card page: one self-contained HTML document served as an MCP Apps UI resource.

The page follows the MCP Apps extension (SEP-1865, spec revision 2026-01-26): it sends
`ui/initialize` to its host over `postMessage`, answers the host's result with
`ui/notifications/initialized`, and renders the title and body from
`ui/notifications/tool-result` (`structuredContent`) or `ui/notifications/tool-input`
(`arguments`). When no host message arrives within two seconds it renders the values baked
into the page, so the same document works as an embedded resource in a tool result, as a
`resources/read` result, and opened on its own. It loads nothing and calls nothing; every
value reaches the page through `textContent`.
"""

import json

CARD_URI = "ui://mcp-app/card"

# The MCP Apps profile of text/html; hosts render a ui:// resource only under this type.
APP_MIME_TYPE = "text/html;profile=mcp-app"

# The spec revision the page names in ui/initialize.
APPS_PROTOCOL_VERSION = "2026-01-26"

TEMPLATE_TITLE = "MCP App card"
TEMPLATE_BODY = "Waiting for a show_card result from the host."

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MCP App card</title>
<style>
  :root { color-scheme: light dark; }
  body { margin: 0; font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
  .card { margin: 8px; padding: 16px 20px; border: 1px solid #8884; border-radius: 12px; }
  .card h1 { margin: 0 0 8px; font-size: 18px; font-weight: 600; }
  .card p { margin: 0; white-space: pre-wrap; }
</style>
</head>
<body>
<article class="card">
  <h1 id="card-title"></h1>
  <p id="card-body"></p>
</article>
<script type="application/json" id="card-data">__CARD_DATA__</script>
<script>
(function () {
  "use strict";
  var baked = JSON.parse(document.getElementById("card-data").textContent);
  var titleEl = document.getElementById("card-title");
  var bodyEl = document.getElementById("card-body");
  var heard = false;
  var nextId = 1;
  var initId = null;

  function post(message) {
    if (window.parent && window.parent !== window) {
      message.jsonrpc = "2.0";
      window.parent.postMessage(message, "*");
    }
  }

  function render(values) {
    if (!values || typeof values !== "object") return;
    if (typeof values.title === "string") titleEl.textContent = values.title;
    if (typeof values.body === "string") bodyEl.textContent = values.body;
    var box = document.documentElement.getBoundingClientRect();
    post({
      method: "ui/notifications/size-changed",
      params: { width: Math.ceil(box.width), height: Math.ceil(box.height) }
    });
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    var message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (initId !== null && message.id === initId && !message.method) {
      heard = true;
      post({ method: "ui/notifications/initialized", params: {} });
      return;
    }
    if (message.method === "ui/notifications/tool-input") {
      heard = true;
      render(message.params && message.params.arguments);
    } else if (message.method === "ui/notifications/tool-result") {
      heard = true;
      render(message.params && message.params.structuredContent);
    }
  });

  if (window.parent && window.parent !== window) {
    initId = nextId++;
    post({
      id: initId,
      method: "ui/initialize",
      params: {
        appInfo: { name: "mcp-app-card", version: "0.1.0" },
        appCapabilities: {},
        protocolVersion: "__APPS_PROTOCOL_VERSION__"
      }
    });
  }

  setTimeout(function () {
    if (!heard) render(baked);
  }, 2000);
})();
</script>
</body>
</html>
"""


def _script_json(value: dict[str, str]) -> str:
    """JSON that is safe inside a <script> element: no `<`, `>` or `&` survives, so the
    values cannot close the element or open a comment."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def card_html(title: str = TEMPLATE_TITLE, body: str = TEMPLATE_BODY) -> str:
    """The card page with `title` and `body` baked in as its fallback values."""
    return _PAGE.replace("__APPS_PROTOCOL_VERSION__", APPS_PROTOCOL_VERSION).replace(
        "__CARD_DATA__", _script_json({"title": title, "body": body})
    )
