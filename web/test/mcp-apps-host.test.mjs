import { test } from "node:test";
import assert from "node:assert/strict";

import {
  createBridge,
  createMcpAppsHost,
  DEFAULT_HEIGHT,
  HOST_CAPABILITIES,
  HOST_INFO,
  MAX_HEIGHT,
  METHOD_NOT_FOUND,
  PROTOCOL_VERSION,
  REFUSED,
  resourceFromEvent,
  resourceFromToolResult,
  SANDBOX_URL,
  toolArguments,
} from "../src/mcp-apps/host.js";

const HTML = "<!DOCTYPE html><html><body><p>card</p></body></html>";
const TOOL_RESULT = {
  content: [{ type: "text", text: "Showed the user a card titled 'Hello'." }],
  structuredContent: { title: "Hello", body: "It works" },
  _meta: { ui: { resourceUri: "ui://mcp-app/card" } },
};

function resourceEvent(overrides = {}) {
  return {
    type: "CUSTOM",
    name: "mcp-app/resource",
    value: {
      toolCallId: "tool-1",
      uri: "ui://mcp-app/card",
      mimeType: "text/html;profile=mcp-app",
      text: HTML,
      toolResult: TOOL_RESULT,
      ...overrides,
    },
  };
}

const TOOL_CALL = {
  id: "tool-1",
  name: "mcp-app___show_card",
  args: '{"title": "Hello", "body": "It works"}',
  result: JSON.stringify(HTML),
};

// A bridge with a fake postMessage: every message it sends lands in `sent`.
function bridge(overrides = {}) {
  const sent = [];
  const resized = [];
  const opened = [];
  const b = createBridge({
    send: (message) => sent.push(message),
    resource: resourceFromEvent(resourceEvent()),
    toolCall: TOOL_CALL,
    hostContext: { theme: "light", displayMode: "inline" },
    onResize: (height) => resized.push(height),
    openLink: (url) => opened.push(url),
    ...overrides,
  });
  return { b, sent, resized, opened };
}

const rpc = (message) => ({ jsonrpc: "2.0", ...message });

test("the CUSTOM event's value is the resource", () => {
  assert.deepEqual(resourceFromEvent(resourceEvent()), {
    toolCallId: "tool-1",
    uri: "ui://mcp-app/card",
    mimeType: "text/html;profile=mcp-app",
    text: HTML,
    toolResult: TOOL_RESULT,
  });
  assert.equal(resourceFromEvent(resourceEvent({ toolResult: undefined })).toolResult, null);
});

test("other CUSTOM events and malformed values carry no resource", () => {
  assert.equal(resourceFromEvent({ type: "CUSTOM", name: "ping", value: {} }), null);
  assert.equal(resourceFromEvent({ ...resourceEvent(), type: "STATE_SNAPSHOT" }), null);
  assert.equal(resourceFromEvent(resourceEvent({ mimeType: "text/html" })), null);
  assert.equal(resourceFromEvent(resourceEvent({ uri: "https://example.com/card" })), null);
  assert.equal(resourceFromEvent(resourceEvent({ text: 7 })), null);
  assert.equal(resourceFromEvent(resourceEvent({ toolCallId: "" })), null);
  assert.equal(resourceFromEvent({ type: "CUSTOM", name: "mcp-app/resource", value: "x" }), null);
});

test("a tool result that is a whole CallToolResult with a ui:// resource carries it", () => {
  const whole = {
    ...TOOL_RESULT,
    content: [
      ...TOOL_RESULT.content,
      { type: "resource", resource: { uri: "ui://mcp-app/card", mimeType: "text/html;profile=mcp-app", text: HTML } },
    ],
  };
  const resource = resourceFromToolResult({ id: "tool-9", result: JSON.stringify(whole) });
  assert.equal(resource.toolCallId, "tool-9");
  assert.equal(resource.text, HTML);
  assert.deepEqual(resource.toolResult, whole);
});

test("today's TOOL_CALL_RESULT content, the HTML as a JSON string, is not claimed", () => {
  assert.equal(resourceFromToolResult(TOOL_CALL), null);
  assert.equal(resourceFromToolResult({ id: "t", result: "not json" }), null);
  assert.equal(resourceFromToolResult({ id: "t" }), null);
});

test("tool arguments come from the AG-UI JSON string", () => {
  assert.deepEqual(toolArguments(TOOL_CALL), { title: "Hello", body: "It works" });
  assert.deepEqual(toolArguments({ args: "{broken" }), {});
  assert.deepEqual(toolArguments({ args: "[1]" }), {});
  assert.deepEqual(toolArguments(undefined), {});
});

test("proxy ready is answered with the app's HTML, once", () => {
  const { b, sent } = bridge();
  b.receive(rpc({ method: "ui/notifications/sandbox-proxy-ready", params: {} }));
  b.receive(rpc({ method: "ui/notifications/sandbox-proxy-ready", params: {} }));
  assert.deepEqual(sent, [rpc({ method: "ui/notifications/sandbox-resource-ready", params: { html: HTML } })]);
});

