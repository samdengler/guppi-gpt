"""The app half of the MCP Apps bridge and the page shell every UI resource here shares.

Each page is one self-contained HTML document (MCP Apps extension, SEP-1865, spec
revision 2026-01-26). `mcpApp()` sends `ui/initialize` to the host over `postMessage`,
answers the host's result with `ui/notifications/initialized`, hands
`ui/notifications/tool-input` and `ui/notifications/tool-result` to the page, and matches
the host's responses to the page's own requests (`tools/call`, `ui/update-model-context`).
When no host message arrives within two seconds the page renders the values baked in at
call time, so a document works as an embedded resource, as a `resources/read` result, and
opened on its own. Pages load nothing and call nothing over the network; every value
reaches the document through `textContent`.
"""

import json
from typing import Any

# The MCP Apps profile of text/html; hosts render a ui:// resource only under this type.
APP_MIME_TYPE = "text/html;profile=mcp-app"

# The spec revision a page names in ui/initialize.
APPS_PROTOCOL_VERSION = "2026-01-26"

BRIDGE_JS = """
function mcpApp(appInfo, handlers) {
  "use strict";
  var embedded = window.parent && window.parent !== window;
  var nextId = 1;
  var pending = {};
  var heard = false;

  function post(message) {
    if (!embedded) return;
    message.jsonrpc = "2.0";
    window.parent.postMessage(message, "*");
  }

  // A request to the host: resolves with its result, rejects with its JSON-RPC error.
  function request(method, params) {
    return new Promise(function (resolve, reject) {
      if (!embedded) {
        reject({ code: -32000, message: "no MCP Apps host around this page" });
        return;
      }
      var id = nextId++;
      pending[id] = { resolve: resolve, reject: reject };
      post({ id: id, method: method, params: params || {} });
    });
  }

  function resize() {
    var box = document.documentElement.getBoundingClientRect();
    // Before the sandboxed frame has been laid out the viewport is 0 by 0 and the
    // measurement is meaningless; the observer below reports again once it has a size.
    if (box.width <= 0 || box.height <= 0) return;
    post({
      method: "ui/notifications/size-changed",
      params: { width: Math.ceil(box.width), height: Math.ceil(box.height) }
    });
  }

  // The frame's viewport arrives after the first messages in some browsers (Chrome lays
  // the sandboxed frame out on its next frame, not synchronously), so the page reports
  // its size whenever its box changes, not only when its content does.
  if (typeof ResizeObserver === "function") {
    new ResizeObserver(function () { resize(); }).observe(document.documentElement);
  }

  // The host's context: its theme and, under styles.variables, the extension's style
  // variables carrying the host page's palette. Applied to the root element so the
  // page's stylesheet can use var(--color-...) with fallbacks; a later
  // host-context-changed merges over it.
  function applyHostContext(context) {
    if (!context || typeof context !== "object") return;
    var root = document.documentElement;
    if (context.theme === "light" || context.theme === "dark") root.setAttribute("data-theme", context.theme);
    var variables = context.styles && context.styles.variables;
    if (!variables || typeof variables !== "object") return;
    for (var name in variables) {
      if (!Object.prototype.hasOwnProperty.call(variables, name)) continue;
      if (name.indexOf("--") !== 0 || typeof variables[name] !== "string") continue;
      root.style.setProperty(name, variables[name]);
    }
    resize();
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    var message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (!message.method) {
      var waiting = pending[message.id];
      if (!waiting) return;
      delete pending[message.id];
      if (message.error) waiting.reject(message.error);
      else waiting.resolve(message.result);
      return;
    }
    var params = message.params || {};
    if (message.method === "ui/notifications/host-context-changed") {
      applyHostContext(params);
    } else if (message.method === "ui/notifications/tool-input") {
      heard = true;
      if (handlers.toolInput) handlers.toolInput(params.arguments || {});
    } else if (message.method === "ui/notifications/tool-result") {
      heard = true;
      if (handlers.toolResult) handlers.toolResult(params);
    }
  });

  if (embedded) {
    request("ui/initialize", {
      appInfo: appInfo,
      appCapabilities: {},
      protocolVersion: "__APPS_PROTOCOL_VERSION__"
    }).then(function (result) {
      heard = true;
      applyHostContext(result && result.hostContext);
      post({ method: "ui/notifications/initialized", params: {} });
    }, function () {});
  }

  setTimeout(function () {
    if (!heard && handlers.fallback) handlers.fallback();
  }, 2000);

  return { request: request, resize: resize };
}

function errorText(error) {
  if (!error || typeof error !== "object") return String(error);
  return "JSON-RPC error " + error.code + ": " + error.message;
}

function firstText(result) {
  var content = result && result.content;
  if (!content || !content.length) return "(no text)";
  for (var i = 0; i < content.length; i++) {
    if (content[i] && content[i].type === "text") return content[i].text;
  }
  return "(no text)";
}
""".replace("__APPS_PROTOCOL_VERSION__", APPS_PROTOCOL_VERSION)

