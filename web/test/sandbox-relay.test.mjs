import { test } from "node:test";
import assert from "node:assert/strict";

import { createRelay, PROXY_READY, RESOURCE_READY } from "../src/sandbox/relay.js";

// A relay with fake ends: what it posted to the host, the apps it loaded, and what it
// posted to the app.
function relay() {
  const toParent = [];
  const loaded = [];
  const toApp = [];
  const r = createRelay({
    toParent: (message) => toParent.push(message),
    loadApp: (html) => {
      loaded.push(html);
      return (message) => toApp.push(message);
    },
  });
  return { r, toParent, loaded, toApp };
}

const resourceReady = (html) => ({ jsonrpc: "2.0", method: RESOURCE_READY, params: { html } });

test("start tells the host the proxy is ready", () => {
  const { r, toParent } = relay();
  r.start();
  assert.deepEqual(toParent, [{ jsonrpc: "2.0", method: PROXY_READY, params: {} }]);
});

test("the first resource-ready loads the app; a second is ignored", () => {
  const { r, loaded } = relay();
  r.fromParent(resourceReady("<p>one</p>"));
  r.fromParent(resourceReady("<p>two</p>"));
  assert.deepEqual(loaded, ["<p>one</p>"]);
});

test("a resource-ready without string HTML loads nothing", () => {
  const { r, loaded } = relay();
  r.fromParent({ jsonrpc: "2.0", method: RESOURCE_READY, params: {} });
  r.fromParent({ jsonrpc: "2.0", method: RESOURCE_READY });
  assert.deepEqual(loaded, []);
});

test("host messages reach the app only once it is loaded", () => {
  const { r, toApp } = relay();
  const response = { jsonrpc: "2.0", id: 1, result: { protocolVersion: "2026-01-26" } };
  r.fromParent(response);
  assert.deepEqual(toApp, []);
  r.fromParent(resourceReady("<p>app</p>"));
  r.fromParent(response);
  assert.deepEqual(toApp, [response]);
});

test("app messages go to the host unchanged", () => {
  const { r, toParent } = relay();
  r.fromParent(resourceReady("<p>app</p>"));
  const init = { jsonrpc: "2.0", id: 1, method: "ui/initialize", params: {} };
  const size = { jsonrpc: "2.0", method: "ui/notifications/size-changed", params: { height: 90 } };
  r.fromApp(init);
  r.fromApp(size);
  assert.deepEqual(toParent, [init, size]);
});

test("reserved sandbox methods and non JSON-RPC messages are never relayed", () => {
  const { r, toParent, toApp } = relay();
  r.fromParent(resourceReady("<p>app</p>"));
  r.fromApp({ jsonrpc: "2.0", method: PROXY_READY, params: {} });
  r.fromApp({ jsonrpc: "2.0", method: RESOURCE_READY, params: { html: "<p>x</p>" } });
  r.fromApp("text");
  r.fromApp({ method: "ui/initialize" });
  r.fromApp(null);
  r.fromParent({ jsonrpc: "2.0", method: "ui/notifications/sandbox-other", params: {} });
  r.fromParent({ id: 1, result: {} });
  assert.deepEqual(toParent, []);
  assert.deepEqual(toApp, []);
});
