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
