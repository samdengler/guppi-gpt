// Pure functions behind the feature flag overlay: parsing the `ff` query parameter,
// merging overrides onto the defaults from config.json, reading and writing the stored
// override set, and building the rows the flags page (web/src/flags.js) renders. No DOM
// and no SDK import here, so this file runs under node:test without a browser
// (web/test/features.test.mjs, web/test/flags.test.mjs).

/** localStorage key for the override set, shared by features.js and flags.js: the same
 * set applies to every tab of the browser, not just the one that wrote it. */
export const OVERRIDE_KEY = "guppigpt_ff_overrides";

/** Flag names that observability depends on; the flags page groups these separately
 * and says they are not meant to be turned off, though a tester still can. */
export const OPERATIONAL_FLAGS = new Set(["logging", "rum"]);

/**
 * Parse the `ff` query parameter's value into an override set.
 * "history,feedback" turns both flags on: { history: true, feedback: true }.
 * "-history" turns one off: { history: false }.
 * "" (the parameter present but empty) returns {}, an explicit clear.
 * Blank entries and a bare "-" are skipped.
 */
export function parseOverrideParam(raw) {
  const overrides = {};
  for (const entry of raw.split(",")) {
    const trimmed = entry.trim();
    if (!trimmed || trimmed === "-") continue;
    if (trimmed.startsWith("-")) overrides[trimmed.slice(1)] = false;
    else overrides[trimmed] = true;
  }
  return overrides;
}

/**
 * Merge default flag values from config.json's features object with the browser's
 * stored overrides. A name present in overrides wins regardless of the default; every
 * other name keeps its default value.
 */
export function overlayFlags(defaults, overrides) {
  return { ...defaults, ...overrides };
}

/** Names of the flags that resolve true, sorted, for the body's data-features attribute. */
export function enabledFlagNames(flags) {
  return Object.keys(flags)
    .filter((name) => flags[name] === true)
    .sort();
}

/**
 * Parse the raw string read from localStorage[OVERRIDE_KEY] into an override set,
 * tolerating garbage: anything that fails to parse as JSON, is not a plain object, or
 * whose value is not a boolean is dropped rather than thrown. Absent or empty input
 * returns {}. A caller still wraps the storage read itself in try/catch, since the
 * access can throw before this function ever sees a value (a private window, storage
 * blocked by the browser).
 */
export function parseStoredOverrides(raw) {
  if (!raw) return {};
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return {};
  const overrides = {};
  for (const [name, value] of Object.entries(parsed)) {
    if (typeof value === "boolean") overrides[name] = value;
  }
  return overrides;
}

/** Serializes an override set for storage under OVERRIDE_KEY. */
export function serializeOverrides(overrides) {
  return JSON.stringify(overrides);
}

/**
 * Applies one choice from the flags page's three-way control to an override set,
 * without mutating the set passed in. "default" removes the name so the committed
 * default from config.json applies again; "on" and "off" force the flag regardless of
 * that default.
 */
export function setOverride(overrides, name, choice) {
  const next = { ...overrides };
  if (choice === "on") next[name] = true;
  else if (choice === "off") next[name] = false;
  else delete next[name];
  return next;
}

/**
 * Builds one row per flag name in defaults (config.features), for the flags page to
 * render: the committed default, this browser's override as the same three-way
 * vocabulary setOverride takes ("default", "on", or "off"), the effective value the
 * flag resolves to, and whether the flag is one observability depends on.
 */
export function buildFlagRows(defaults, overrides) {
  return Object.keys(defaults)
    .sort()
    .map((name) => {
      const hasOverride = Object.prototype.hasOwnProperty.call(overrides, name);
      const defaultValue = Boolean(defaults[name]);
      return {
        name,
        defaultValue,
        override: hasOverride ? (overrides[name] ? "on" : "off") : "default",
        effective: hasOverride ? overrides[name] : defaultValue,
        operational: OPERATIONAL_FLAGS.has(name),
      };
    });
}
