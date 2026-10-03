import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { MAX_PROJECT_CARDS, PROJECTS_URL, projectCard, projectNames } from "../src/project.js";

const manifest = (extra = {}) => ({
  name: "hr",
  label: "HR Assistant",
  agent: "/api/hr/invocations",
  ...extra,
});

test("the project list lives at the site root", () => {
  assert.equal(PROJECTS_URL, "/projects.json");
});

test("projectNames keeps valid, unique names in order", () => {
  assert.deepEqual(projectNames({ projects: ["hr", "hr-connect", "hr", "Bad Name", 7, "mcp-app"] }), [
    "hr",
    "hr-connect",
    "mcp-app",
  ]);
});

test("projectNames is empty for anything that is not the expected shape", () => {
  assert.deepEqual(projectNames(null), []);
  assert.deepEqual(projectNames({ projects: "hr" }), []);
  assert.deepEqual(projectNames(["hr"]), []);
});

test("projectNames stops at the card limit", () => {
  const many = Array.from({ length: MAX_PROJECT_CARDS + 3 }, (_, i) => `p${i}`);
  assert.equal(projectNames({ projects: many }).length, MAX_PROJECT_CARDS);
});

test("a card carries the label, the trimmed description and the project's page", () => {
  assert.deepEqual(projectCard(manifest({ description: "  Routes HR questions.  " }), "hr"), {
    name: "hr",
    label: "HR Assistant",
    description: "Routes HR questions.",
    href: "/p/hr/",
  });
});

test("a card without a description has an empty one, and a long one is cut", () => {
  assert.equal(projectCard(manifest(), "hr").description, "");
  assert.equal(projectCard(manifest({ description: 42 }), "hr").description, "");
  assert.equal(projectCard(manifest({ description: "x".repeat(400) }), "hr").description.length, 200);
});

test("a manifest the page would not use gives no card", () => {
  assert.equal(projectCard(manifest(), "other"), null);
  assert.equal(projectCard(null, "hr"), null);
  assert.equal(projectCard({ name: "hr", agent: "/api/hr/invocations" }, "hr"), null);
});

test("the shipped list names the three projects", () => {
  const list = JSON.parse(readFileSync(new URL("../src/projects.json", import.meta.url), "utf8"));
  assert.deepEqual(projectNames(list), ["hr", "hr-diy", "mcp-app"]);
});

test("the switcher lists GuppiGPT first, then the projects, and marks the current one", async () => {
  const { switcherEntries } = await import("../src/project.js");
  const cards = [
    { name: "hr", label: "HR Assistant", description: "d", href: "/p/hr/" },
    { name: "mcp-app", label: "MCP App Lab", description: "", href: "/p/mcp-app/" },
  ];
  const onHr = switcherEntries(cards, "hr");
  assert.deepEqual(
    onHr.map((e) => [e.label, e.href, e.current]),
    [
      ["GuppiGPT", "/", false],
      ["HR Assistant", "/p/hr/", true],
      ["MCP App Lab", "/p/mcp-app/", false],
    ],
  );
  assert.ok(onHr[0].description.length > 0);
  assert.equal(switcherEntries(cards, null)[0].current, true);
  assert.deepEqual(switcherEntries([], null).map((e) => e.href), ["/"]);
});
