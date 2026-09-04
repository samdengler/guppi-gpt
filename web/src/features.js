// Feature switches. features.json is bundled into app.js at build time, so flipping a flag
// is an edit plus a page build and deploy; nothing reads it at runtime.

import flags from "../features.json";
import { emptyStateText, hintText } from "./copy.js";

export function isEnabled(name) {
  return flags[name] === true;
}

export function notice() {
  const history = isEnabled("history");
  const logging = isEnabled("logging");
  return { hint: hintText(history, logging), empty: emptyStateText(history, logging) };
}
