import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const EFFECTIVE_DATE = "Effective date: 7 September 2026";

const privacy = readFileSync(
  fileURLToPath(new URL("../src/privacy.html", import.meta.url)),
  "utf8",
);
const terms = readFileSync(
  fileURLToPath(new URL("../src/terms.html", import.meta.url)),
  "utf8",
);

for (const [name, html, otherHref] of [
  ["privacy.html", privacy, "terms.html"],
  ["terms.html", terms, "privacy.html"],
]) {
  test(`${name} carries the effective date`, () => {
    assert.ok(html.includes(EFFECTIVE_DATE), `${name} is missing "${EFFECTIVE_DATE}"`);
  });

  test(`${name} links to the other legal page`, () => {
    assert.ok(html.includes(`href="${otherHref}"`), `${name} does not link to ${otherHref}`);
  });

  test(`${name} has no inline style attributes`, () => {
    assert.ok(!/\sstyle\s*=/.test(html), `${name} has a style= attribute`);
  });

  test(`${name} has no inline scripts`, () => {
    assert.ok(!html.includes("<script"), `${name} has a <script> element`);
  });
}
