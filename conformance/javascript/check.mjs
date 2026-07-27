// SPDX-License-Identifier: MPL-2.0
// Copyright (C) 2026 Greater Expanse LLC

import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import Ajv2020 from "ajv/dist/2020.js";
import addFormats from "ajv-formats";
import { parse } from "lossless-json";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const SCHEMA_PATH = path.join(
  ROOT,
  "src",
  "itself",
  "schemas",
  "v0alpha2",
  "protocol.schema.json",
);

const ARTIFACT = new Set(["artifact_reference"]);
const CLAIM_OR_HYPOTHESIS = new Set(["claim", "hypothesis"]);
const EVIDENCE = new Set(["evidence"]);
const PREDICTION = new Set(["prediction"]);
const TEST = new Set(["test"]);
const TRANSITION = new Set(["status_transition"]);
const VERDICT = new Set(["verdict"]);
const ANY_RECORD = new Set([
  "artifact_reference",
  "claim",
  "hypothesis",
  "prediction",
  "test",
  "evidence",
  "verdict",
  "decision",
  "status_transition",
]);

const REFERENCE_FIELDS = {
  artifact_reference: [["derived_from_refs", ARTIFACT, true]],
  claim: [
    ["evidence_refs", EVIDENCE, true],
    ["contradicting_evidence_refs", EVIDENCE, true],
    ["dependency_refs", ANY_RECORD, true],
    ["transition_refs", TRANSITION, true],
  ],
  hypothesis: [
    ["explains_evidence_refs", EVIDENCE, true],
    ["alternative_hypothesis_refs", new Set(["hypothesis"]), true],
    ["prediction_refs", PREDICTION, true],
    ["dependency_refs", ANY_RECORD, true],
  ],
  prediction: [
    ["hypothesis_ref", new Set(["hypothesis"]), true],
    ["test_ref", TEST, true],
  ],
  test: [
    ["subject_refs", CLAIM_OR_HYPOTHESIS, true],
    ["prediction_refs", PREDICTION, true],
    ["plan_ref", TEST, true],
    ["evidence_refs", EVIDENCE, true],
  ],
  evidence: [
    ["artifact_refs", ARTIFACT, true],
    ["test_ref", TEST, true],
  ],
  verdict: [
    ["subject_ref", CLAIM_OR_HYPOTHESIS, true],
    ["evidence_refs", EVIDENCE, true],
    ["policy_ref", ARTIFACT, true],
  ],
  decision: [
    ["policy_ref", ARTIFACT, true],
    ["relied_on_claim_refs", new Set(["claim"]), true],
    ["unresolved_claim_refs", new Set(["claim"]), true],
  ],
  status_transition: [
    ["subject_ref", CLAIM_OR_HYPOTHESIS, false],
    ["evidence_refs", EVIDENCE, false],
    ["verdict_ref", VERDICT, false],
    ["policy_ref", ARTIFACT, false],
  ],
};

const ALLOWED_TRANSITIONS = {
  proposed: new Set(["testable", "blocked", "withdrawn"]),
  testable: new Set(["under_test", "blocked", "withdrawn"]),
  blocked: new Set(["testable", "withdrawn"]),
  under_test: new Set([
    "supported",
    "refuted",
    "inconclusive",
    "mixed_evidence",
    "blocked",
  ]),
  supported: new Set([
    "corroborated_within_scope",
    "under_test",
    "mixed_evidence",
    "stale",
    "invalidated",
  ]),
  refuted: new Set(["under_test", "stale", "invalidated"]),
  inconclusive: new Set(["under_test", "stale", "invalidated"]),
  mixed_evidence: new Set([
    "under_test",
    "supported",
    "refuted",
    "inconclusive",
    "stale",
    "invalidated",
  ]),
  corroborated_within_scope: new Set([
    "under_test",
    "mixed_evidence",
    "stale",
    "invalidated",
  ]),
  stale: new Set(["under_test", "invalidated"]),
  invalidated: new Set(["under_test"]),
  withdrawn: new Set(),
};

const EVIDENCE_BACKED_STATES = new Set([
  "supported",
  "refuted",
  "inconclusive",
  "mixed_evidence",
  "corroborated_within_scope",
]);
const PROMOTION_ROLES = new Set(["evaluator", "reviewer", "authority"]);

