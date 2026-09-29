"""The chart page, served as the `ui://mcp-app/chart` UI resource (experiment E2).

`show_chart` names this resource in `_meta.ui.resourceUri` and does not embed it, so a
host has to fetch the page with `resources/read` and push the values to it. The page
draws one bar per value from `structuredContent.values` (or the `values` argument), with
plain elements and inline heights; before any host message it says it is waiting.
"""

from mcp_app_server.bridge import app_page

CHART_URI = "ui://mcp-app/chart"

_STYLE = """\
  .bars { display: flex; align-items: flex-end; gap: 6px; height: 120px; margin-top: 8px; }
  .bar { flex: 1; min-width: 8px; background: var(--color-background-info, #c8102e); border-radius: 4px 4px 0 0; }
  .labels { display: flex; gap: 6px; font-size: 12px; color: var(--color-text-secondary, #5b6b80); }
  .labels span { flex: 1; min-width: 8px; text-align: center; }
"""

_BODY = """\
<section class="chart panel">
  <span class="kicker">MCP App</span>
  <h1 id="chart-title">Chart</h1>
  <div class="bars" id="chart-bars"></div>
  <div class="labels" id="chart-labels"></div>
</section>
"""

_SCRIPT = """\
  var titleEl = document.getElementById("chart-title");
  var barsEl = document.getElementById("chart-bars");
  var labelsEl = document.getElementById("chart-labels");

  function render(values) {
    if (!Array.isArray(values)) return;
    var numbers = values.filter(function (v) { return typeof v === "number" && isFinite(v); });
    barsEl.textContent = "";
    labelsEl.textContent = "";
    if (!numbers.length) {
      titleEl.textContent = "No values yet";
      app.resize();
      return;
    }
    var top = Math.max.apply(null, numbers.map(Math.abs)) || 1;
    numbers.forEach(function (value) {
      var bar = document.createElement("div");
      bar.className = "bar";
      bar.style.height = Math.max(2, Math.round((Math.abs(value) / top) * 120)) + "px";
      barsEl.appendChild(bar);
      var label = document.createElement("span");
      label.textContent = String(value);
      labelsEl.appendChild(label);
    });
    titleEl.textContent = "Chart of " + numbers.length + " values";
    app.resize();
  }

  var app = mcpApp({ name: "mcp-app-chart", version: "0.1.0" }, {
    toolInput: function (args) { render(args.values); },
    toolResult: function (result) {
      render(result.structuredContent && result.structuredContent.values);
    },
    fallback: function () { render(baked.values); }
  });
"""


def chart_html() -> str:
    """The chart page. It carries no values of its own: the host pushes them."""
    return app_page(
        title="MCP App chart", style=_STYLE, body=_BODY, script=_SCRIPT, data={"values": []}
    )
