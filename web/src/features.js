import { OpenFeature } from "@openfeature/web-sdk";
import { parseOverrideParam, overlayFlags } from "./flags-core.js";

const OVERRIDE_KEY = "guppigpt_ff_overrides";

// A static provider: every value comes from the flags object computed once at
// initFeatures time (config.json's features overlaid with the tab's overrides). The
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
    return JSON.parse(sessionStorage.getItem(OVERRIDE_KEY) || "{}");
  } catch {
    return {};
  }
}

// Read the `ff` param, store it, and strip it from the URL the way finishSignIn strips
// `code` (history.replaceState, no reload). Stored in sessionStorage so the override
// set survives the sign-in redirect to Cognito and back, which lands on a bare URL.
// A `ff` param, present or empty, replaces the whole stored override set; its absence
// leaves whatever this tab already stored in place.
function applyUrlOverrides() {
  const params = new URLSearchParams(location.search);
  if (!params.has("ff")) return readStoredOverrides();
  const overrides = parseOverrideParam(params.get("ff") || "");
  sessionStorage.setItem(OVERRIDE_KEY, JSON.stringify(overrides));
  params.delete("ff");
  const query = params.toString();
  history.replaceState(null, "", location.pathname + (query ? `?${query}` : "") + location.hash);
  return overrides;
}

/**
 * Set the flags provider from config.json's features overlaid with this tab's
 * overrides. Returns the merged flags object; app.js awaits this before first render
 * and uses the result for the body's data-features attribute.
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