const EXPECTED_BUNDLE_CODES = {
  "dangling-reference.json": "unresolved_reference",
  "duplicate-id.json": "duplicate_id",
  "invalid-transition.json": "invalid_transition",
  "state-drift.json": "state_mismatch",
  "test-plan-kind-mismatch.json": "reference_kind_mismatch",
  "transition-subject-mismatch.json": "transition_subject_mismatch",
  "unrelated-evidence.json": "evidence_relation_missing",
  "verdict-after-transition.json": "reference_not_available",
  "wrong-reference-kind.json": "reference_kind_mismatch",
};

function parseIJsonNumber(source) {
  const value = Number(source);
  if (!Number.isFinite(value)) {
    throw new SyntaxError(`number ${source} is not a finite IEEE 754 value`);
  }
  if (Object.is(value, -0)) {
    throw new SyntaxError("negative zero is not accepted");
  }
  const mantissa = source.split(/[eE]/, 1)[0];
  if (value === 0 && /[1-9]/.test(mantissa)) {
    throw new SyntaxError(`number ${source} underflows IEEE 754 binary64`);
  }
  if (
    !source.includes(".") &&
    !/[eE]/.test(source) &&
    !Number.isSafeInteger(value)
  ) {
    throw new SyntaxError(
      `integer ${source} exceeds the interoperable IEEE 754 safe range`,
    );
  }
  return value;
}

function assertWellFormedString(value) {
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (next < 0xdc00 || next > 0xdfff) {
        throw new TypeError("string contains a Unicode surrogate");
      }
      index += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) {
      throw new TypeError("string contains a Unicode surrogate");
    }
  }
}

function ensureIJson(value) {
  if (value === null || typeof value === "boolean") {
    return;
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value) || Object.is(value, -0)) {
      throw new TypeError("number is not an interoperable I-JSON value");
    }
    return;
  }
  if (typeof value === "string") {
    assertWellFormedString(value);
    return;
  }
  if (Array.isArray(value)) {
    value.forEach(ensureIJson);
    return;
  }
  for (const [key, child] of Object.entries(value)) {
    assertWellFormedString(key);
    ensureIJson(child);
  }
}

function parseJson(source) {
  const value = parse(source, undefined, {
    parseNumber: parseIJsonNumber,
  });
  ensureIJson(value);
  return value;
}

function readJson(filePath) {
  return parseJson(fs.readFileSync(filePath, "utf8"));
}

