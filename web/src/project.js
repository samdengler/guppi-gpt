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
 * "platform" or a same-origin /api/<name>/invocations path. Unknown fields are kept for
 * the project's own extension.
 */
export function checkManifest(manifest, name) {
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) return null;
  if (manifest.name !== name) return null;
  if (typeof manifest.label !== "string" || !manifest.label.trim()) return null;
  if (manifest.agent !== "platform" && !PROJECT_AGENT_PATH.test(manifest.agent || "")) {
    return null;
  }
  return manifest;
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
