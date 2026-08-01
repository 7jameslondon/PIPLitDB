"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");

const {
  decideFinalStatus,
  decidePendingStatus,
  statusContext,
} = require("../scripts/pr_status_decision.cjs");

const expected = {
  expectedBaseRef: "main",
  expectedBaseSha: "b".repeat(40),
  expectedHeadSha: "h".repeat(40),
};

function currentPullRequest(overrides = {}) {
  return {
    state: "open",
    base: { ref: expected.expectedBaseRef, sha: expected.expectedBaseSha },
    head: { sha: expected.expectedHeadSha },
    ...overrides,
  };
}

function finalInput(overrides = {}) {
  return {
    ...expected,
    pullRequest: currentPullRequest(),
    beforeTreeSha: "t".repeat(40),
    afterTreeSha: "t".repeat(40),
    beforeResolutionOutcome: "success",
    afterResolutionOutcome: "success",
    validationOutcome: "success",
    baseValidationOutcome: "success",
    ...overrides,
  };
}

test("status contexts are isolated by base branch", () => {
  assert.equal(statusContext("main"), "Validate metadata records (main)");
  assert.equal(
    statusContext("release"),
    "Validate metadata records (release)",
  );
  assert.notEqual(statusContext("main"), statusContext("release"));
});

test("matching current pull request publishes pending", () => {
  assert.deepEqual(
    decidePendingStatus({
      ...expected,
      pullRequest: currentPullRequest(),
    }),
    {
      decision: "pending",
      context: "Validate metadata records (main)",
      description: "Trusted metadata validation is running.",
    },
  );
});

for (const [name, pullRequest] of [
  ["closed pull request", currentPullRequest({ state: "closed" })],
  [
    "retargeted pull request",
    currentPullRequest({
      base: { ref: "release", sha: expected.expectedBaseSha },
    }),
  ],
  [
    "changed base commit",
    currentPullRequest({
      base: { ref: "main", sha: "c".repeat(40) },
    }),
  ],
  [
    "changed head commit",
    currentPullRequest({ head: { sha: "i".repeat(40) } }),
  ],
]) {
  test(`${name} skips stale status writes`, () => {
    const decision = decidePendingStatus({ ...expected, pullRequest });
    assert.equal(decision.decision, "skip");
  });
}

test("stable validated content succeeds", () => {
  const decision = decideFinalStatus(finalInput());
  assert.equal(decision.decision, "success");
  assert.equal(decision.context, "Validate metadata records (main)");
  assert.match(decision.description, /^Validated merge tree t{12}\.$/);
});

test("changed merge tree fails", () => {
  const decision = decideFinalStatus(
    finalInput({ afterTreeSha: "u".repeat(40) }),
  );
  assert.equal(decision.decision, "failure");
  assert.match(decision.description, /merge content changed/);
});

test("candidate validation failure fails", () => {
  const decision = decideFinalStatus(
    finalInput({ validationOutcome: "failure" }),
  );
  assert.equal(decision.decision, "failure");
  assert.equal(decision.description, "Trusted metadata validation failed.");
});

test("base validation failure prevents a stale success", () => {
  const decision = decideFinalStatus(
    finalInput({ baseValidationOutcome: "failure" }),
  );
  assert.equal(decision.decision, "failure");
  assert.equal(decision.description, "Current base-branch validation failed.");
});

test("superseded final run does not publish", () => {
  const decision = decideFinalStatus(
    finalInput({ pullRequest: currentPullRequest({ head: { sha: "i".repeat(40) } }) }),
  );
  assert.equal(decision.decision, "skip");
});
