import { test } from "node:test";
import assert from "node:assert/strict";
import { rumActive, buildFlagSessionProperty } from "../src/rum.js";

test("rumActive is false when the flag is off", () => {
  assert.equal(rumActive({ rum: false }, { rum: { scriptPath: "/dt/ruxitagentjs.js" } }), false);
});

test("rumActive is false when no script path is configured", () => {
  assert.equal(rumActive({ rum: true }, { rum: {} }), false);
  assert.equal(rumActive({ rum: true }, {}), false);
  assert.equal(rumActive({ rum: true }, undefined), false);
});

test("rumActive is true only with the flag on and a script path set", () => {
  assert.equal(rumActive({ rum: true }, { rum: { scriptPath: "/dt/ruxitagentjs.js" } }), true);
});

test("buildFlagSessionProperty maps a true evaluation to the string true", () => {
  assert.deepEqual(buildFlagSessionProperty("history", true), { history: "true" });
});

test("buildFlagSessionProperty maps a false evaluation to the string false", () => {
  assert.deepEqual(buildFlagSessionProperty("feedback", false), { feedback: "false" });
});

test("buildFlagSessionProperty lower-cases the flag key", () => {
  assert.deepEqual(buildFlagSessionProperty("Feedback", true), { feedback: "true" });
});
