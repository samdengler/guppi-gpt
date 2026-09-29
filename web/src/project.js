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

export function manifestUrl(name) {
  return `/projects/${name}/manifest.json`;
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
