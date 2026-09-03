import { test } from "node:test";
import assert from "node:assert/strict";
import { nextVote, buildFeedbackDetail } from "../src/feedback.js";

test("nextVote sets a vote from no vote", () => {
  assert.equal(nextVote(null, "up"), "up");
  assert.equal(nextVote(null, "down"), "down");
});

test("nextVote withdraws a vote on a second click of the same choice", () => {
  assert.equal(nextVote("up", "up"), null);
  assert.equal(nextVote("down", "down"), null);
});

test("nextVote replaces a vote when the other choice is clicked", () => {
  assert.equal(nextVote("up", "down"), "down");
  assert.equal(nextVote("down", "up"), "up");
});

test("buildFeedbackDetail carries every field through unchanged", () => {
  assert.deepEqual(
    buildFeedbackDetail({
      threadId: "t1",
      runId: "r1",
      traceId: "tr1",
      requestId: "req1",
      messageId: "m1",
      vote: "up",
    }),
    { threadId: "t1", runId: "r1", traceId: "tr1", requestId: "req1", messageId: "m1", vote: "up" },
  );
});

test("buildFeedbackDetail normalizes a withdrawn vote to null", () => {
  const detail = buildFeedbackDetail({
    threadId: "t1",
    runId: "r1",
    traceId: "tr1",
    requestId: "req1",
    messageId: "m1",
    vote: null,
  });
  assert.equal(detail.vote, null);
});

test("buildFeedbackDetail normalizes a missing request id to null", () => {
  const detail = buildFeedbackDetail({
    threadId: "t1",
    runId: "r1",
    traceId: "tr1",
    requestId: undefined,
    messageId: "m1",
    vote: "down",
  });
  assert.equal(detail.requestId, null);
});