test("ui/initialize gets the host's version, info, capabilities and context", () => {
  const { b, sent } = bridge();
  b.receive(
    rpc({
      id: 1,
      method: "ui/initialize",
      params: { appInfo: { name: "card", version: "0.1.0" }, appCapabilities: {}, protocolVersion: PROTOCOL_VERSION },
    }),
  );
  assert.deepEqual(sent, [
    rpc({
      id: 1,
      result: {
        protocolVersion: "2026-01-26",
        hostInfo: HOST_INFO,
        hostCapabilities: HOST_CAPABILITIES,
        hostContext: { theme: "light", displayMode: "inline" },
      },
    }),
  ]);
  assert.deepEqual(HOST_CAPABILITIES, { openLinks: {} });
});

test("an unknown protocol version is answered with the host's own", () => {
  const { b, sent } = bridge();
  b.receive(rpc({ id: "a", method: "ui/initialize", params: { protocolVersion: "2099-01-01" } }));
  assert.equal(sent[0].result.protocolVersion, PROTOCOL_VERSION);
  assert.equal(sent[0].id, "a");
});

test("nothing reaches the app before initialized; then the tool input and result, once", () => {
  const { b, sent } = bridge();
  b.receive(rpc({ id: 1, method: "ui/initialize", params: {} }));
  assert.equal(sent.length, 1);
  b.receive(rpc({ method: "ui/notifications/initialized", params: {} }));
  b.receive(rpc({ method: "ui/notifications/initialized", params: {} }));
  assert.deepEqual(sent.slice(1), [
    rpc({ method: "ui/notifications/tool-input", params: { arguments: { title: "Hello", body: "It works" } } }),
    rpc({ method: "ui/notifications/tool-result", params: TOOL_RESULT }),
  ]);
});

test("without a tool result from the agent the app still gets an empty one", () => {
  const resource = resourceFromEvent(resourceEvent({ toolResult: undefined }));
  const { b, sent } = bridge({ resource, toolCall: undefined });
  b.receive(rpc({ method: "ui/notifications/initialized" }));
  assert.deepEqual(sent, [
    rpc({ method: "ui/notifications/tool-input", params: { arguments: {} } }),
    rpc({ method: "ui/notifications/tool-result", params: { content: [] } }),
  ]);
});

test("size-changed resizes within the bounds", () => {
  const { b, resized, sent } = bridge();
  b.receive(rpc({ method: "ui/notifications/size-changed", params: { width: 600, height: 97.2 } }));
  b.receive(rpc({ method: "ui/notifications/size-changed", params: { height: 5000 } }));
  b.receive(rpc({ method: "ui/notifications/size-changed", params: { height: 1 } }));
  b.receive(rpc({ method: "ui/notifications/size-changed", params: { width: 0, height: 0 } }));
  b.receive(rpc({ method: "ui/notifications/size-changed", params: { height: -3 } }));
  b.receive(rpc({ method: "ui/notifications/size-changed", params: { height: "tall" } }));
  b.receive(rpc({ method: "ui/notifications/size-changed" }));
  assert.deepEqual(resized, [98, MAX_HEIGHT, 40]);
  assert.deepEqual(sent, []);
});

test("open-link opens http and https links and refuses the rest", () => {
  const { b, sent, opened } = bridge();
  b.receive(rpc({ id: 1, method: "ui/open-link", params: { url: "https://example.com/a?b=c" } }));
  b.receive(rpc({ id: 2, method: "ui/open-link", params: { url: "javascript:alert(1)" } }));
  b.receive(rpc({ id: 3, method: "ui/open-link", params: {} }));
  assert.deepEqual(opened, ["https://example.com/a?b=c"]);
  assert.deepEqual(sent, [
    rpc({ id: 1, result: {} }),
    rpc({ id: 2, error: { code: REFUSED, message: "Invalid URL" } }),
    rpc({ id: 3, error: { code: REFUSED, message: "Invalid URL" } }),
  ]);
});

test("tools/call is refused with a JSON-RPC error in this phase", () => {
  const { b, sent } = bridge();
  b.receive(rpc({ id: 7, method: "tools/call", params: { name: "mcp-app___show_card", arguments: {} } }));
  assert.deepEqual(sent, [
    rpc({ id: 7, error: { code: METHOD_NOT_FOUND, message: "tools/call is not available on this host yet" } }),
  ]);
});

test("ping is answered; unknown requests get method not found; responses and junk are ignored", () => {
  const { b, sent } = bridge();
  b.receive(rpc({ id: 1, method: "ping" }));
  b.receive(rpc({ id: 2, method: "resources/read", params: { uri: "ui://x" } }));
  b.receive(rpc({ id: 3, result: {} }));
  b.receive(rpc({ method: "notifications/message", params: { level: "info", data: "hi" } }));
  b.receive({ id: 4, method: "ping" });
  b.receive("ping");
  b.receive(null);
  assert.deepEqual(sent, [
    rpc({ id: 1, result: {} }),
    rpc({ id: 2, error: { code: METHOD_NOT_FOUND, message: "Method not found: resources/read" } }),
  ]);
});