function canonicalJson(value) {
  ensureIJson(value);
  if (value === null || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    return JSON.stringify(value);
  }
  if (typeof value === "string") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map(canonicalJson).join(",")}]`;
  }
  return `{${Object.keys(value)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`)
    .join(",")}}`;
}

function sameStringSet(left, right) {
  const leftValues = [...new Set(left)].sort();
  const rightValues = [...new Set(right)].sort();
  return (
    leftValues.length === rightValues.length &&
    leftValues.every((value, index) => value === rightValues[index])
  );
}

function fixturePaths(...segments) {
  const directory = path.join(ROOT, "conformance", ...segments);
  return fs
    .readdirSync(directory)
    .filter((name) => name.endsWith(".json"))
    .sort()
    .map((name) => path.join(directory, name));
}

function references(record, field) {
  const value = record[field];
  if (value === undefined) {
    return [];
  }
  return Array.isArray(value) ? value : [value];
}

function issue(code, recordId, reference = undefined) {
  return { code, recordId, reference };
}

function checkReference(
  recordId,
  reference,
  allowedKinds,
  allowExternal,
  index,
  externalRefs,
) {
  const target = index.get(reference);
  if (target === undefined) {
    if (allowExternal && externalRefs.has(reference)) {
      return [];
    }
    return [issue("unresolved_reference", recordId, reference)];
  }
  if (!allowedKinds.has(target.kind)) {
    return [issue("reference_kind_mismatch", recordId, reference)];
  }
  return [];
}

function transitionPolicyIssues(record) {
  const issues = [];
  if (!ALLOWED_TRANSITIONS[record.from_status].has(record.to_status)) {
    issues.push(issue("invalid_transition", record.id, record.subject_ref));
    return issues;
  }
  if (EVIDENCE_BACKED_STATES.has(record.to_status)) {
    if (references(record, "evidence_refs").length === 0) {
      issues.push(issue("invalid_transition", record.id, record.subject_ref));
    } else if (record.verdict_ref === undefined) {
      issues.push(issue("invalid_transition", record.id, record.subject_ref));
    } else if (!PROMOTION_ROLES.has(record.authorized_by.role)) {
      issues.push(issue("invalid_transition", record.id, record.subject_ref));
    } else if (record.authorized_by.actor_type === "model") {
      issues.push(issue("invalid_transition", record.id, record.subject_ref));
    }
  }
  if (
    record.to_status === "corroborated_within_scope" &&
    record.policy_ref === undefined
  ) {
    issues.push(issue("invalid_transition", record.id, record.subject_ref));
  }
  return issues;
}

function verdictIssues(records, index, positions) {
  const issues = [];
  records.forEach((verdict, position) => {
    if (verdict.kind !== "verdict") {
      return;
    }

    const materialRefs = [
      verdict.subject_ref,
      ...references(verdict, "evidence_refs"),
      ...references(verdict, "policy_ref"),
    ];
    for (const reference of materialRefs) {
      const referencePosition = positions.get(reference);
      if (referencePosition !== undefined && referencePosition >= position) {
        issues.push(
          issue("reference_not_available", verdict.id, reference),
        );
      }
    }

    const subject = index.get(verdict.subject_ref);
    if (
      subject === undefined ||
      !CLAIM_OR_HYPOTHESIS.has(subject.kind)
    ) {
      return;
    }
    if (canonicalJson(verdict.scope) !== canonicalJson(subject.scope)) {
      issues.push(issue("scope_mismatch", verdict.id, verdict.subject_ref));
    }

    const observedRelations = new Set();
    for (const evidenceRef of references(verdict, "evidence_refs")) {
      const evidence = index.get(evidenceRef);
      if (evidence?.kind !== "evidence") {
        continue;
      }
      if (canonicalJson(evidence.scope) !== canonicalJson(verdict.scope)) {
        issues.push(issue("scope_mismatch", verdict.id, evidenceRef));
      }
      const relations = new Set(
        evidence.relations
          .filter((relation) => relation.subject_ref === verdict.subject_ref)
          .map((relation) => relation.relation),
      );
      if (relations.size === 0) {
        issues.push(
          issue("evidence_relation_missing", verdict.id, evidenceRef),
        );
        continue;
      }
      relations.forEach((relation) => observedRelations.add(relation));
      const expectedRelation = {
        supported: "supports",
        corroborated_within_scope: "supports",
        refuted: "contradicts",
      }[verdict.outcome];
      if (
        expectedRelation !== undefined &&
        !relations.has(expectedRelation)
      ) {
        issues.push(
          issue("evidence_relation_missing", verdict.id, evidenceRef),
        );
      }
    }
    if (
      verdict.outcome === "mixed_evidence" &&
      !(
        observedRelations.has("supports") &&
        observedRelations.has("contradicts")
      )
    ) {
      issues.push(
        issue("evidence_relation_missing", verdict.id, verdict.subject_ref),
      );
    }
  });
  return issues;
}

function validateBundle(records, externalRefs = new Set()) {
  const schemaIssues = [];
  for (const record of records) {
    if (!validateRecord(record)) {
      schemaIssues.push(issue("schema_invalid", record.id ?? "<unknown>"));
    }
  }
  if (schemaIssues.length > 0) {
    return { issues: schemaIssues, states: {} };
  }

  const firstPositions = new Map();
  const duplicateIssues = [];
  records.forEach((record, position) => {
    if (firstPositions.has(record.id)) {
      duplicateIssues.push(issue("duplicate_id", record.id));
    } else {
      firstPositions.set(record.id, position);
    }
  });
  if (duplicateIssues.length > 0) {
    return { issues: duplicateIssues, states: {} };
  }

  const index = new Map(records.map((record) => [record.id, record]));
  const positions = new Map(records.map((record, position) => [record.id, position]));
  const issues = [];

  for (const record of records) {
    for (const [field, allowedKinds, allowExternal] of REFERENCE_FIELDS[
      record.kind
    ]) {
      for (const reference of references(record, field)) {
        issues.push(
          ...checkReference(
            record.id,
            reference,
            allowedKinds,
            allowExternal,
            index,
            externalRefs,
          ),
        );
      }
    }

    if (record.kind === "evidence") {
      for (const relation of record.relations) {
        issues.push(
          ...checkReference(
            record.id,
            relation.subject_ref,
            CLAIM_OR_HYPOTHESIS,
            true,
            index,
            externalRefs,
          ),
        );
      }
    }

    if (record.kind === "claim" || record.kind === "hypothesis") {
      for (const transitionRef of references(record, "transition_refs")) {
        const transition = index.get(transitionRef);
        if (
          transition?.kind === "status_transition" &&
          transition.subject_ref !== record.id
        ) {
          issues.push(
            issue("transition_subject_mismatch", record.id, transitionRef),
          );
        }
      }
    }
  }

  issues.push(...verdictIssues(records, index, positions));

  const states = new Map();
  records.forEach((record, position) => {
    if (record.kind === "claim" || record.kind === "hypothesis") {
      states.set(record.id, record.status);
      return;
    }
    if (record.kind !== "status_transition") {
      return;
    }

    let canApply = true;
    const current = states.get(record.subject_ref);
    if (current === undefined) {
      issues.push(
        issue("subject_not_available", record.id, record.subject_ref),
      );
      canApply = false;
    } else if (record.from_status !== current) {
      issues.push(issue("state_mismatch", record.id, record.subject_ref));
      canApply = false;
    }

    const authorizingRefs = [
      ...references(record, "evidence_refs"),
      ...references(record, "verdict_ref"),
      ...references(record, "policy_ref"),
    ];
    for (const reference of authorizingRefs) {
      const referencePosition = positions.get(reference);
      if (referencePosition !== undefined && referencePosition >= position) {
        issues.push(issue("reference_not_available", record.id, reference));
        canApply = false;
      }
    }

    const policyIssues = transitionPolicyIssues(record);
    if (policyIssues.length > 0) {
      issues.push(...policyIssues);
      canApply = false;
    }

    if (record.verdict_ref !== undefined) {
      const verdict = index.get(record.verdict_ref);
      if (verdict?.kind !== "verdict") {
        canApply = false;
      } else {
        const mismatches = [];
        if (verdict.subject_ref !== record.subject_ref) {
          mismatches.push("subject");
        }
        if (verdict.outcome !== record.to_status) {
          mismatches.push("outcome");
        }
        if (
          !sameStringSet(
            references(verdict, "evidence_refs"),
            references(record, "evidence_refs"),
          )
        ) {
          mismatches.push("evidence");
        }
        if (verdict.policy_ref !== record.policy_ref) {
          mismatches.push("policy");
        }
        if (mismatches.length > 0) {
          issues.push(
            issue("verdict_mismatch", record.id, record.verdict_ref),
          );
          canApply = false;
        }
      }
    }

    if (canApply) {
      states.set(record.subject_ref, record.to_status);
    }
  });

  return { issues, states: Object.fromEntries(states) };
}

function assertRecordFixtures(expectedValid, files) {
  assert(files.length > 0, "record fixture set must not be empty");
  for (const filePath of files) {
    const accepted = validateRecord(readJson(filePath));
    assert.equal(
      accepted,
      expectedValid,
      `${path.relative(ROOT, filePath)}: ${JSON.stringify(validateRecord.errors)}`,
    );
  }
}

function assertBundleFixtures(expectedValid, files) {
  assert(files.length > 0, "bundle fixture set must not be empty");
  for (const filePath of files) {
    const records = readJson(filePath);
    assert(Array.isArray(records), `${filePath}: expected a record array`);
    const before = JSON.stringify(records);
    const first = validateBundle(records);
    const second = validateBundle(records);
    assert.equal(
      JSON.stringify(records),
      before,
      `${filePath}: validation mutated its input`,
    );
    assert.deepEqual(first, second, `${filePath}: replay was not deterministic`);

    if (expectedValid) {
      assert.deepEqual(
        first.issues,
        [],
        `${path.relative(ROOT, filePath)}: ${JSON.stringify(first.issues)}`,
      );
      continue;
    }

    const expectedCode = EXPECTED_BUNDLE_CODES[path.basename(filePath)];
    assert.equal(first.issues.length, 1, `${filePath}: expected one issue`);
    assert.equal(first.issues[0].code, expectedCode, filePath);
  }
}

const schema = readJson(SCHEMA_PATH);
// The canonical schema composes object types through $ref/allOf instead of
// repeating `type: object` beside every `unevaluatedProperties` keyword.
const ajv = new Ajv2020({
  allErrors: true,
  strict: true,
  strictTypes: false,
});
addFormats(ajv);
const validateRecord = ajv.compile(schema);

const validRecords = fixturePaths("valid");
const invalidRecords = fixturePaths("invalid");
const validBundles = fixturePaths("bundles", "valid");
const invalidBundles = fixturePaths("bundles", "invalid");

assert.deepEqual(
  invalidBundles.map((filePath) => path.basename(filePath)),
  Object.keys(EXPECTED_BUNDLE_CODES).sort(),
  "invalid bundle fixtures and expected integrity codes must stay synchronized",
);
assertRecordFixtures(true, validRecords);
assertRecordFixtures(false, invalidRecords);
assertBundleFixtures(true, validBundles);
assertBundleFixtures(false, invalidBundles);

const locallyWrongKind = validateBundle(
  readJson(
    path.join(
      ROOT,
      "conformance",
      "bundles",
      "invalid",
      "wrong-reference-kind.json",
    ),
  ),
  new Set(["artifact-not-evidence"]),
);
assert.equal(
  locallyWrongKind.issues[0]?.code,
  "reference_kind_mismatch",
  "an external declaration must not mask a local record of the wrong kind",
);

const evidenceHistory = validateBundle(
  readJson(
    path.join(
      ROOT,
      "conformance",
      "bundles",
      "valid",
      "evidence-backed-history.json",
    ),
  ),
);
assert.equal(
  evidenceHistory.states["claim-cache-cause"],
  "corroborated_within_scope",
);

const historyRecords = readJson(
  path.join(
    ROOT,
    "conformance",
    "bundles",
    "valid",
    "evidence-backed-history.json",
  ),
);
const reversedRelation = structuredClone(historyRecords);
reversedRelation.find(
  (record) => record.id === "evidence-cache-ablation",
).relations[0].relation = "contradicts";
assert(
  validateBundle(reversedRelation).issues.some(
    (entry) => entry.code === "evidence_relation_missing",
  ),
  "a verdict must reject evidence with the wrong relation",
);

const mismatchedScope = structuredClone(historyRecords);
mismatchedScope.find(
  (record) => record.id === "evidence-cache-ablation",
).scope.description = "A different controlled scope";
assert(
  validateBundle(mismatchedScope).issues.some(
    (entry) => entry.code === "scope_mismatch",
  ),
  "a verdict must reject evidence from a different scope",
);

const mismatchedVerdict = structuredClone(historyRecords);
mismatchedVerdict.find(
  (record) => record.id === "transition-cache-corroborated",
).verdict_ref = "verdict-cache-supported";
assert(
  validateBundle(mismatchedVerdict).issues.some(
    (entry) => entry.code === "verdict_mismatch",
  ),
  "a transition must match its authorizing verdict",
);

assert.throws(
  () => parseJson('{"kind":"claim","kind":"evidence"}'),
  /duplicate/i,
  "duplicate JSON object keys must be rejected",
);
assert.throws(
  () => parseJson('{"value":9007199254740992}'),
  /safe range/i,
  "unsafe integer literals must be rejected",
);

const canonicalizationFixture = readJson(
  path.join(
    ROOT,
    "conformance",
    "canonicalization",
    "rfc8785-jsonl-v1.json",
  ),
);
for (const vector of canonicalizationFixture.vectors) {
  assert.equal(
    canonicalJson(vector.value),
    vector.canonical,
    `RFC 8785 vector failed: ${vector.name}`,
  );
}
const vectorLedger = canonicalizationFixture.ledger_records
  .map((record) => `${canonicalJson(record)}\n`)
  .join("");
assert.equal(
  crypto.createHash("sha256").update(vectorLedger).digest("hex"),
  canonicalizationFixture.ledger_sha256,
  "RFC 8785 JSON Lines digest vector drifted",
);

const referenceBundle = path.join(
  ROOT,
  "examples",
  "cache-key-diagnosis",
  "bundle",
);
const referenceLedgerText = fs.readFileSync(
  path.join(referenceBundle, "ledger.jsonl"),
  "utf8",
);
const referenceCanonicalLedger = referenceLedgerText
  .trimEnd()
  .split("\n")
  .map((line) => `${canonicalJson(parseJson(line))}\n`)
  .join("");
const referenceReceipt = readJson(
  path.join(referenceBundle, "reasoning-receipt.json"),
);
assert.equal(
  referenceLedgerText,
  referenceCanonicalLedger,
  "reference ledger is not canonical RFC 8785 JSON Lines",
);
assert.equal(
  crypto
    .createHash("sha256")
    .update(referenceCanonicalLedger)
    .digest("hex"),
  referenceReceipt.source_ledger.digest.value,
  "JavaScript did not reproduce the reference receipt ledger digest",
);

const fixtureCount =
  validRecords.length +
  invalidRecords.length +
  validBundles.length +
  invalidBundles.length;
console.log(
  `PASS JavaScript conformance: ${fixtureCount} fixtures, ` +
    `${validBundles.length} deterministic bundle replays`,
);
