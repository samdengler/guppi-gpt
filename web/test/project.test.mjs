import { test } from "node:test";
import assert from "node:assert/strict";

import {
  acceptedReturnPath,
  agentUrlFor,
  brandFor,
  checkManifest,
  manifestUrl,
  mergeFeatures,
  projectPath,
  resolveProject,
  suggestionsFor,
  themeFor,
  wantsWarmStart,
  warmDue,
  warmRunInput,
  WARM_STALE_MS,
  MAX_SUGGESTIONS,
  connectChatFor,
  wantsConnectChat,
  CONNECT_CHAT_LINES,
  CONNECT_TURN_LIMIT_MAX_MS,
} from "../src/project.js";

const DEMO = { name: "demo", label: "Demo", agent: "platform" };

// ---- resolveProject: the project name from the page path ----

test("resolveProject reads the name from /p/<name>/", () => {
  assert.equal(resolveProject("/p/demo/"), "demo");
  assert.equal(resolveProject("/p/mcp-app/"), "mcp-app");
  assert.equal(resolveProject("/p/demo/index.html"), "demo");
});

test("resolveProject returns null for the root page and anything else", () => {
  for (const pathname of [
    "/",
    "/index.html",
    "/p/demo",
    "/p/",
    "/p/Demo/",
    "/p/de_mo/",
    "/p/demo/x/",
    "/projects/demo/",
    "",
    undefined,
  ]) {
    assert.equal(resolveProject(pathname), null, String(pathname));
  }
});

test("manifestUrl is the project's manifest under /projects/", () => {
  assert.equal(manifestUrl("demo"), "/projects/demo/manifest.json");
});

// ---- checkManifest: what makes a manifest usable ----

test("checkManifest accepts a platform manifest and keeps unknown fields", () => {
  const manifest = { ...DEMO, extra: { anything: 1 } };
  assert.equal(checkManifest(manifest, "demo"), manifest);
});

test("checkManifest accepts a project agent path on this origin", () => {
  const manifest = { ...DEMO, agent: "/api/demo/invocations" };
  assert.equal(checkManifest(manifest, "demo"), manifest);
});

test("checkManifest refuses a manifest for another project", () => {
  assert.equal(checkManifest(DEMO, "other"), null);
});

test("checkManifest refuses a missing label or agent", () => {
  assert.equal(checkManifest({ name: "demo", agent: "platform" }, "demo"), null);
  assert.equal(checkManifest({ ...DEMO, label: "  " }, "demo"), null);
  assert.equal(checkManifest({ name: "demo", label: "Demo" }, "demo"), null);
});

test("checkManifest refuses an agent anywhere but this origin's /api/", () => {
  for (const agent of [
    "https://example.com/api/demo/invocations",
    "//example.com/api/demo/invocations",
    "/api/demo/invocations?x=1",
    "/other/demo/invocations",
    "api/demo/invocations",
  ]) {
    assert.equal(checkManifest({ ...DEMO, agent }, "demo"), null, agent);
  }
});

test("checkManifest refuses what is not an object", () => {
  for (const value of [null, undefined, "demo", 1, [DEMO]]) {
    assert.equal(checkManifest(value, "demo"), null);
  }
});

// ---- mergeFeatures: manifest features over config features ----

test("mergeFeatures lays the manifest's booleans over config.features", () => {
  const config = { siteUrl: "https://chat.dengler.io/", features: { history: false, rum: true } };
  const merged = mergeFeatures(config, { ...DEMO, features: { history: true, feedback: false } });
  assert.deepEqual(merged, {
    siteUrl: "https://chat.dengler.io/",
    features: { history: true, rum: true, feedback: false },
  });
});

test("mergeFeatures ignores values that are not booleans", () => {
  const config = { features: { history: false } };
  const merged = mergeFeatures(config, { ...DEMO, features: { history: "yes", rum: 1 } });
  assert.deepEqual(merged.features, { history: false });
});

test("mergeFeatures leaves config unchanged without a manifest and never mutates it", () => {
  const config = { features: { history: false } };
  assert.deepEqual(mergeFeatures(config, null), config);
  mergeFeatures(config, { ...DEMO, features: { history: true } });
  assert.deepEqual(config, { features: { history: false } });
  assert.deepEqual(mergeFeatures({}, DEMO), { features: {} });
});

// ---- brandFor and agentUrlFor ----

test("brandFor is GuppiGPT for the default project", () => {
  assert.deepEqual(brandFor(null), { label: "GuppiGPT", assistant: "GuppiGPT" });
});

test("brandFor takes the label, and the assistant when given", () => {
  assert.deepEqual(brandFor(DEMO), { label: "Demo", assistant: "Demo" });
  assert.deepEqual(brandFor({ ...DEMO, assistant: "Guppi" }), { label: "Demo", assistant: "Guppi" });
  assert.deepEqual(brandFor({ ...DEMO, assistant: " " }), { label: "Demo", assistant: "Demo" });
});

test("agentUrlFor maps platform and the default project to /api/invocations", () => {
  assert.equal(agentUrlFor(null), "/api/invocations");
  assert.equal(agentUrlFor(DEMO), "/api/invocations");
  assert.equal(agentUrlFor({ ...DEMO, agent: "/api/demo/invocations" }), "/api/demo/invocations");
});

