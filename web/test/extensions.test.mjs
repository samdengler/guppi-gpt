import { test } from "node:test";
import assert from "node:assert/strict";

import { createExtensionHost, EXTENSION_EVENT_TYPES, hasCapability } from "../src/extensions.js";

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

test("mountSurface hands the element to every onSurface hook, past one that throws or rejects", async () => {
  const { guppi, mountSurface } = host();
  const element = { id: "surface-screen" };
  const seen = [];
  guppi.onSurface(() => {
    throw new Error("boom");
  });
  guppi.onSurface(async () => {
    throw new Error("async boom");
  });
  guppi.onSurface((el) => seen.push(el));
  guppi.onSurface("not a function");
  const original = console.warn;
  let warnings = 0;
  console.warn = () => {
    warnings += 1;
  };
  try {
    mountSurface(element);
    await new Promise((resolve) => setTimeout(resolve, 0));
  } finally {
    console.warn = original;
  }
  assert.deepEqual(seen, [element]);
  assert.equal(warnings, 2);
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

// Built-in renderers, switched on by the manifest's capabilities. A fake factory stands in
// for the MCP Apps host, which is covered in mcp-apps-host.test.mjs.
function builtinHost(capabilities) {
  const seen = { created: 0, tools: [], events: [] };
  const builtins = {
    "mcp-apps": () => {
      seen.created += 1;
      return {
        tool: (toolCall, slot) => {
          seen.tools.push([toolCall.id, slot]);
          return false;
        },
        event: (event, slot) => {
          seen.events.push([event.name, slot]);
          return event.name === "mcp-app/resource";
        },
      };
    },
  };
  const project = { ...MANIFEST, capabilities };
  return { seen, ...createExtensionHost({ project, getToken: () => "t", builtins }) };
}

test("hasCapability reads the manifest's capabilities list", () => {
  assert.equal(hasCapability({ capabilities: ["mcp-apps"] }, "mcp-apps"), true);
  assert.equal(hasCapability({ capabilities: [] }, "mcp-apps"), false);
  assert.equal(hasCapability({ capabilities: "mcp-apps" }, "mcp-apps"), false);
  assert.equal(hasCapability({}, "mcp-apps"), false);
  assert.equal(hasCapability(null, "mcp-apps"), false);
});

test("the mcp-apps built-in is created only when the manifest asks for it", () => {
  assert.equal(builtinHost(["mcp-apps"]).seen.created, 1);
  assert.equal(builtinHost([]).seen.created, 0);
  assert.equal(builtinHost(undefined).seen.created, 0);
  const defaultProject = createExtensionHost({ project: null, getToken: () => "t" });
  assert.equal(defaultProject.renderEvent({ type: "CUSTOM", name: "mcp-app/resource" }, {}, {}), false);
});

test("a built-in claims its CUSTOM event ahead of a project renderer", () => {
  const { seen, guppi, renderEvent } = builtinHost(["mcp-apps"]);
  const projectEvents = [];
  guppi.renderers.event("CUSTOM", (event) => projectEvents.push(event.name));
  const slot = {};
  assert.equal(renderEvent({ type: "CUSTOM", name: "mcp-app/resource" }, slot, {}), true);
  assert.equal(renderEvent({ type: "CUSTOM", name: "progress" }, slot, {}), true);
  assert.deepEqual(seen.events, [
    ["mcp-app/resource", slot],
    ["progress", slot],
  ]);
  assert.deepEqual(projectEvents, ["progress"]);
  // The keepalive reaches no renderer, built-in or not.
  assert.equal(renderEvent({ type: "CUSTOM", name: "ping" }, slot, {}), false);
  assert.equal(seen.events.length, 2);
});

test("a built-in watches every tool call without claiming it", () => {
  const { seen, guppi, renderTool } = builtinHost(["mcp-apps"]);
  const slot = {};
  assert.equal(renderTool({ id: "c1", name: "mcp-app___show_card" }, slot, {}), false);
  guppi.renderers.tool("demo___chart", () => {});
  assert.equal(renderTool({ id: "c2", name: "demo___chart" }, slot, {}), true);
  assert.deepEqual(seen.tools, [
    ["c1", slot],
    ["c2", slot],
  ]);
});

test("a built-in that throws logs one warning and the project renderer still runs", () => {
  const builtins = {
    "mcp-apps": () => ({
      tool: () => {
        throw new Error("boom");
      },
      event: () => {
        throw new Error("boom");
      },
    }),
  };
  const ext = createExtensionHost({ project: { ...MANIFEST, capabilities: ["mcp-apps"] }, getToken: () => "t", builtins });
  let rendered = 0;
  ext.guppi.renderers.event("CUSTOM", () => {
    rendered += 1;
  });
  const warnings = countWarnings(() => {
    assert.equal(ext.renderEvent({ type: "CUSTOM", name: "x" }, {}, {}), true);
    assert.equal(ext.renderTool({ id: "c", name: "t" }, {}, {}), false);
  });
  assert.equal(warnings, 2);
  assert.equal(rendered, 1);
});

test("tool call start and end events reach a project renderer, which claims them", () => {
  const { guppi, renderEvent } = host();
  assert.ok(EXTENSION_EVENT_TYPES.includes("TOOL_CALL_START"));
  assert.ok(EXTENSION_EVENT_TYPES.includes("TOOL_CALL_END"));
  const seen = [];
  guppi.renderers.event("TOOL_CALL_START", (event, slot, ctx) => {
    seen.push([event.toolCallName, slot, ctx.threadId]);
    guppi.status("Asking the Pay agent…");
  });
  const claimed = renderEvent(
    { type: "TOOL_CALL_START", toolCallId: "c1", toolCallName: "delegate_pay" },
    "slot",
    { threadId: "t1" },
  );
  assert.equal(claimed, true);
  assert.deepEqual(seen, [["delegate_pay", "slot", "t1"]]);
  // Without a renderer for the type the page keeps its own status line.
  assert.equal(host().renderEvent({ type: "TOOL_CALL_END", toolCallId: "c1" }, "slot", {}), false);
});

test("the render context's setLabel is what a renderer calls to relabel the reply", () => {
  const { guppi, renderEvent } = host();
  let label = "Guppi";
  guppi.renderers.event("STEP_STARTED", (event, slot, ctx) => ctx.setLabel(`HR Assistant · ${event.stepName}`));
  renderEvent({ type: "STEP_STARTED", stepName: "Pay" }, "slot", {
    setLabel: (text) => {
      label = text;
    },
  });
  assert.equal(label, "HR Assistant · Pay");
});

test("onThread hooks hear every thread change, and a throwing hook is skipped", () => {
  const { guppi, notifyThread } = host();
  const heard = [];
  guppi.onThread(({ threadId }) => heard.push(threadId));
  guppi.onThread(() => {
    throw new Error("boom");
  });
  guppi.onThread(({ threadId }) => heard.push(`second:${threadId}`));
  const warnings = countWarnings(() => {
    notifyThread("t1");
    notifyThread("t2");
  });
  assert.deepEqual(heard, ["t1", "second:t1", "t2", "second:t2"]);
  assert.equal(warnings, 2);
});

test("an onSend hook can set the run's state, which the page sends as AG-UI state", () => {
  const { guppi, applySendHooks } = host();
  guppi.onSend((input) => ({ ...input, state: { activeDomain: "pay", pendingAction: null } }));
  const out = applySendHooks({ threadId: "t1", runId: "r1", messages: [], forwardedProps: {}, state: {} });
  assert.deepEqual(out.state, { activeDomain: "pay", pendingAction: null });
  assert.equal(out.threadId, "t1");
});
