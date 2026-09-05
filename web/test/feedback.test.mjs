import { test } from "node:test";
import assert from "node:assert/strict";
import { nextVote, buildFeedbackDetail, buildFeedbackRequestBody } from "../src/feedback.js";

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

test("buildFeedbackRequestBody carries a full detail through", () => {
  assert.deepEqual(
    buildFeedbackRequestBody({
      threadId: "t1",
      runId: "r1",
      traceId: "tr1",
      requestId: "req1",
      messageId: "m1",
      vote: "up",
    }),
    { vote: "up", runId: "r1", threadId: "t1", traceId: "tr1", requestId: "req1", messageId: "m1" },
  );
});

test("buildFeedbackRequestBody sends a withdrawn vote as none", () => {
  const body = buildFeedbackRequestBody({ threadId: "t1", runId: "r1", vote: null });
  assert.equal(body.vote, "none");
});

test("buildFeedbackRequestBody leaves out the identifiers it was not given", () => {
  const body = buildFeedbackRequestBody({
    threadId: "t1",
    runId: "r1",
    traceId: null,
    requestId: null,
    messageId: null,
    vote: "down",
  });
  assert.deepEqual(body, { vote: "down", runId: "r1", threadId: "t1" });
});

test("buildFeedbackRequestBody keeps the required fields present on an empty detail", () => {
  assert.deepEqual(buildFeedbackRequestBody({}), { vote: "none", runId: "", threadId: "" });
  assert.deepEqual(buildFeedbackRequestBody(undefined), { vote: "none", runId: "", threadId: "" });
});
