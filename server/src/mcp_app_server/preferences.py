"""The preferences form, served as the `ui://mcp-app/preferences` UI resource (experiment
E6).

Two fields, a display name and a reply style. On the button's click the page sends
`ui/update-model-context` to the host, the extension's request for context "used in
future turns" (spec revision 2026-01-26), with the answers as a text block and as
`structuredContent`, and shows what the host answered. The fields sit in a plain element
and the button is `type="button"`: the extension asks hosts for `allow-scripts` and
`allow-same-origin` only, and a frame sandboxed without `allow-forms` never submits a
form, not even to a script's `submit` handler.
"""

from mcp_app_server.bridge import app_page

PREFERENCES_URI = "ui://mcp-app/preferences"

REPLY_STYLES = ("brief", "detailed")

_STYLE = """\
  .form { margin: 8px; padding: 16px 20px; border: 1px solid #8884; border-radius: 12px; }
  .form h1 { margin: 0 0 12px; font-size: 18px; font-weight: 600; }
  .form label { display: block; margin: 0 0 10px; font-size: 14px; }
  .form input, .form select { display: block; margin-top: 4px; font: inherit; }
  .form button { font: inherit; font-size: 13px; padding: 4px 10px; border-radius: 6px; }
  .form .status { margin: 10px 0 0; font-size: 13px; opacity: 0.8; }
"""

_BODY = """\
<section class="form" id="prefs-form">
  <h1>Your preferences</h1>
  <label>Display name <input id="prefs-name" name="display_name" maxlength="60" required></label>
  <label>Reply style
    <select id="prefs-style" name="reply_style">
      <option value="brief">Brief</option>
      <option value="detailed">Detailed</option>
    </select>
  </label>
  <button type="button" id="prefs-submit">Send to the assistant</button>
  <p class="status" id="prefs-status" role="status"></p>
</section>
"""

_SCRIPT = """\
  var nameEl = document.getElementById("prefs-name");
  var styleEl = document.getElementById("prefs-style");
  var statusEl = document.getElementById("prefs-status");

  function showStatus(text) {
    statusEl.textContent = text;
    app.resize();
  }

  var app = mcpApp({ name: "mcp-app-preferences", version: "0.1.0" }, {
    toolInput: function () { app.resize(); },
    toolResult: function () { app.resize(); },
    fallback: function () { app.resize(); }
  });

  // E6: the answers go to the host as model context for the next turn.
  document.getElementById("prefs-submit").addEventListener("click", function () {
    var values = { display_name: nameEl.value.trim(), reply_style: styleEl.value };
    if (baked.reply_styles.indexOf(values.reply_style) < 0) values.reply_style = "brief";
    showStatus("Sending ui/update-model-context...");
    app.request("ui/update-model-context", {
      content: [{
        type: "text",
        text: "The user's preferences: display name " + JSON.stringify(values.display_name) +
          ", reply style " + values.reply_style + "."
      }],
      structuredContent: values
    }).then(
      function () { showStatus("The host accepted the preferences for the next turn."); },
      function (error) { showStatus("Host refused ui/update-model-context: " + errorText(error)); }
    );
  });
"""


def preferences_html() -> str:
    """The preferences form. Its only baked value is the list of reply styles."""
    return app_page(
        title="MCP App preferences",
        style=_STYLE,
        body=_BODY,
        script=_SCRIPT,
        data={"reply_styles": list(REPLY_STYLES)},
    )
