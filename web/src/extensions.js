// The page extension API (docs/proposals/platform.md, "Page extension API"). A project's
// manifest may name a same-origin ES module; its default export receives the `guppi`
// object built here and registers renderers and send hooks on it. The registry holds no
// DOM of its own: app.js hands each renderer the reply's `reply-attachments` element.
// Renderers write plain text or sandboxed iframes into that slot, never model or user
// text through innerHTML.
//
// Built-in renderers ship with the page and are switched on by the manifest's
// `capabilities`; `mcp-apps` enables the MCP Apps host (web/src/mcp-apps/host.js). A
// built-in sees events and tool calls before a project's renderers do.

import { createMcpAppsHost } from "./mcp-apps/host.js";

// AG-UI event types a project renderer can take. The page renders none of these itself
// except the tool call pair, whose built-in status line a renderer can replace: the page
// skips its own line for a tool call whose start or end event a renderer took.
export const EXTENSION_EVENT_TYPES = [
  "TOOL_CALL_START",
  "TOOL_CALL_END",
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

/** True when the manifest lists `name` among its capabilities. */
export function hasCapability(project, name) {
  return Boolean(project) && Array.isArray(project.capabilities) && project.capabilities.includes(name);
}

// Built-in renderer factories by capability name.
const BUILTIN_RENDERERS = { "mcp-apps": createMcpAppsHost };

/**
 * Builds the `guppi` object for one page load plus the page's side of it. `project` is
 * the manifest (null on the default project); `getToken` returns the current access
 * token. `builtins` maps capability names to renderer factories; tests pass fakes.
 */
export function createExtensionHost({ project, getToken, builtins = BUILTIN_RENDERERS }) {
  // Each built-in is `{ tool(toolCall, slot, ctx), event(event, slot, ctx) }`; `event`
  // returns true when it claims the event.
  const builtinRenderers = Object.entries(builtins)
    .filter(([capability]) => hasCapability(project, capability))
    .map(([, create]) => create());
  const toolRenderers = new Map();
  const eventRenderers = new Map();
  const sendHooks = [];
  const threadHooks = [];
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
    // Called with { threadId } on the first load, a new chat, and a resumed or switched
    // thread, so an extension can drop state it kept for the previous thread.
    onThread(fn) {
      if (typeof fn === "function") threadHooks.push(fn);
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

    /**
     * Hands a tool call to its renderer, if any; called on TOOL_CALL_END and
     * TOOL_CALL_RESULT. True only when a project renderer claims the tool.
     */
    renderTool(toolCall, slot, ctx) {
      // Built-ins watch every tool call and never claim one, so the status line stays the
      // page's unless a project renderer takes the tool.
      for (const builtin of builtinRenderers) {
        try {
          builtin.tool(toolCall, slot, ctx);
        } catch (error) {
          warn("a built-in tool renderer", error);
        }
      }
      const fn = toolRenderers.get(toolCall.name);
      if (!fn) return false;
      try {
        fn(toolCall, slot, ctx);
      } catch (error) {
        warn(`the ${toolCall.name} renderer`, error);
      }
      return true;
    },

    /**
     * Hands an AG-UI event to a built-in that claims it, else to the renderer registered
     * for its type, if any.
     */
    renderEvent(event, slot, ctx) {
      if (event.type === "CUSTOM" && event.name === KEEPALIVE_EVENT_NAME) return false;
      for (const builtin of builtinRenderers) {
        try {
          if (builtin.event(event, slot, ctx)) return true;
        } catch (error) {
          warn("a built-in event renderer", error);
        }
      }
      const fn = eventRenderers.get(event.type);
      if (!fn) return false;
      try {
        fn(event, slot, ctx);
      } catch (error) {
        warn(`the ${event.type} renderer`, error);
      }
      return true;
    },

    /** Tells every onThread hook which thread the page is now on. */
    notifyThread(threadId) {
      for (const hook of threadHooks) {
        try {
          hook({ threadId });
        } catch (error) {
          warn("an onThread hook", error);
        }
      }
    },

    /** Where guppi.status(text) writes: the running reply's status line, or nowhere. */
    setStatusSink(fn) {
      statusSink = typeof fn === "function" ? fn : null;
    },
  };
}