_SHELL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  /* The host's palette arrives as the extension's style variables (applyHostContext);
     every color here reads one with a fallback for a page opened on its own. */
  :root { color-scheme: light dark; }
  body {
    margin: 0;
    font: var(--font-text-md-size, 15px)/1.5 var(--font-sans, system-ui, -apple-system, "Segoe UI", sans-serif);
    color: var(--color-text-primary, #12263f);
    background: transparent;
  }
  .panel {
    position: relative; overflow: hidden;
    margin: 4px 2px 6px; padding: 14px 20px 16px 24px;
    background: var(--color-background-secondary, #e9f1fc);
    border: 1px solid var(--color-border-primary, #cfe0f5);
    border-radius: var(--border-radius-lg, 14px);
    box-shadow: var(--shadow-sm, 0 1px 2px rgba(0, 0, 0, 0.06));
  }
  .panel::before {
    content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 5px;
    background: var(--color-background-info, #2d7ff9);
  }
  .panel .kicker {
    display: block; margin: 0 0 4px; font-size: 11px; font-weight: 600;
    letter-spacing: .08em; text-transform: uppercase;
    color: var(--color-text-secondary, #587089);
  }
  .panel h1 { margin: 0 0 6px; font-size: var(--font-heading-sm-size, 17px); font-weight: 600; letter-spacing: -.01em; }
  .panel p { margin: 0; }
  .panel button {
    font: inherit; font-size: 13px; font-weight: 600; padding: 6px 14px; border: 0;
    border-radius: var(--border-radius-full, 999px); cursor: pointer;
    background: var(--color-background-info, #2d7ff9);
    color: var(--color-text-inverse, #fff);
  }
  .panel button:hover { filter: brightness(1.08); }
  .panel button:focus-visible { outline: 2px solid var(--color-ring-primary, #2d7ff9); outline-offset: 2px; }
  .panel input, .panel select {
    font: inherit; padding: 6px 10px; color: inherit;
    border: 1px solid var(--color-border-primary, #cfe0f5);
    border-radius: var(--border-radius-md, 10px);
    background: var(--color-background-primary, #fff);
  }
  .panel .status { font-size: 13px; color: var(--color-text-secondary, #587089); }
__STYLE__</style>
</head>
<body>
__BODY__<script type="application/json" id="app-data">__DATA__</script>
<script>
__BRIDGE__
(function () {
  "use strict";
  var baked = JSON.parse(document.getElementById("app-data").textContent);
__SCRIPT__})();
</script>
</body>
</html>
"""


def script_json(value: Any) -> str:
    """JSON that is safe inside a <script> element: no `<`, `>` or `&` survives, so the
    values cannot close the element or open a comment."""
    return (
        json.dumps(value, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def app_page(*, title: str, style: str, body: str, script: str, data: Any) -> str:
    """One app document: the shell, the bridge, the page's own markup and script, and
    `data` as the page's baked values (`baked` in the script). The data goes in last, so
    nothing a caller passes can be read as a placeholder."""
    return (
        _SHELL.replace("__TITLE__", title)
        .replace("__STYLE__", style)
        .replace("__BODY__", body)
        .replace("__BRIDGE__", BRIDGE_JS)
        .replace("__SCRIPT__", script)
        .replace("__DATA__", script_json(data))
    )
