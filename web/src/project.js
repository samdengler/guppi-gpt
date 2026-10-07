// Project resolution and the manifest, as pure functions (docs/proposals/platform.md).
// A project's page is /p/<name>/ on the same host; its manifest is
// /projects/<name>/manifest.json. The root page is the default project, the Guppi docs
// agent, and has no manifest: every function here returns today's values for `null`.

export const DEFAULT_LABEL = "GuppiGPT";
export const PLATFORM_AGENT_URL = "/api/invocations";

const PROJECT_PATH = /^\/p\/([a-z0-9-]+)\/(?:index\.html)?$/;
// A project agent answers on this origin through the /api/* behavior, so the bearer
// never leaves it.
const PROJECT_AGENT_PATH = /^\/api\/[a-z0-9-]+\/invocations$/;

/** The project name in a pathname of the form /p/<name>/, else null (the default project). */
export function resolveProject(pathname) {
  const match = PROJECT_PATH.exec(pathname || "");
  return match ? match[1] : null;
}

/** The page path of a project, or / for the default project. */
export function projectPath(name) {
  return name ? `/p/${name}/` : "/";
}

/**
 * The path to return to after sign-in, from the OAuth `state` parameter. Only / and
 * /p/<name>/ are accepted; anything else, a missing state included, becomes /, so the
 * parameter can never send the page somewhere else.
 */
export function acceptedReturnPath(state) {
  if (typeof state !== "string") return "/";
  return /^\/p\/[a-z0-9-]+\/$/.test(state) ? state : "/";
}

export function manifestUrl(name) {
  return `/projects/${name}/manifest.json`;
}

// The home page's "Try a project" cards (docs/proposals/platform.md, "Project list").
// /projects.json names the projects; each card's text comes from that project's own
// manifest, so a project owns its label and description.
export const PROJECTS_URL = "/projects.json";
export const MAX_PROJECT_CARDS = 12;
const MAX_DESCRIPTION = 200;
const PROJECT_NAME = /^[a-z0-9-]+$/;

/** The project names in /projects.json, valid and unique, in the file's order. */
export function projectNames(list) {
  const raw = list && !Array.isArray(list) && typeof list === "object" ? list.projects : null;
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const name of raw) {
    if (out.length === MAX_PROJECT_CARDS) break;
    if (typeof name === "string" && PROJECT_NAME.test(name) && !out.includes(name)) out.push(name);
  }
  return out;
}

// The header's project switcher lists the default project first.
const HOME_DESCRIPTION = "Answers from the GuppiGPT documentation.";

/** The switcher's entries: GuppiGPT, then each project card, with `current` set on the
 * page's own project (null is the default project). */
export function switcherEntries(cards, current) {
  const home = { name: null, label: DEFAULT_LABEL, description: HOME_DESCRIPTION, href: "/" };
  return [home, ...cards].map((entry) => ({ ...entry, current: entry.name === (current || null) }));
}

/** One card for the project `name` from its manifest, or null when the page would not
 * use that manifest. The description is optional plain text, trimmed and capped. */
export function projectCard(manifest, name) {
  const usable = checkManifest(manifest, name);
  if (!usable) return null;
  const description =
    typeof usable.description === "string" ? usable.description.trim().slice(0, MAX_DESCRIPTION) : "";
  return { name, label: usable.label.trim(), description, href: projectPath(name) };
}

/**
 * The manifest when it is usable for the project `name`, else null. Usable means an
 * object whose `name` matches, with a non-empty string `label` and an `agent` that is
 * "platform" or a same-origin /api/<name>/invocations path, or, for a project with its
 * own surface, an `extension` module under /projects/<name>/ and no agent. Unknown fields
 * are kept for the project's own extension.
 */
export function checkManifest(manifest, name) {
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) return null;
  if (manifest.name !== name) return null;
  if (typeof manifest.label !== "string" || !manifest.label.trim()) return null;
  if (manifest.surface === "extension") {
    return typeof manifest.extension === "string" && manifest.extension.startsWith(`/projects/${name}/`)
      ? manifest
      : null;
  }
  if (manifest.agent !== "platform" && !PROJECT_AGENT_PATH.test(manifest.agent || "")) {
    return null;
  }
  return manifest;
}

/**
 * True when the project's extension draws the screen below the header (`"surface":
 * "extension"`): the page signs the employee in and then hands that screen to the
 * extension's onSurface hooks, with no thread, composer, history or agent of its own.
 * guppi-hr's /p/hr-widget/ is the first, hosting AWS's Touchpoint chat widget.
 */
export function hasOwnSurface(manifest) {
  return Boolean(manifest) && manifest.surface === "extension";
}

/**
 * config with manifest.features laid over config.features. Only boolean values are taken
 * from the manifest; browser overrides are applied later, by initFeatures, so they still
 * win. Neither argument is changed.
 */
