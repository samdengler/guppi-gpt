// The static page's half of the MCP Apps bridge (spec revision 2026-01-26): the same
// handshake as the server's inline pages (server/src/mcp_app_server/bridge.py), written
// out here because this page is a file in the site bucket. It says whether a host
// answered, and shows the tool result's text if one arrives.
(function () {
  "use strict";
  var stateEl = document.getElementById("page-state");
  var resultEl = document.getElementById("page-result");
  var embedded = window.parent && window.parent !== window;
  var heard = false;

  function post(message) {
    message.jsonrpc = "2.0";
    window.parent.postMessage(message, "*");
  }

  function resize() {
    var box = document.documentElement.getBoundingClientRect();
    post({
      method: "ui/notifications/size-changed",
      params: { width: Math.ceil(box.width), height: Math.ceil(box.height) }
    });
  }

  if (!embedded) {
    stateEl.textContent = "Opened on its own: no MCP Apps host around this page.";
    return;
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    var message = event.data;
    if (!message || message.jsonrpc !== "2.0") return;
    if (message.id === 1 && !message.method) {
      heard = true;
      stateEl.textContent = "Loaded from its URL inside an MCP Apps host.";
      post({ method: "ui/notifications/initialized", params: {} });
      resize();
    } else if (message.method === "ui/notifications/tool-result") {
      var content = (message.params && message.params.content) || [];
      var text = content.filter(function (c) { return c && c.type === "text"; })[0];
      if (text) resultEl.textContent = text.text;
      resize();
    }
  });

  stateEl.textContent = "Framed; waiting for the host.";
  post({
    id: 1,
    method: "ui/initialize",
    params: {
      appInfo: { name: "mcp-app-static-page", version: "0.1.0" },
      appCapabilities: {},
      protocolVersion: "2026-01-26"
    }
  });
  setTimeout(function () {
    if (!heard) stateEl.textContent = "Framed, but no host answered ui/initialize.";
  }, 2000);
})();
