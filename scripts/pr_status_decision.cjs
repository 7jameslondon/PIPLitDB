"use strict";

const STATUS_PREFIX = "Validate metadata records";

function requiredText(value, name) {
  const normalized = String(value ?? "").trim();
  if (normalized === "") {
    throw new TypeError(`${name} must not be empty`);
  }
  return normalized;
}

function statusContext(baseRef) {
  return `${STATUS_PREFIX} (${requiredText(baseRef, "baseRef")})`;
}

function pullRequestMatches({
  pullRequest,
  expectedBaseRef,
  expectedBaseSha,
  expectedHeadSha,
}) {
  return Boolean(
    pullRequest &&
      pullRequest.state === "open" &&
      pullRequest.base?.ref === expectedBaseRef &&
      pullRequest.base?.sha === expectedBaseSha &&
      pullRequest.head?.sha === expectedHeadSha,
  );
}

function skipReason({
  pullRequest,
  expectedBaseRef,
  expectedBaseSha,
  expectedHeadSha,
}) {
  if (!pullRequest || pullRequest.state !== "open") {
    return "The pull request is no longer open.";
  }
  if (pullRequest.base?.ref !== expectedBaseRef) {
    return "The pull request now targets a different base branch.";
  }
  if (pullRequest.base?.sha !== expectedBaseSha) {
    return "The pull request base commit changed.";
  }
  if (pullRequest.head?.sha !== expectedHeadSha) {
    return "The pull request head commit changed.";
  }
  return "The pull request no longer matches this validation run.";
}

function decidePendingStatus(input) {
  const context = statusContext(input.expectedBaseRef);
  if (!pullRequestMatches(input)) {
    return {
      decision: "skip",
      context,
      description: skipReason(input),
    };
  }
  return {
    decision: "pending",
    context,
    description: "Trusted metadata validation is running.",
  };
}

function decideFinalStatus(input) {
  const pendingDecision = decidePendingStatus(input);
  if (pendingDecision.decision === "skip") {
    return pendingDecision;
  }

  const beforeTreeSha = String(input.beforeTreeSha ?? "");
  const afterTreeSha = String(input.afterTreeSha ?? "");
  const contentStable =
    input.beforeResolutionOutcome === "success" &&
    input.afterResolutionOutcome === "success" &&
    beforeTreeSha !== "" &&
    beforeTreeSha === afterTreeSha;
  const baseValidationPassed =
    input.baseValidationOutcome === undefined ||
    input.baseValidationOutcome === "success";
  const validationPassed =
    input.validationOutcome === "success" && baseValidationPassed;

  if (contentStable && validationPassed) {
    return {
      decision: "success",
      context: pendingDecision.context,
      description: `Validated merge tree ${afterTreeSha.slice(0, 12)}.`,
    };
  }
  if (!baseValidationPassed) {
    return {
      decision: "failure",
      context: pendingDecision.context,
      description: "Current base-branch validation failed.",
    };
  }
  if (!contentStable) {
    return {
      decision: "failure",
      context: pendingDecision.context,
      description: "Pull request merge content changed or could not be resolved.",
    };
  }
  return {
    decision: "failure",
    context: pendingDecision.context,
    description: "Trusted metadata validation failed.",
  };
}

module.exports = {
  decideFinalStatus,
  decidePendingStatus,
  pullRequestMatches,
  statusContext,
};