export function mergeFeatures(config, manifest) {
  const features = { ...(config.features || {}) };
  const projectFeatures = manifest && manifest.features;
  if (projectFeatures && typeof projectFeatures === "object") {
    for (const [name, value] of Object.entries(projectFeatures)) {
      if (typeof value === "boolean") features[name] = value;
    }
  }
  return { ...config, features };
}

/**
 * The page's brand strings. `label` goes on the header, the sign-in card and the tab
 * title; `assistant` on each reply and in the composer placeholder, and defaults to
 * `label`.
 */
export function brandFor(manifest) {
  if (!manifest) return { label: DEFAULT_LABEL, assistant: DEFAULT_LABEL };
  const label = manifest.label.trim();
  const assistant =
    typeof manifest.assistant === "string" && manifest.assistant.trim()
      ? manifest.assistant.trim()
      : label;
  return { label, assistant };
}

/** The path the AG-UI client posts to: "platform" and the default project mean the Guppi agent. */
export function agentUrlFor(manifest) {
  if (!manifest || manifest.agent === "platform") return PLATFORM_AGENT_URL;
  return manifest.agent;
}

/** True when the manifest asks for a warm start ("warm-start" in `capabilities`). */
export function wantsWarmStart(manifest) {
  return Boolean(manifest) && Array.isArray(manifest.capabilities) && manifest.capabilities.includes("warm-start");
}

/**
 * A warm start goes out again for a thread still empty after this long: the agent may hold
 * something for an hour (guppi-hr's Connect contact, whose chat and hop tokens last 60
 * minutes), so a tab left open is warmed again when the employee comes back to it.
 */
export const WARM_STALE_MS = 50 * 60 * 1000;

/**
 * Whether the page sends a warm start now (guppi-hr D50): the project wants one, the
 * employee is signed in with the page in view, and the thread is empty and has not been
 * warmed, or was warmed WARM_STALE_MS ago or more.
 */
export function warmDue({ wanted, signedIn, visible, empty, warmed, warmedAt, now }) {
  if (!wanted || !signedIn || !visible || !empty) return false;
  return !warmed || now - warmedAt >= WARM_STALE_MS;
}

/**
 * The AG-UI run input of a warm start: no messages and `forwardedProps.warm`, which the
 * agent kit answers without a model call (docs/proposals/platform.md, "Warm start").
 */
export function warmRunInput({ threadId, runId, manifest, previousThreadId = null }) {
  return {
    threadId,
    runId,
    messages: [],
    tools: [],
    context: [],
    state: {},
    forwardedProps: {
      ...(manifest ? { project: manifest.name } : {}),
      warm: true,
      ...(previousThreadId ? { previousThreadId } : {}),
    },
  };
}

export const MAX_SUGGESTIONS = 6;
const MAX_SUGGESTION_LABEL = 48;
const MAX_SUGGESTION_PROMPT = 500;

/**
 * The manifest's `suggestions`, checked: up to MAX_SUGGESTIONS entries, each an object
 * with a non-empty string `label` (the pill's text) and `prompt` (what clicking it
 * sends), both trimmed and capped in length. Anything else is dropped, and the default
 * project has none. The page renders them as plain text, never as markup.
 */
export function suggestionsFor(manifest) {
  const raw = manifest && manifest.suggestions;
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const entry of raw) {
    if (out.length === MAX_SUGGESTIONS) break;
    if (!entry || typeof entry !== "object") continue;
    const label = typeof entry.label === "string" ? entry.label.trim() : "";
    const prompt = typeof entry.prompt === "string" ? entry.prompt.trim() : "";
    if (!label || !prompt) continue;
    out.push({
      label: label.slice(0, MAX_SUGGESTION_LABEL),
      prompt: prompt.slice(0, MAX_SUGGESTION_PROMPT),
    });
  }
  return out;
}

/** Manifest theme keys and the page custom properties they set (web/src/app.css). */
export const THEME_KEYS = Object.freeze({
  bg: "--bg",
  fg: "--fg",
  muted: "--muted",
  border: "--border",
  surface: "--surface",
  bubbleUser: "--bubble-user",
  accent: "--accent",
  accentContrast: "--accent-contrast",
  brand: "--brand",
});
const HEX_COLOR = /^#(?:[0-9a-f]{3}|[0-9a-f]{4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;

function themeValues(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const out = {};
  for (const [key, property] of Object.entries(THEME_KEYS)) {
    const value = raw[key];
    if (typeof value === "string" && HEX_COLOR.test(value.trim())) out[property] = value.trim().toLowerCase();
  }
  return Object.keys(out).length ? out : null;
}

