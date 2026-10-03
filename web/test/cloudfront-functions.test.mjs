// The two CloudFront Functions in infra/guppi_gpt_infra/functions/, run against sample
// URIs. Each file is a CloudFront Functions script with a top-level handler, so it is
// evaluated as a function body and its handler returned.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

function load(name) {
  const url = new URL(`../../infra/guppi_gpt_infra/functions/${name}`, import.meta.url);
  return new Function(`${readFileSync(url, "utf8")}\nreturn handler;`)();
}

function rewrite(handler, uri) {
  return handler({ request: { uri, method: "GET", headers: {} } }).uri;
}

test("page path: a project page is served from /index.html", () => {
  const handler = load("page-path.js");
  assert.equal(rewrite(handler, "/p/demo/"), "/index.html");
  assert.equal(rewrite(handler, "/p/demo/index.html"), "/index.html");
  assert.equal(rewrite(handler, "/p/mcp-app2/"), "/index.html");
});

test("page path: every other uri is left alone", () => {
  const handler = load("page-path.js");
  for (const uri of [
    "/",
    "/index.html",
    "/app.js",
    "/p/demo",
    "/p/demo/app.js",
    "/p/Demo/",
    "/p/de_mo/",
    "/p/demo/x/",
    "/p//",
    "/projects/demo/manifest.json",
    "/flags.html",
  ]) {
    assert.equal(rewrite(handler, uri), uri, uri);
  }
});

test("agent path: a project's invocations reach the gateway target of that name", () => {
  const handler = load("agent-path.js");
  assert.equal(rewrite(handler, "/api/demo/invocations"), "/demo/invocations");
  assert.equal(rewrite(handler, "/api/mcp-app/invocations"), "/mcp-app/invocations");
});

test("agent path: the platform agent, feedback and reserved names are left alone", () => {
  const handler = load("agent-path.js");
  for (const uri of [
    "/api/invocations",
    "/api/feedback",
    "/api/feedback/invocations",
    "/api/invocations/invocations",
    "/api/demo/invocations/x",
    "/api/Demo/invocations",
    "/api/demo/ping",
  ]) {
    assert.equal(rewrite(handler, uri), uri, uri);
  }
});

test("page path: a renamed project's old page redirects to its new one", () => {
  const handler = load("page-path.js");
  for (const uri of ["/p/hr-connect/", "/p/hr-connect/index.html"]) {
    const response = handler({ request: { uri, method: "GET", headers: {} } });
    assert.equal(response.statusCode, 301, uri);
    assert.equal(response.headers.location.value, "/p/hr/", uri);
  }
  // The new name is served as any project is.
  assert.equal(rewrite(handler, "/p/hr/"), "/index.html");
});
