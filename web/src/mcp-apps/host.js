// The built-in MCP Apps host (docs/proposals/platform-phase-3.md), enabled by a manifest's
// `capabilities: ["mcp-apps"]` and registered by extensions.js. It renders a tool call's
// UI resource in the reply's `reply-attachments` slot and runs the host half of the
// bridge. Message names and shapes follow the MCP Apps extension, spec revision
// 2026-01-26, as published in modelcontextprotocol/ext-apps 2.0.3 (commit 82221c0).
//
// The resource arrives from the platform agent as a CUSTOM event named `mcp-app/resource`
// right after the tool call's TOOL_CALL_RESULT (agent/src/guppi_agent/agent.py), or inside
// a TOOL_CALL_RESULT whose content is a whole MCP CallToolResult. The HTML goes to the
// sandbox proxy (/sandbox/frame.html, web/src/sandbox/) by postMessage and is written only
// there, into a nested frame; the page itself never writes HTML.

export const RESOURCE_EVENT_NAME = "mcp-app/resource";
export const APP_MIME_TYPE = "text/html;profile=mcp-app";
export const SANDBOX_URL = "/sandbox/frame.html";
export const PROTOCOL_VERSION = "2026-01-26";
export const EXT_APPS_VERSION = "2.0.3";
export const HOST_INFO = Object.freeze({ name: "guppi-gpt", version: "0.1.0" });

export const DEFAULT_HEIGHT = 160;
export const MIN_HEIGHT = 40;
export const MAX_HEIGHT = 640;

const PROXY_READY = "ui/notifications/sandbox-proxy-ready";
const RESOURCE_READY = "ui/notifications/sandbox-resource-ready";

// JSON-RPC error codes: the standard method-not-found, and the extension's
// implementation-defined code for a refused request.
export const METHOD_NOT_FOUND = -32601;
export const REFUSED = -32000;

// What this host offers an app: opening links, nothing proxied to the server yet.
export const HOST_CAPABILITIES = Object.freeze({ openLinks: {} });

export function isAppMimeType(mimeType) {
  return typeof mimeType === "string" && mimeType.replace(/\s/g, "").toLowerCase() === APP_MIME_TYPE;
}

function isObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isUiResource({ uri, mimeType, text }) {
  return typeof uri === "string" && uri.startsWith("ui://") && isAppMimeType(mimeType) && typeof text === "string";
}

/** The resource an `mcp-app/resource` CUSTOM event carries, or null. */
export function resourceFromEvent(event) {
  if (!event || event.type !== "CUSTOM" || event.name !== RESOURCE_EVENT_NAME) return null;
  const value = event.value;
  if (!isObject(value) || typeof value.toolCallId !== "string" || !value.toolCallId) return null;
  if (!isUiResource(value)) return null;
  return {
    toolCallId: value.toolCallId,
    uri: value.uri,
    mimeType: value.mimeType,
    text: value.text,
    toolResult: isObject(value.toolResult) ? value.toolResult : null,
  };
}

/**
 * The resource a tool call's result carries, when TOOL_CALL_RESULT's content is a whole
 * MCP CallToolResult with an embedded ui:// resource, else null. The platform agent's
 * adapter sends only the last text item today, which is never such an object, so this
 * path waits for an adapter or a project agent that sends the whole result.
 */
export function resourceFromToolResult(toolCall) {
  if (!toolCall || typeof toolCall.result !== "string") return null;
  let result;
  try {
    result = JSON.parse(toolCall.result);
  } catch {
    return null;
  }
  if (!isObject(result) || !Array.isArray(result.content)) return null;
  const wanted = isObject(result._meta) && isObject(result._meta.ui) ? result._meta.ui.resourceUri : undefined;
  for (const block of result.content) {
    if (!isObject(block) || block.type !== "resource" || !isObject(block.resource)) continue;
    const { uri, mimeType, text } = block.resource;
    if (!isUiResource({ uri, mimeType, text }) || (wanted && uri !== wanted)) continue;
    return { toolCallId: toolCall.id, uri, mimeType, text, toolResult: result };
  }
  return null;
}

/** A tool call's arguments as an object; AG-UI hands them over as a JSON string. */
export function toolArguments(toolCall) {
  const args = toolCall && toolCall.args;
  if (isObject(args)) return args;
  if (typeof args !== "string" || !args) return {};
  try {
    const parsed = JSON.parse(args);
    return isObject(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** Only http and https links leave the page. */
export function linkToOpen(url) {
  if (typeof url !== "string") return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" || parsed.protocol === "http:" ? parsed.href : null;
  } catch {
    return null;
  }
}

export function clampHeight(height) {
  if (typeof height !== "number" || !Number.isFinite(height)) return null;
  // An app that measures itself before its sandboxed frame has been laid out (the
  // frame's viewport is still 0 by 0 in that first task) reports 0; that is not a size,
  // so the frame keeps its current height until a real measurement arrives.
  if (height <= 0) return null;
  return Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Math.ceil(height)));
}

/**
 * The host half of the bridge for one app, without the DOM. `send(message)` posts to the
 * sandbox proxy; `receive(message)` takes what the proxy relays from the app. `toolCall`
 * is the page's record of the call (its `args` arrive before the resource does);
 * `onResize(height)` and `openLink(url)` are the page's side effects.
 */
