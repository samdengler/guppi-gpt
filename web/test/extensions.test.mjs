import { test } from "node:test";
import assert from "node:assert/strict";

import { createExtensionHost, EXTENSION_EVENT_TYPES } from "../src/extensions.js";

const MANIFEST = { name: "demo", label: "Demo", agent: "platform" };

function host(token = "t") {
  return createExtensionHost({ project: MANIFEST, getToken: () => token });
}

// Silences the one warning a failing hook or renderer logs, and counts it.
function countWarnings(fn) {
  const original = console.warn;
  let count = 0;
  console.warn = () => {
    count += 1;
  };
  try {
    fn();
  } finally {
    console.warn = original;
  }
  return count;
}

test("guppi carries the manifest, the token, and no MCP client in this phase", () => {
  const { guppi } = host("access-token");
  assert.equal(guppi.project, MANIFEST);
  assert.equal(guppi.token(), "access-token");
  assert.equal(guppi.mcp, null);
  assert.ok(Object.isFrozen(guppi));
});

test("a tool renderer is claimed by name and called with the call, the slot and ctx", () => {
  const { guppi, claimsTool, renderTool } = host();
  const calls = [];
  guppi.renderers.tool("demo___chart", (toolCall, slot, ctx) => calls.push([toolCall, slot, ctx]));
  assert.equal(claimsTool("demo___chart"), true);
  assert.equal(claimsTool("docs___Retrieve"), false);
  const slot = {};
  const toolCall = { id: "c1", name: "demo___chart", args: { q: 1 } };
  assert.equal(renderTool(toolCall, slot, { runId: "r" }), true);
  assert.deepEqual(calls, [[toolCall, slot, { runId: "r" }]]);
  assert.equal(renderTool({ id: "c2", name: "docs___Retrieve" }, slot, {}), false);
});

test("an event renderer takes only the extension event types", () => {
  const { guppi, renderEvent } = host();
  const seen = [];
  for (const type of [...EXTENSION_EVENT_TYPES, "TEXT_MESSAGE_CONTENT", "RUN_FINISHED"]) {
    guppi.renderers.event(type, (event) => seen.push(event.type));
  }
  for (const type of [...EXTENSION_EVENT_TYPES, "TEXT_MESSAGE_CONTENT", "RUN_FINISHED"]) {
    renderEvent({ type }, {}, {});
  }
  assert.deepEqual(seen, EXTENSION_EVENT_TYPES);
});

test("the agent's keepalive ping never reaches a CUSTOM renderer", () => {
  const { guppi, renderEvent } = host();
  const names = [];
  guppi.renderers.event("CUSTOM", (event) => names.push(event.name));
  assert.equal(renderEvent({ type: "CUSTOM", name: "ping", value: {} }, {}, {}), false);
  assert.equal(renderEvent({ type: "CUSTOM", name: "chart", value: {} }, {}, {}), true);
  assert.deepEqual(names, ["chart"]);
});

test("onSend hooks run in order and a hook that throws or returns nothing is skipped", () => {
  const { guppi, applySendHooks } = host();
  guppi.onSend((input) => ({ ...input, forwardedProps: { ...input.forwardedProps, a: 1 } }));
  guppi.onSend(() => {
    throw new Error("broken hook");
  });
  guppi.onSend(() => undefined);
  guppi.onSend((input) => ({ ...input, state: { b: 2 } }));
  const input = { runId: "r", forwardedProps: { project: "demo" }, state: {} };
  let result;
  assert.equal(
    countWarnings(() => {
      result = applySendHooks(input);
    }),
    1,
  );
  assert.deepEqual(result, { runId: "r", forwardedProps: { project: "demo", a: 1 }, state: { b: 2 } });
  assert.deepEqual(input, { runId: "r", forwardedProps: { project: "demo" }, state: {} });
});

test("a renderer that throws is still a claim and logs one warning", () => {
  const { guppi, renderTool } = host();
  guppi.renderers.tool("demo___x", () => {
    throw new Error("broken renderer");
  });
  let claimed;
  assert.equal(
    countWarnings(() => {
      claimed = renderTool({ id: "c", name: "demo___x" }, {}, {});
    }),
    1,
  );
  assert.equal(claimed, true);
});

test("guppi.status writes to the current reply's status line, or nowhere", () => {
  const { guppi, setStatusSink } = host();
  guppi.status("before any turn"); // no sink yet: a no-op
  const lines = [];
  setStatusSink((line) => lines.push(line));
  guppi.status("Drawing the chart");
  assert.deepEqual(lines, ["Drawing the chart"]);
});

test("registering something that is not a function is ignored", () => {
  const { guppi, claimsTool, applySendHooks } = host();
  guppi.renderers.tool("demo___x", "not a function");
  guppi.onSend(null);
  assert.equal(claimsTool("demo___x"), false);
  assert.deepEqual(applySendHooks({ a: 1 }), { a: 1 });
});