/**
 * The manifest's `theme` as custom property values for the light and dark schemes, or
 * null when the manifest sets none. `theme` is `{ light: {...}, dark: {...} }` with the
 * keys of THEME_KEYS, or one flat object taken as the light scheme. Only hex colors are
 * accepted, so a manifest can recolor the page and nothing else; the default project has
 * no theme and keeps the stylesheet's own values.
 */
export function themeFor(manifest) {
  const raw = manifest && manifest.theme;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const light = themeValues(raw.light) || (raw.light === undefined && raw.dark === undefined ? themeValues(raw) : null);
  const dark = themeValues(raw.dark);
  if (!light && !dark) return null;
  return { light: light || {}, dark: dark || light || {} };
}

// ---- The Connect chat transport's rules (guppi-hr D55, web/src/connect-chat.js) ----

/** The lines the transport shows, by key; a project's `connectChat.lines` replaces any. */
export const CONNECT_CHAT_LINES = Object.freeze({
  tooLong: "That message is too long: keep it under the limit, or split it into two messages.",
  noReply: "No answer came back. Try again in a moment.",
  restarted: "(The assistant started a new conversation, so it may ask for details again.)",
  signin: "The assistant could not confirm the sign-in. Try again in a minute.",
  ended: "The conversation ended. A new message here starts a new one.",
  escalated: "The service desk has this conversation now. A new message here starts over with the assistant.",
  error: "The assistant ran into an error and ended this conversation. A new message here starts a new one.",
});
// Connect's SendMessage takes at most 1,024 characters of text/plain.
export const CONNECT_MAX_CHARS = 1024;
// Under the page's 30 s stall timer, which the transport's pings reset anyway.
export const CONNECT_TURN_LIMIT_MAX_MS = 29_000;
const CONNECT_ROUTE = /^\/api\/[a-z0-9-]+(?:\/[a-z0-9-]+)+$/;
const MAX_LINE = 500;
const MAX_MARK = 4;
const MAX_PREFIX = 120;

const textField = (value, max) => (typeof value === "string" && value.trim() && value.length <= max ? value : null);
// A mark is usually one invisible character (U+2063, U+2064), which trim() keeps.
const markField = (value) => (typeof value === "string" && value.length >= 1 && value.length <= MAX_MARK ? value : "");
const numberField = (value, min, max, fallback) =>
  typeof value === "number" && Number.isFinite(value) && value >= min && value <= max ? value : fallback;

/** True when the manifest lists the `connect-chat` capability. */
export function wantsConnectChat(manifest) {
  return Boolean(manifest) && Array.isArray(manifest.capabilities) && manifest.capabilities.includes("connect-chat");
}

/**
 * The manifest's `connectChat` block as the transport's rules, or null when the project
 * does not use the transport or the block has no usable `start` route. `start` and
 * `report` are same-origin /api/... paths (the bearer goes there); marks are one to four
 * characters; prefixes and the legacy end and closed lines are optional strings; the
 * numbers are clamped to their ranges by falling back to the defaults; each line is
 * plain text up to 500 characters, and a missing or unusable one keeps the default.
 */
export function connectChatFor(manifest) {
  if (!wantsConnectChat(manifest)) return null;
  const raw = manifest.connectChat;
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  if (typeof raw.start !== "string" || !CONNECT_ROUTE.test(raw.start)) return null;
  const lines = { ...CONNECT_CHAT_LINES };
  if (raw.lines && typeof raw.lines === "object" && !Array.isArray(raw.lines)) {
    for (const key of Object.keys(CONNECT_CHAT_LINES)) {
      const line = textField(raw.lines[key], MAX_LINE);
      if (line) lines[key] = line;
    }
  }
  return Object.freeze({
    start: raw.start,
    report: typeof raw.report === "string" && CONNECT_ROUTE.test(raw.report) ? raw.report : null,
    endMark: markField(raw.endMark),
    closedMark: markField(raw.closedMark),
    hiddenPrefix: textField(raw.hiddenPrefix, MAX_PREFIX) || "",
    endLine: textField(raw.endLine, MAX_PREFIX) || "",
    closedLine: textField(raw.closedLine, MAX_PREFIX) || "",
    escalationPrefix: textField(raw.escalationPrefix, MAX_PREFIX) || "",
    errorPrefix: textField(raw.errorPrefix, MAX_PREFIX) || "",
    quietAfterMs: numberField(raw.quietAfterMs, 0, 10_000, 800),
    turnLimitMs: numberField(raw.turnLimitMs, 1_000, CONNECT_TURN_LIMIT_MAX_MS, 28_000),
    maxChars: Number.isInteger(raw.maxChars) ? numberField(raw.maxChars, 1, CONNECT_MAX_CHARS, CONNECT_MAX_CHARS) : CONNECT_MAX_CHARS,
    lines: Object.freeze(lines),
  });
}