export function createBridge({ send, resource, toolCall, hostContext, onResize, openLink }) {
  let resourceSent = false;
  let initialized = false;

  const respond = (id, result) => send({ jsonrpc: "2.0", id, result });
  const fail = (id, code, message) => send({ jsonrpc: "2.0", id, error: { code, message } });
  const notify = (method, params) => send({ jsonrpc: "2.0", method, params });

  function toolResult() {
    if (resource.toolResult && Array.isArray(resource.toolResult.content)) return resource.toolResult;
    return { content: [] };
  }

  function handleRequest({ id, method, params }) {
    switch (method) {
      case "ui/initialize": {
        const requested = isObject(params) ? params.protocolVersion : undefined;
        respond(id, {
          protocolVersion: requested === PROTOCOL_VERSION ? requested : PROTOCOL_VERSION,
          hostInfo: HOST_INFO,
          hostCapabilities: HOST_CAPABILITIES,
          hostContext,
        });
        return;
      }
      case "ui/open-link": {
        const url = linkToOpen(isObject(params) ? params.url : undefined);
        if (!url) {
          fail(id, REFUSED, "Invalid URL");
          return;
        }
        openLink(url);
        respond(id, {});
        return;
      }
      case "ping":
        respond(id, {});
        return;
      case "tools/call":
        // Phase 4 decides how an app's tool calls reach the server.
        fail(id, METHOD_NOT_FOUND, "tools/call is not available on this host yet");
        return;
      default:
        fail(id, METHOD_NOT_FOUND, `Method not found: ${method}`);
    }
  }

  function handleNotification({ method, params }) {
    if (method === PROXY_READY) {
      if (resourceSent) return;
      resourceSent = true;
      notify(RESOURCE_READY, { html: resource.text });
    } else if (method === "ui/notifications/initialized") {
      // Nothing goes to the app before it says it is initialized, and the tool input and
      // result go once each.
      if (initialized) return;
      initialized = true;
      notify("ui/notifications/tool-input", { arguments: toolArguments(toolCall) });
      notify("ui/notifications/tool-result", toolResult());
    } else if (method === "ui/notifications/size-changed") {
      const height = clampHeight(isObject(params) ? params.height : undefined);
      if (height !== null) onResize(height);
    }
    // Other notifications (notifications/message included) are accepted and ignored.
  }

  return {
    receive(message) {
      if (!isObject(message) || message.jsonrpc !== "2.0") return;
      if (typeof message.method !== "string") return; // a response: this host sends no requests
      if (typeof message.id === "string" || typeof message.id === "number") handleRequest(message);
      else handleNotification(message);
    },
  };
}

function hostContextFor(win) {
  const dark = Boolean(win.matchMedia && win.matchMedia("(prefers-color-scheme: dark)").matches);
  const context = {
    theme: dark ? "dark" : "light",
    displayMode: "inline",
    availableDisplayModes: ["inline"],
    containerDimensions: { maxHeight: MAX_HEIGHT },
    platform: "web",
  };
  const locale = win.navigator && win.navigator.language;
  if (locale) context.locale = locale;
  return context;
}

/**
 * The built-in renderer: `tool(toolCall)` watches every tool call of the page (it never
 * claims one, so the status line stays the page's), and `event(event, slot)` claims the
 * `mcp-app/resource` CUSTOM event. `doc` and `win` default to the page's own and are
 * read only when an app is mounted.
 */
export function createMcpAppsHost({ doc, win } = {}) {
  const toolCalls = new Map();
  const mounted = new Set();
  const bridges = new Map();
  let listening = false;

  function listen(w) {
    if (listening) return;
    listening = true;
    w.addEventListener("message", (event) => {
      const bridge = bridges.get(event.source);
      if (bridge) bridge.receive(event.data);
    });
  }

  function mount(resource, slot) {
    if (!slot || mounted.has(resource.toolCallId)) return;
    const d = doc || globalThis.document;
    const w = win || globalThis.window;
    mounted.add(resource.toolCallId);
    listen(w);

    const frame = d.createElement("iframe");
    frame.className = "mcp-app-frame";
    // An opaque origin: the app cannot reach the page's storage, cookies or tokens.
    frame.setAttribute("sandbox", "allow-scripts");
    frame.title = `MCP App ${resource.uri}`;
    frame.height = String(DEFAULT_HEIGHT);
    frame.src = SANDBOX_URL;
    slot.appendChild(frame);

    const bridge = createBridge({
      // The proxy's origin is opaque, so "*" is the only target that reaches it; the
      // message listener above accepts only what comes from this frame's window.
      send: (message) => frame.contentWindow && frame.contentWindow.postMessage(message, "*"),
      resource,
      toolCall: toolCalls.get(resource.toolCallId),
      hostContext: hostContextFor(w),
      onResize: (height) => {
        frame.height = String(height);
      },
      openLink: (url) => {
        w.open(url, "_blank", "noopener,noreferrer");
      },
    });
    bridges.set(frame.contentWindow, bridge);
  }

  return {
    tool(toolCall, slot) {
      if (!toolCall || !toolCall.id) return false;
      toolCalls.set(toolCall.id, toolCall);
      const resource = resourceFromToolResult(toolCall);
      if (resource) mount(resource, slot);
      return false;
    },

    event(event, slot) {
      const resource = resourceFromEvent(event);
      if (!resource) return false;
      mount(resource, slot);
      return true;
    },
  };
}
