// The inner half of the MCP Apps bridge (docs/proposals/platform-phase-3.md), without the
// DOM so it can be tested: frame.js wires it to postMessage. The sandbox proxy tells the
// host it is ready, takes the app's HTML from exactly one
// `ui/notifications/sandbox-resource-ready` notification, and after that relays JSON-RPC
// messages between the host and the app unchanged, except the reserved
// `ui/notifications/sandbox-*` methods, which are never relayed (ext-apps spec revision
// 2026-01-26, "Sandbox proxy").

export const PROXY_READY = "ui/notifications/sandbox-proxy-ready";
export const RESOURCE_READY = "ui/notifications/sandbox-resource-ready";
const RESERVED_PREFIX = "ui/notifications/sandbox-";

function isJsonRpc(message) {
  return Boolean(message) && typeof message === "object" && message.jsonrpc === "2.0";
}

function isReserved(message) {
  return typeof message.method === "string" && message.method.startsWith(RESERVED_PREFIX);
}

/**
 * `toParent(message)` posts to the host; `loadApp(html)` creates the app's frame and
 * returns the function that posts to it.
 */
export function createRelay({ toParent, loadApp }) {
  let toApp = null;
  return {
    start() {
      toParent({ jsonrpc: "2.0", method: PROXY_READY, params: {} });
    },

    fromParent(message) {
      if (!isJsonRpc(message)) return;
      if (message.method === RESOURCE_READY) {
        // One app per frame: a second resource is ignored rather than replacing the first.
        const html = message.params && message.params.html;
        if (toApp || typeof html !== "string") return;
        toApp = loadApp(html);
        return;
      }
      if (isReserved(message) || !toApp) return;
      toApp(message);
    },

    fromApp(message) {
      if (!isJsonRpc(message) || isReserved(message)) return;
      toParent(message);
    },
  };
}