// ---- the sign-in round trip: the path carried in the OAuth state ----

test("projectPath is /p/<name>/ for a project and / for the default", () => {
  assert.equal(projectPath("demo"), "/p/demo/");
  assert.equal(projectPath(null), "/");
});

test("acceptedReturnPath accepts / and /p/<name>/", () => {
  assert.equal(acceptedReturnPath("/"), "/");
  assert.equal(acceptedReturnPath("/p/demo/"), "/p/demo/");
  assert.equal(acceptedReturnPath("/p/mcp-app/"), "/p/mcp-app/");
});

test("acceptedReturnPath turns anything else into /", () => {
  for (const state of [
    null,
    undefined,
    "",
    "https://example.com/",
    "//example.com/",
    "/\\example.com",
    "/p/demo",
    "/p/demo/index.html",
    "/p/Demo/",
    "/p/demo/../../x",
    "/p/demo/?x=1",
    "/flags.html",
    "/projects/demo/manifest.json",
    "javascript:alert(1)",
  ]) {
    assert.equal(acceptedReturnPath(state), "/", String(state));
  }
});

test("the path a sign-in starts from is always accepted on the way back", () => {
  for (const name of ["demo", "mcp-app", null]) {
    assert.equal(acceptedReturnPath(projectPath(name)), projectPath(name));
  }
});

test("suggestionsFor keeps well-formed entries, trimmed and capped", () => {
  const manifest = {
    suggestions: [
      { label: "  Show a card ", prompt: " Show me a card titled Hello " },
      { label: "", prompt: "no label" },
      { label: "no prompt" },
      "a string",
      null,
      { label: "x".repeat(80), prompt: "y".repeat(600) },
    ],
  };
  const out = suggestionsFor(manifest);
  assert.deepEqual(out[0], { label: "Show a card", prompt: "Show me a card titled Hello" });
  assert.equal(out.length, 2);
  assert.equal(out[1].label.length, 48);
  assert.equal(out[1].prompt.length, 500);
});

test("suggestionsFor is empty for the default project and for a bad field", () => {
  assert.deepEqual(suggestionsFor(null), []);
  assert.deepEqual(suggestionsFor({ suggestions: "Show a card" }), []);
  assert.deepEqual(suggestionsFor({ suggestions: { label: "a", prompt: "b" } }), []);
  const many = Array.from({ length: 10 }, (_, i) => ({ label: `s${i}`, prompt: `p${i}` }));
  assert.equal(suggestionsFor({ suggestions: many }).length, MAX_SUGGESTIONS);
});

test("themeFor maps hex colors per scheme and drops the rest", () => {
  const theme = themeFor({
    theme: {
      light: { accent: "#C8102E", brand: " #003a70 ", bg: "red", fg: 12, bogus: "#fff" },
      dark: { accent: "#ff5a6e" },
    },
  });
  assert.deepEqual(theme, {
    light: { "--accent": "#c8102e", "--brand": "#003a70" },
    dark: { "--accent": "#ff5a6e" },
  });
});

test("themeFor takes a flat object as the light scheme and falls back to it for dark", () => {
  assert.deepEqual(themeFor({ theme: { accent: "#abc" } }), {
    light: { "--accent": "#abc" },
    dark: { "--accent": "#abc" },
  });
  assert.equal(themeFor(null), null);
  assert.equal(themeFor({ theme: "#c8102e" }), null);
  assert.equal(themeFor({ theme: { light: { bg: "url(x)" } } }), null);
});

test("wantsWarmStart reads the warm-start capability", () => {
  assert.equal(wantsWarmStart({ ...DEMO, capabilities: ["warm-start"] }), true);
  assert.equal(wantsWarmStart({ ...DEMO, capabilities: ["mcp-apps"] }), false);
  assert.equal(wantsWarmStart({ ...DEMO, capabilities: "warm-start" }), false);
  assert.equal(wantsWarmStart(null), false);
});

test("warmRunInput has no messages and marks the run warm", () => {
  assert.deepEqual(warmRunInput({ threadId: "t1", runId: "r1", manifest: { name: "hr" } }), {
    threadId: "t1",
    runId: "r1",
    messages: [],
    tools: [],
    context: [],
    state: {},
    forwardedProps: { project: "hr", warm: true },
  });
  assert.deepEqual(warmRunInput({ threadId: "t1", runId: "r1", manifest: null }).forwardedProps, { warm: true });
});

// ---- warmDue: when the page sends a warm start (guppi-hr D50) ----

const READY = { wanted: true, signedIn: true, visible: true, empty: true, warmed: false, warmedAt: 0, now: 1_000_000 };

test("warmDue warms a new, empty thread as soon as the signed-in page is in view", () => {
  assert.equal(warmDue(READY), true);
});

test("warmDue waits for the page to be in view, signed in, and wanting a warm start", () => {
  assert.equal(warmDue({ ...READY, visible: false }), false);
  assert.equal(warmDue({ ...READY, signedIn: false }), false);
  assert.equal(warmDue({ ...READY, wanted: false }), false);
});

