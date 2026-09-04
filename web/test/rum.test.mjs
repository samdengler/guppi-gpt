import { test } from "node:test";
import assert from "node:assert/strict";
import { rumActive, buildFeedbackActionProperties, buildFlagSessionProperty } from "../src/rum.js";

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

test("buildFeedbackActionProperties carries every identifier through as a string", () => {
  assert.deepEqual(
    buildFeedbackActionProperties({
      threadId: "t1",
      runId: "r1",
      traceId: "tr1",
      requestId: "req1",
      vote: "up",
    }),
    { vote: "up", runId: "r1", traceId: "tr1", requestId: "req1", threadId: "t1" },
  );
});

test("buildFeedbackActionProperties reports a withdrawn vote as the string withdrawn", () => {
  const props = buildFeedbackActionProperties({
    threadId: "t1",
    runId: "r1",
    traceId: "tr1",
    requestId: "req1",
    vote: null,
  });
  assert.equal(props.vote, "withdrawn");
});

test("buildFeedbackActionProperties normalizes a missing request id to a dash", () => {
  const props = buildFeedbackActionProperties({
    threadId: "t1",
    runId: "r1",
    traceId: "tr1",
    requestId: undefined,
    vote: "down",
  });
  assert.equal(props.requestId, "-");
});

test("buildFeedbackActionProperties normalizes every field to a dash on an empty detail", () => {
  assert.deepEqual(buildFeedbackActionProperties({}), {
    vote: "-",
    runId: "-",
    traceId: "-",
    requestId: "-",
    threadId: "-",
  });
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
