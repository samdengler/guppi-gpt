import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import {
  buildFlagRows,
  setOverride,
  parseStoredOverrides,
  serializeOverrides,
  OVERRIDE_KEY,
} from "../src/flags-core.js";

test("OVERRIDE_KEY is the localStorage key features.js and flags.js share", () => {
  assert.equal(OVERRIDE_KEY, "guppigpt_ff_overrides");
});

test("buildFlagRows marks a name with no override as default, using the committed default", () => {
  assert.deepEqual(buildFlagRows({ history: false }, {}), [
    { name: "history", defaultValue: false, override: "default", effective: false, operational: false },
  ]);
});

test("buildFlagRows reflects an override that turns a flag on", () => {
  assert.deepEqual(buildFlagRows({ history: false }, { history: true }), [
    { name: "history", defaultValue: false, override: "on", effective: true, operational: false },
  ]);
});

test("buildFlagRows reflects an override that turns a flag off", () => {
  assert.deepEqual(buildFlagRows({ logging: true }, { logging: false }), [
    { name: "logging", defaultValue: true, override: "off", effective: false, operational: true },
  ]);
});

test("buildFlagRows marks logging and rum operational, and everything else not", () => {
  const rows = buildFlagRows({ history: false, feedback: false, logging: true, rum: true }, {});
  const operational = rows.filter((row) => row.operational).map((row) => row.name);
  assert.deepEqual(operational, ["logging", "rum"]);
});

test("buildFlagRows sorts rows by name", () => {
  const rows = buildFlagRows({ rum: true, feedback: false, history: false, logging: true }, {});
  assert.deepEqual(rows.map((row) => row.name), ["feedback", "history", "logging", "rum"]);
});

test("setOverride forces a flag on", () => {
  assert.deepEqual(setOverride({}, "history", "on"), { history: true });
});

test("setOverride forces a flag off", () => {
  assert.deepEqual(setOverride({}, "history", "off"), { history: false });
});

test("setOverride with 'default' removes the key, uncovering the committed default", () => {
  assert.deepEqual(setOverride({ history: true, feedback: false }, "history", "default"), { feedback: false });
});

test("setOverride does not mutate the override set it was given", () => {
  const overrides = { history: true };
  setOverride(overrides, "history", "off");
  assert.deepEqual(overrides, { history: true });
});

test("parseStoredOverrides round-trips what serializeOverrides writes", () => {
  const overrides = { history: true, feedback: false };
  assert.deepEqual(parseStoredOverrides(serializeOverrides(overrides)), overrides);
});

test("parseStoredOverrides returns {} for null, undefined, and an empty string", () => {
  assert.deepEqual(parseStoredOverrides(null), {});
  assert.deepEqual(parseStoredOverrides(undefined), {});
  assert.deepEqual(parseStoredOverrides(""), {});
});

test("parseStoredOverrides tolerates a value that is not JSON", () => {
  assert.deepEqual(parseStoredOverrides("not json{"), {});
});

test("parseStoredOverrides tolerates a JSON value that is not a plain object", () => {
  assert.deepEqual(parseStoredOverrides("[1,2,3]"), {});
  assert.deepEqual(parseStoredOverrides('"a string"'), {});
  assert.deepEqual(parseStoredOverrides("42"), {});
  assert.deepEqual(parseStoredOverrides("null"), {});
});

test("parseStoredOverrides drops entries whose value is not a boolean, keeping the rest", () => {
  assert.deepEqual(
    parseStoredOverrides('{"history": true, "feedback": "yes", "rum": 1, "logging": false}'),
    { history: true, logging: false },
  );
});

const flagsHtml = readFileSync(fileURLToPath(new URL("../src/flags.html", import.meta.url)), "utf8");

test("flags.html has no inline style attributes", () => {
  assert.ok(!/\sstyle\s*=/.test(flagsHtml), "flags.html has a style= attribute");
});

test("flags.html has no inline script other than the bundled flags.js reference", () => {
  const scriptTags = [...flagsHtml.matchAll(/<script\b[^>]*>/g)].map((match) => match[0]);
  assert.equal(scriptTags.length, 1, `expected exactly one <script> tag, found ${scriptTags.length}`);
  assert.ok(scriptTags[0].includes('src="flags.js"'), "the one <script> tag is not the flags.js reference");
});

test("flags.html is not linked from index.html, privacy.html, or terms.html", () => {
  for (const page of ["index.html", "privacy.html", "terms.html"]) {
    const html = readFileSync(fileURLToPath(new URL(`../src/${page}`, import.meta.url)), "utf8");
    assert.ok(!html.includes("flags.html"), `${page} links to flags.html`);
  }
});