test("warmDue never warms a thread with messages", () => {
  assert.equal(warmDue({ ...READY, empty: false }), false);
});

test("warmDue warms a thread once, and again when the employee comes back after WARM_STALE_MS", () => {
  const warmed = { ...READY, warmed: true, warmedAt: READY.now };
  assert.equal(warmDue({ ...warmed, now: READY.now + WARM_STALE_MS - 1 }), false);
  assert.equal(warmDue({ ...warmed, now: READY.now + WARM_STALE_MS }), true);
});

test("warmRunInput names the thread the page left, when there is one", () => {
  const input = warmRunInput({ threadId: "t2", runId: "r1", manifest: { name: "hr" }, previousThreadId: "t1" });
  assert.deepEqual(input.forwardedProps, { project: "hr", warm: true, previousThreadId: "t1" });
});

// ---- connectChatFor: the Connect chat transport's rules from the manifest ----

const CONNECT = {
  name: "hr",
  label: "HR Assistant",
  agent: "/api/hr/invocations",
  capabilities: ["warm-start", "connect-chat"],
  connectChat: {
    start: "/api/hr/chat/start",
    report: "/api/hr/chat/report",
    endMark: "⁣",
    closedMark: "⁤",
    hiddenPrefix: "[flow]",
    quietAfterMs: 800,
    turnLimitMs: 28000,
    maxChars: 1024,
    lines: { noReply: "No answer came back from the HR assistant. Try again in a moment." },
  },
};

test("connectChatFor reads the block of a project with the connect-chat capability", () => {
  const rules = connectChatFor(CONNECT);
  assert.equal(wantsConnectChat(CONNECT), true);
  assert.equal(rules.start, "/api/hr/chat/start");
  assert.equal(rules.report, "/api/hr/chat/report");
  assert.equal(rules.endMark, "⁣");
  assert.equal(rules.closedMark, "⁤");
  assert.equal(rules.hiddenPrefix, "[flow]");
  assert.deepEqual([rules.quietAfterMs, rules.turnLimitMs, rules.maxChars], [800, 28000, 1024]);
  assert.equal(rules.lines.noReply, "No answer came back from the HR assistant. Try again in a moment.");
  // A line the manifest leaves out keeps the platform's wording.
  assert.equal(rules.lines.restarted, CONNECT_CHAT_LINES.restarted);
  assert.ok(Object.isFrozen(rules) && Object.isFrozen(rules.lines));
});

test("connectChatFor is null without the capability, the block, or a same-origin start route", () => {
  assert.equal(connectChatFor(null), null);
  assert.equal(connectChatFor({ ...CONNECT, capabilities: ["warm-start"] }), null);
  assert.equal(connectChatFor({ ...CONNECT, connectChat: undefined }), null);
  assert.equal(connectChatFor({ ...CONNECT, connectChat: [] }), null);
  for (const start of ["https://evil.example/api/chat/start", "//evil.example/api/x", "/api/../x", "/api/hr", "/other/chat/start", 7]) {
    assert.equal(connectChatFor({ ...CONNECT, connectChat: { ...CONNECT.connectChat, start } }), null, String(start));
  }
});

test("connectChatFor drops an unusable report route, marks, numbers and lines, keeping the defaults", () => {
  const rules = connectChatFor({
    ...CONNECT,
    connectChat: {
      start: "/api/hr/chat/start",
      report: "https://evil.example/report",
      endMark: "far too long a mark",
      closedMark: 4,
      hiddenPrefix: "",
      quietAfterMs: -1,
      turnLimitMs: 60000,
      maxChars: 5000,
      lines: { noReply: "", signin: 3, ended: "x".repeat(501), restarted: "(New chat.)" },
    },
  });
  assert.equal(rules.report, null);
  assert.equal(rules.endMark, "");
  assert.equal(rules.closedMark, "");
  assert.equal(rules.hiddenPrefix, "");
  assert.deepEqual([rules.quietAfterMs, rules.turnLimitMs, rules.maxChars], [800, 28000, 1024]);
  assert.equal(rules.lines.noReply, CONNECT_CHAT_LINES.noReply);
  assert.equal(rules.lines.signin, CONNECT_CHAT_LINES.signin);
  assert.equal(rules.lines.ended, CONNECT_CHAT_LINES.ended);
  assert.equal(rules.lines.restarted, "(New chat.)");
  // The turn limit stays under the page's 30 s stall timer.
  assert.ok(CONNECT_TURN_LIMIT_MAX_MS < 30000);
});

test("the HR manifest's own block is usable", async () => {
  const { readFile } = await import("node:fs/promises");
  const path = new URL("../../../guppi-hr/connect/web/manifest.json", import.meta.url);
  let manifest;
  try {
    manifest = JSON.parse(await readFile(path, "utf8"));
  } catch {
    return; // guppi-hr is not checked out beside this repository
  }
  const rules = connectChatFor(checkManifest(manifest, "hr"));
  assert.ok(rules);
  assert.equal(rules.endMark, "⁣");
  assert.equal(rules.report, "/api/hr/chat/report");
});
