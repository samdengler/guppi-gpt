import { OpenFeature } from "@openfeature/web-sdk";
import {
  parseOverrideParam,
  overlayFlags,
  parseStoredOverrides,
  serializeOverrides,
  OVERRIDE_KEY,
} from "./flags-core.js";

// A static provider: every value comes from the flags object computed once at
// initFeatures time (config.json's features overlaid with the browser's overrides). The
// web SDK's evaluation is synchronous, so no network round trip belongs in a resolver.
class StaticFlagsProvider {
  runsOn = "client";
  metadata = { name: "GuppiGPT static flags" };

  constructor(flags) {
    this.flags = flags;
  }

  resolveBooleanEvaluation(flagKey, defaultValue) {
    const value = this.flags[flagKey];
    return { value: typeof value === "boolean" ? value : defaultValue };
  }

  resolveStringEvaluation(_flagKey, defaultValue) {
    return { value: defaultValue };
  }

  resolveNumberEvaluation(_flagKey, defaultValue) {
    return { value: defaultValue };
  }

  resolveObjectEvaluation(_flagKey, defaultValue) {
    return { value: defaultValue };
  }
}

function readStoredOverrides() {
  try {
    return parseStoredOverrides(localStorage.getItem(OVERRIDE_KEY));
  } catch {
    return {};
  }
}

function writeStoredOverrides(overrides) {
  try {
    localStorage.setItem(OVERRIDE_KEY, serializeOverrides(overrides));
  } catch {
    // A private window or storage blocked by the browser: the override still applies
    // for this load, it just will not persist or reach another tab.
  }
}

// Read the `ff` param, store it, and strip it from the URL the way finishSignIn strips
// `code` (history.replaceState, no reload). Stored in localStorage so the override set
// applies to every tab of the browser, survives the sign-in redirect to the issuer and
// back (localStorage, like the sessionStorage this replaced, outlives that redirect;
// unlike it, the same set also now reaches every other open tab), and is still there
// after the tab that set it closes. A `ff` param, present or empty, replaces the whole
// stored override set; its absence leaves whatever is already stored in place.
function applyUrlOverrides() {
  const params = new URLSearchParams(location.search);
  if (!params.has("ff")) return readStoredOverrides();
  const overrides = parseOverrideParam(params.get("ff") || "");
  writeStoredOverrides(overrides);
  params.delete("ff");
  const query = params.toString();
  history.replaceState(null, "", location.pathname + (query ? `?${query}` : "") + location.hash);
  return overrides;
}

// The provider is static: flags are computed once, here, at load. A `storage` event
// fires in every other tab when one tab changes OVERRIDE_KEY (localStorage's own
// cross-tab notification; a tab never receives it for its own write), but re-resolving
// flags from it mid-session would contradict the static provider this file documents
// above. This listener exists only so a session working from the browser console can
// see the moment another tab changed the override set; it changes nothing on the page.
// The honest behavior is simpler than a live update: open tabs pick up a change on
// their next reload, the same way they pick up a new committed default.
if (typeof window !== "undefined") {
  window.addEventListener("storage", (event) => {
    if (event.key === OVERRIDE_KEY) {
      console.debug("guppigpt: feature flag overrides changed in another tab; reload to apply");
    }
  });
}

/**
 * Set the flags provider from config.json's features overlaid with this browser's
 * stored overrides. Returns the merged flags object; app.js awaits this before first
 * render and uses the result for the body's data-features attribute.
 */
export async function initFeatures(config) {
  const overrides = applyUrlOverrides();
  const flags = overlayFlags(config.features || {}, overrides);
  await OpenFeature.setProviderAndWait(new StaticFlagsProvider(flags));
  return flags;
}

/** Wraps OpenFeature.getClient().getBooleanValue with a false default. */
export function isEnabled(name) {
  return OpenFeature.getClient().getBooleanValue(name, false);
}