// The renderer with a fake DOM: iframes are plain objects whose contentWindow records what
// the page posts, and the window keeps its message listener so a test can play the proxy.
function fakePage() {
  const frames = [];
  const listeners = [];
  const opened = [];
  const doc = {
    createElement(tag) {
      assert.equal(tag, "iframe");
      const posted = [];
      const frame = {
        attributes: {},
        setAttribute(name, value) {
          this.attributes[name] = value;
        },
        contentWindow: null,
        posted,
      };
      frames.push(frame);
      return frame;
    },
  };
  const slot = {
    children: [],
    appendChild(child) {
      this.children.push(child);
      child.contentWindow = { postMessage: (message, target) => child.posted.push([message, target]) };
    },
  };
  const win = {
    addEventListener(type, fn) {
      assert.equal(type, "message");
      listeners.push(fn);
    },
    open: (...args) => opened.push(args),
    matchMedia: () => ({ matches: true }),
    navigator: { language: "en-US" },
  };
  const deliver = (source, data) => listeners.forEach((fn) => fn({ source, data }));
  return { doc, win, slot, frames, listeners, opened, deliver };
}

test("the resource event mounts one sandboxed frame on /sandbox/frame.html", () => {
  const page = fakePage();
  const host = createMcpAppsHost({ doc: page.doc, win: page.win });
  assert.equal(host.tool({ ...TOOL_CALL, result: undefined }, page.slot), false);
  assert.equal(host.tool(TOOL_CALL, page.slot), false);
  assert.equal(page.frames.length, 0);

  assert.equal(host.event(resourceEvent(), page.slot), true);
  assert.equal(host.event(resourceEvent(), page.slot), true);
  const [frame] = page.frames;
  assert.equal(page.frames.length, 1);
  assert.deepEqual(page.slot.children, [frame]);
  assert.equal(frame.attributes.sandbox, "allow-scripts");
  assert.equal(frame.src, SANDBOX_URL);
  assert.equal(frame.height, String(DEFAULT_HEIGHT));
  assert.equal(frame.className, "mcp-app-frame");
  assert.equal(page.listeners.length, 1);
  assert.equal(host.event({ type: "CUSTOM", name: "other", value: {} }, page.slot), false);
});

test("the frame's messages drive the whole handshake; other windows are ignored", () => {
  const page = fakePage();
  const host = createMcpAppsHost({ doc: page.doc, win: page.win });
  host.tool(TOOL_CALL, page.slot);
  host.event(resourceEvent(), page.slot);
  const [frame] = page.frames;
  const methods = () => frame.posted.map(([message]) => message.method || `response ${message.id}`);

  page.deliver({}, rpc({ method: "ui/notifications/sandbox-proxy-ready", params: {} }));
  assert.deepEqual(frame.posted, []);

  page.deliver(frame.contentWindow, rpc({ method: "ui/notifications/sandbox-proxy-ready", params: {} }));
  page.deliver(frame.contentWindow, rpc({ id: 1, method: "ui/initialize", params: { protocolVersion: PROTOCOL_VERSION } }));
  page.deliver(frame.contentWindow, rpc({ method: "ui/notifications/initialized", params: {} }));
  page.deliver(frame.contentWindow, rpc({ method: "ui/notifications/size-changed", params: { height: 120 } }));
  page.deliver(frame.contentWindow, rpc({ id: 2, method: "ui/open-link", params: { url: "https://example.com/" } }));

  assert.deepEqual(methods(), [
    "ui/notifications/sandbox-resource-ready",
    "response 1",
    "ui/notifications/tool-input",
    "ui/notifications/tool-result",
    "response 2",
  ]);
  assert.ok(frame.posted.every(([, target]) => target === "*"));
  assert.equal(frame.posted[0][0].params.html, HTML);
  const context = frame.posted[1][0].result.hostContext;
  assert.equal(context.theme, "dark");
  assert.equal(context.locale, "en-US");
  assert.deepEqual(context.containerDimensions, { maxHeight: MAX_HEIGHT });
  assert.deepEqual(frame.posted[2][0].params.arguments, { title: "Hello", body: "It works" });
  assert.equal(frame.height, "120");
  assert.deepEqual(page.opened, [["https://example.com/", "_blank", "noopener,noreferrer"]]);
});

test("a whole CallToolResult in TOOL_CALL_RESULT mounts the app without the event", () => {
  const page = fakePage();
  const host = createMcpAppsHost({ doc: page.doc, win: page.win });
  const whole = {
    content: [{ type: "resource", resource: { uri: "ui://a/b", mimeType: "text/html;profile=mcp-app", text: HTML } }],
  };
  host.tool({ id: "tool-2", name: "x", args: "{}", result: JSON.stringify(whole) }, page.slot);
  assert.equal(page.frames.length, 1);
  // The event for the same call then finds it already mounted.
  host.event(resourceEvent({ toolCallId: "tool-2" }), page.slot);
  assert.equal(page.frames.length, 1);
});
