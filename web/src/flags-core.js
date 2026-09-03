// Pure functions behind the feature flag overlay: parsing the `ff` query parameter and
// merging overrides onto the defaults from config.json. No DOM and no SDK import here,
// so this file runs under node:test without a browser (web/test/features.test.mjs).

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
 * Merge default flag values from config.json's features object with the per-tab
 * overrides. A name present in overrides wins regardless of the default; every other
 * name keeps its default value.
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
