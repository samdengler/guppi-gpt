import { test } from "node:test";
import assert from "node:assert/strict";
import { parseOverrideParam, overlayFlags, enabledFlagNames } from "../src/flags-core.js";

test("parseOverrideParam turns listed names on", () => {
  assert.deepEqual(parseOverrideParam("history,feedback"), { history: true, feedback: true });
});

test("parseOverrideParam turns a single name off with a leading dash", () => {
  assert.deepEqual(parseOverrideParam("-history"), { history: false });
});

test("parseOverrideParam mixes on and off entries", () => {
  assert.deepEqual(parseOverrideParam("history,-feedback"), { history: true, feedback: false });
});

test("parseOverrideParam returns an empty override set for an empty value", () => {
  assert.deepEqual(parseOverrideParam(""), {});
});

test("parseOverrideParam ignores blank entries and a bare dash", () => {
  assert.deepEqual(parseOverrideParam("history,, -,feedback"), { history: true, feedback: true });
});

test("overlayFlags keeps defaults not named in the overrides", () => {
  assert.deepEqual(
    overlayFlags({ history: false, feedback: false }, { history: true }),
    { history: true, feedback: false },
  );
});

test("overlayFlags lets an override force a flag off", () => {
  assert.deepEqual(
    overlayFlags({ history: true, feedback: false }, { history: false }),
    { history: false, feedback: false },
  );
});

test("overlayFlags with no overrides returns the defaults unchanged", () => {
  assert.deepEqual(overlayFlags({ history: false }, {}), { history: false });
});

test("enabledFlagNames lists only the true flags, sorted", () => {
  assert.deepEqual(
    enabledFlagNames({ feedback: true, history: false, other: true }),
    ["feedback", "other"],
  );
});

test("enabledFlagNames returns an empty array when nothing is enabled", () => {
  assert.deepEqual(enabledFlagNames({ history: false, feedback: false }), []);
});
