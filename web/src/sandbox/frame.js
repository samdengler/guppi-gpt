// /sandbox/frame.html's script: the sandbox proxy between the page and an MCP App. The
// page frames this document with sandbox="allow-scripts" and no allow-same-origin, so it
// runs in an opaque origin and cannot reach the page's storage or tokens. The app's HTML
// goes into a nested srcdoc frame sandboxed the same way, which inherits this document's
// CSP (served with the /sandbox/* response headers policy), not the page's.

import { createRelay } from "./relay.js";

// Location keeps the URL's origin even in an opaque-origin document, and frame-ancestors
// 'self' means the parent is always this site.
const hostOrigin = location.origin;
let appFrame = null;

const relay = createRelay({
  toParent(message) {
    window.parent.postMessage(message, hostOrigin);
  },
  loadApp(html) {
    appFrame = document.createElement("iframe");
    appFrame.setAttribute("sandbox", "allow-scripts");
    appFrame.title = "MCP App";
    // The one place any HTML is written: the app's own document, inside the sandbox.
    appFrame.srcdoc = html;
    document.body.appendChild(appFrame);
    // The app's frame has an opaque origin, so "*" is the only target that reaches it.
    return (message) => {
      if (appFrame.contentWindow) appFrame.contentWindow.postMessage(message, "*");
    };
  },
});

window.addEventListener("message", (event) => {
  if (event.source === window.parent && event.origin === hostOrigin) {
    relay.fromParent(event.data);
  } else if (appFrame && event.source === appFrame.contentWindow) {
    relay.fromApp(event.data);
  }
});

if (window.parent !== window) relay.start();
