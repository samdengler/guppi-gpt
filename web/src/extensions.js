// The page extension API (docs/proposals/platform.md, "Page extension API"). A project's
// manifest may name a same-origin ES module; its default export receives the `guppi`
// object built here and registers renderers and send hooks on it. The registry holds no
// DOM of its own: app.js hands each renderer the reply's `reply-attachments` element.
// Renderers write plain text or sandboxed iframes into that slot, never model or user
// text through innerHTML.

// AG-UI event types a project renderer can take; the page renders none of them itself.
export const EXTENSION_EVENT_TYPES = [
  "CUSTOM",
  "STEP_STARTED",
  "STEP_FINISHED",
  "STATE_SNAPSHOT",
  "STATE_DELTA",
];

// The agent's keepalive (agent/src/guppi_agent/keepalive.py) is transport, not content,
// and never reaches a CUSTOM renderer.
const KEEPALIVE_EVENT_NAME = "ping";

function warn(what, error) {
  console.warn(`guppigpt: ${what} failed`, error);
}

/**
 * Builds the `guppi` object for one page load plus the page's side of it. `project` is
 * the manifest (null on the default project); `getToken` returns the current access
 * token.
 */
export function createExtensionHost({ project, getToken }) {
  const toolRenderers = new Map();
  const eventRenderers = new Map();
  const sendHooks = [];
  let statusSink = null;

  const guppi = Object.freeze({
    project,
    renderers: Object.freeze({
      tool(name, fn) {
        if (typeof name === "string" && typeof fn === "function") toolRenderers.set(name, fn);
      },
      event(type, fn) {
        if (EXTENSION_EVENT_TYPES.includes(type) && typeof fn === "function") {
          eventRenderers.set(type, fn);
        }
      },
    }),
    onSend(fn) {
      if (typeof fn === "function") sendHooks.push(fn);
    },
    status(text) {
      if (statusSink) statusSink(String(text));
    },
    token() {
      return getToken();
    },
    // A browser-side MCP client arrives in a later phase.
    mcp: null,
  });

  return {
    guppi,

    /** True when an extension renders this tool, so the built-in status line stays quiet. */
    claimsTool(name) {
      return toolRenderers.has(name);
    },

    /**
     * Runs every onSend hook in registration order. A hook returns the run input to use
     * next; a hook that throws or returns nothing leaves the input as it was.
     */
    applySendHooks(runInput) {
      let current = runInput;
      for (const hook of sendHooks) {
        try {
          const next = hook(current);
          if (next && typeof next === "object") current = next;
        } catch (error) {
          warn("an onSend hook", error);
        }
      }
      return current;
    },

    /** Hands a tool call to its renderer, if any; called on TOOL_CALL_END and TOOL_CALL_RESULT. */
    renderTool(toolCall, slot, ctx) {
      const fn = toolRenderers.get(toolCall.name);
      if (!fn) return false;
      try {
        fn(toolCall, slot, ctx);
      } catch (error) {
        warn(`the ${toolCall.name} renderer`, error);
      }
      return true;
    },

    /** Hands an AG-UI event to the renderer registered for its type, if any. */
    renderEvent(event, slot, ctx) {
      if (event.type === "CUSTOM" && event.name === KEEPALIVE_EVENT_NAME) return false;
      const fn = eventRenderers.get(event.type);
      if (!fn) return false;
      try {
        fn(event, slot, ctx);
      } catch (error) {
        warn(`the ${event.type} renderer`, error);
      }
      return true;
    },

    /** Where guppi.status(text) writes: the running reply's status line, or nowhere. */
    setStatusSink(fn) {
      statusSink = typeof fn === "function" ? fn : null;
    },
  };
}
