# SPDX-License-Identifier: MPL-2.0
# Copyright (C) 2026 Greater Expanse LLC

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Collection
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Final, cast

import pytest

from itself import (
    BundleIntegrityError,
    BundleValidator,
    ClaimStatus,
    EvidenceBundleValidator,
    IntegrityCode,
    JsonlLedgerStore,
    JsonObject,
    Ledger,
    ValidationPolicy,
)
from itself._json import strict_json_loads
from itself.bundle import _at_or_before  # pyright: ignore[reportPrivateUsage]

ROOT: Final = Path(__file__).resolve().parents[1]
BUNDLES: Final = ROOT / "conformance" / "bundles"
FIXTURES: Final = sorted(BUNDLES.glob("*/*.json"))
HISTORY: Final = BUNDLES / "valid" / "evidence-backed-history.json"
TRUSTED: Final = ("default-transition-policy", "reviewer-alex")
POLICY_CODES: Final = frozenset(
    {
        IntegrityCode.ACTOR_TYPE_CONFLICT,
        IntegrityCode.UNSUPPORTED_RELIANCE,
        IntegrityCode.EXPIRED_SUPPORT,
    }
)
EXPIRY: Final = "2026-08-01T00:00:00Z"
AT_EXPIRY: Final = datetime(2026, 8, 1, tzinfo=UTC)
BEFORE_EXPIRY: Final = AT_EXPIRY - timedelta(microseconds=1)

Issue = tuple[IntegrityCode, str, str | None]


def _load(path: Path) -> list[JsonObject]:
    value = strict_json_loads(path.read_bytes())
    assert isinstance(value, list)
    return cast(list[JsonObject], value)


def _history() -> list[JsonObject]:
    return _load(HISTORY)


def _record(records: list[JsonObject], record_id: str) -> JsonObject:
    return next(record for record in records if record["id"] == record_id)


def _insert_after(
    records: list[JsonObject], record_id: str, *added: JsonObject
) -> list[JsonObject]:
    position = [record["id"] for record in records].index(record_id) + 1
    return [*records[:position], *added, *records[position:]]


def _issues(
    validator: BundleValidator,
    records: list[JsonObject],
    *,
    external_refs: Collection[str] = (),
) -> list[Issue]:
    return [
        (issue.code, issue.record_id, issue.reference)
        for issue in validator.errors(records, external_refs=external_refs)
    ]


def _policy(
    *,
    require_supported_reliance: bool = False,
    as_of: datetime | None = None,
) -> BundleValidator:
    return BundleValidator(
        policy=ValidationPolicy(
            require_supported_reliance=require_supported_reliance, as_of=as_of
        )
    )


def _actor(actor_id: str, actor_type: str, role: str) -> JsonObject:
    return {"id": actor_id, "actor_type": actor_type, "role": role}


REVIEWER: Final = _actor("reviewer-alex", "human", "reviewer")
MODEL: Final = _actor("diagnostician-model", "model", "proposer")


def _transition(
    record_id: str,
    from_status: str,
    to_status: str,
    actor: JsonObject,
) -> JsonObject:
    return {
        "protocol_version": "0.1.0-alpha.3",
        "kind": "status_transition",
        "id": record_id,
        "created_at": "2026-07-22T13:00:00Z",
        "created_by": dict(actor),
        "subject_ref": "claim-cache-cause",
        "from_status": from_status,
        "to_status": to_status,
        "authorized_by": dict(actor),
        "reason": "The deployment the claim was scoped to has changed.",
    }


def _decision(
    disposition: str,
    *relied_on: str,
    record_id: str = "decision-cache-fix",
    actor: JsonObject = REVIEWER,
) -> JsonObject:
    decision: JsonObject = {
        "protocol_version": "0.1.0-alpha.3",
        "kind": "decision",
        "id": record_id,
        "created_at": "2026-07-22T13:00:00Z",
        "created_by": dict(actor),
        "question": "May the cache fix ship?",
        "disposition": disposition,
        "authorized_by": dict(actor),
        "scope": {"description": "Controlled cache-failure case, harness version 1"},
    }
    if relied_on:
        decision["relied_on_claim_refs"] = list(relied_on)
    return decision


# The policy object and how validators accept it.


def test_no_policy_is_an_empty_policy() -> None:
    validator = BundleValidator()

    assert validator.policy == ValidationPolicy()
    assert validator.policy == ValidationPolicy(
        trusted_authorizers=None, require_supported_reliance=False, as_of=None
    )
    assert validator.trusted_authorizers is None
    assert EvidenceBundleValidator().policy is None


def test_trusted_authorizers_keyword_is_a_policy_with_only_that_list() -> None:
    validator = BundleValidator(trusted_authorizers=["reviewer-alex", "reviewer-alex"])

    assert validator.policy == ValidationPolicy(
        trusted_authorizers=frozenset({"reviewer-alex"})
    )
    assert validator.trusted_authorizers == frozenset({"reviewer-alex"})
    assert BundleValidator(
        policy=ValidationPolicy(trusted_authorizers=["reviewer-alex"])
    ).trusted_authorizers == frozenset({"reviewer-alex"})


@pytest.mark.parametrize("trusted", [(), ["reviewer-alex"]], ids=["empty", "listed"])
@pytest.mark.parametrize(
    "build",
    [BundleValidator, EvidenceBundleValidator],
    ids=["bundle", "evidence-bundle"],
)
def test_trusted_authorizers_and_policy_cannot_be_combined(
    build: type[BundleValidator] | type[EvidenceBundleValidator],
    trusted: list[str],
) -> None:
    with pytest.raises(ValueError, match="trusted_authorizers or policy, not both"):
        build(trusted_authorizers=trusted, policy=ValidationPolicy())


@pytest.mark.parametrize("value", [object(), {"trusted_authorizers": ()}, "policy"])
def test_policy_must_be_a_validation_policy(value: object) -> None:
    with pytest.raises(ValueError, match="policy must be a ValidationPolicy"):
        BundleValidator(policy=value)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="policy must be a ValidationPolicy"):
        EvidenceBundleValidator(policy=value)  # type: ignore[arg-type]


def test_evidence_bundle_validator_keeps_the_policy_it_was_given() -> None:
    policy = ValidationPolicy(
        trusted_authorizers=["a"], require_supported_reliance=True
    )
    validator = EvidenceBundleValidator(policy=policy)

    assert validator.policy is policy
    assert validator.trusted_authorizers is None
    assert dataclasses.replace(validator).policy is policy


class _NoOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> timedelta | None:
        return None

    def dst(self, dt: datetime | None) -> timedelta | None:
        return None

    def tzname(self, dt: datetime | None) -> str | None:
        return None


@pytest.mark.parametrize(
    "as_of",
    [
        datetime(2026, 8, 1),
        datetime(2026, 8, 1, tzinfo=_NoOffset()),
        "2026-08-01T00:00:00Z",
        1_785_542_400,
    ],
    ids=["naive", "no-offset", "string", "number"],
)
def test_as_of_must_be_a_timezone_aware_datetime(as_of: object) -> None:
    with pytest.raises(ValueError, match="as_of must be a timezone-aware datetime"):
        ValidationPolicy(as_of=as_of)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [1, 0, "yes", None])
def test_require_supported_reliance_must_be_a_boolean(value: object) -> None:
    with pytest.raises(
        ValueError, match="require_supported_reliance must be a boolean"
    ):
        ValidationPolicy(require_supported_reliance=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["reviewer-alex", [""], [3]])
def test_policy_refuses_invalid_trusted_authorizers(value: object) -> None:
    with pytest.raises(ValueError, match="trusted_authorizers"):
        ValidationPolicy(trusted_authorizers=value)  # type: ignore[arg-type]


def test_policy_is_frozen() -> None:
    policy = ValidationPolicy()

    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.as_of = AT_EXPIRY  # type: ignore[misc]


@pytest.mark.parametrize(
    "fixture", FIXTURES, ids=lambda path: f"{path.parent.name}/{path.name}"
)
def test_fixtures_validate_identically_without_a_policy(fixture: Path) -> None:
    records = _load(fixture)
    default = BundleValidator().errors(records)

    assert BundleValidator(policy=ValidationPolicy()).errors(records) == default
    assert BundleValidator(trusted_authorizers=None).errors(records) == default
    assert not {issue.code for issue in default} & POLICY_CODES
    assert (default == []) == (fixture.parent.name == "valid")


# Trust lists cover transitions out of an evidence-backed status.


def _demoted_history(actor: JsonObject) -> list[JsonObject]:
    return [
        *_history(),
        _transition(
            "transition-cache-stale", "corroborated_within_scope", "stale", actor
        ),
    ]


def test_trust_list_refuses_an_untrusted_demotion() -> None:
    records = _demoted_history(MODEL)

    issues = BundleValidator(trusted_authorizers=TRUSTED).errors(records)

    assert BundleValidator().validate(records).current_states == {
        "claim-cache-cause": ClaimStatus.STALE
    }
    assert [(issue.code, issue.record_id, issue.reference) for issue in issues] == [
        (
            IntegrityCode.UNTRUSTED_AUTHORIZER,
            "transition-cache-stale",
            "diagnostician-model",
        )
    ]
    assert issues[0].message == (
        "transition out of an evidence-backed status is authorized by an actor "
        "that is not trusted to authorize it"
    )


def test_a_refused_demotion_does_not_apply() -> None:
    records = [
        *_demoted_history(MODEL),
        _transition("transition-cache-retest", "stale", "under_test", REVIEWER),
    ]

    assert _issues(BundleValidator(trusted_authorizers=TRUSTED), records) == [
        (
            IntegrityCode.UNTRUSTED_AUTHORIZER,
            "transition-cache-stale",
            "diagnostician-model",
        ),
        (IntegrityCode.STATE_MISMATCH, "transition-cache-retest", "claim-cache-cause"),
    ]


def test_trust_list_accepts_a_trusted_demotion_and_later_moves() -> None:
    # Once the claim has left the evidence-backed statuses, moving it between
    # other statuses needs no trusted authorizer.
    records = [
        *_demoted_history(REVIEWER),
        _transition("transition-cache-retest", "stale", "under_test", MODEL),
    ]

    snapshot = BundleValidator(trusted_authorizers=TRUSTED).validate(records)

    assert snapshot.current_states == {"claim-cache-cause": ClaimStatus.UNDER_TEST}


# Trust lists refuse an actor id declared with more than one actor type.


def _conflicting_created_by() -> list[JsonObject]:
    records = _history()
    cast(JsonObject, _record(records, "evidence-cache-review")["created_by"])[
        "actor_type"
    ] = "organization"
    return records


def _conflicting_transition_authorizer() -> list[JsonObject]:
    records = _history()
    cast(
        JsonObject, _record(records, "transition-cache-corroborated")["authorized_by"]
    )["actor_type"] = "organization"
    return records


def _conflicting_decision_authorizer() -> list[JsonObject]:
    decision = _decision("approved", "claim-cache-cause")
    cast(JsonObject, decision["authorized_by"])["actor_type"] = "software"
    return [*_history(), decision]


def _conflicting_decision_creator() -> list[JsonObject]:
    decision = _decision(
        "approved",
        "claim-cache-cause",
        actor=_actor("local-runner", "human", "operator"),
    )
    decision["authorized_by"] = dict(REVIEWER)
    return [*_history(), decision]


@pytest.mark.parametrize(
    ("build", "record_id", "actor_id", "declared", "earlier"),
    [
        (
            _conflicting_created_by,
            "verdict-cache-corroborated",
            "reviewer-alex",
            "human",
            "organization",
        ),
        (
            _conflicting_transition_authorizer,
            "transition-cache-corroborated",
            "reviewer-alex",
            "organization",
            "human",
        ),
        (
            _conflicting_decision_authorizer,
            "decision-cache-fix",
            "reviewer-alex",
            "software",
            "human",
        ),
        (
            _conflicting_decision_creator,
            "decision-cache-fix",
            "local-runner",
            "human",
            "software",
        ),
    ],
    ids=[
        "record-creator",
        "transition-authorizer",
        "decision-authorizer",
        "decision-creator",
    ],
)
def test_trust_list_refuses_an_actor_declared_with_two_types(
    build: Callable[[], list[JsonObject]],
    record_id: str,
    actor_id: str,
    declared: str,
    earlier: str,
) -> None:
    records = build()

    issues = BundleValidator(trusted_authorizers=TRUSTED).errors(records)

    assert BundleValidator().errors(records) == []
    # The conflicting actor appears again later, but is reported once, on the
    # record where its second type first appears.
    assert [(issue.code, issue.record_id, issue.reference) for issue in issues] == [
        (IntegrityCode.ACTOR_TYPE_CONFLICT, record_id, actor_id)
    ]
    assert issues[0].message == (
        f"actor is declared as {declared} here but as {earlier} earlier in the bundle"
    )


def test_actor_type_conflicts_are_reported_per_actor() -> None:
    records = _conflicting_created_by()
    cast(JsonObject, _record(records, "transition-cache-under-test")["authorized_by"])[
        "actor_type"
    ] = "human"

    assert _issues(BundleValidator(trusted_authorizers=TRUSTED), records) == [
        (
            IntegrityCode.ACTOR_TYPE_CONFLICT,
            "transition-cache-under-test",
            "local-runner",
        ),
        (
            IntegrityCode.ACTOR_TYPE_CONFLICT,
            "verdict-cache-corroborated",
            "reviewer-alex",
        ),
    ]


# Supported reliance.


@pytest.mark.parametrize("disposition", ["approved", "executed"])
@pytest.mark.parametrize(
    "after", ["transition-cache-supported", "transition-cache-corroborated"]
)
def test_supported_reliance_accepts_a_decision_on_a_supported_claim(
    disposition: str, after: str
) -> None:
    records = _insert_after(
        _history(), after, _decision(disposition, "claim-cache-cause")
    )

    assert _policy(require_supported_reliance=True).errors(records) == []


@pytest.mark.parametrize("disposition", ["approved", "executed"])
def test_supported_reliance_refuses_a_decision_before_support(disposition: str) -> None:
    records = _insert_after(
        _history(),
        "transition-cache-under-test",
        _decision(disposition, "claim-cache-cause"),
    )

    issues = _policy(require_supported_reliance=True).errors(records)

    assert BundleValidator().errors(records) == []
    assert [(issue.code, issue.record_id, issue.reference) for issue in issues] == [
        (IntegrityCode.UNSUPPORTED_RELIANCE, "decision-cache-fix", "claim-cache-cause")
    ]
    assert issues[0].message == (
        f"{disposition} decision relies on a subject that has replayed status "
        "under_test at the decision"
    )


def test_supported_reliance_refuses_a_decision_before_the_claim() -> None:
    records = [_decision("approved", "claim-cache-cause"), *_history()]

    issues = _policy(require_supported_reliance=True).errors(records)

    assert BundleValidator().errors(records) == []
    assert [(issue.code, issue.reference, issue.message) for issue in issues] == [
        (
            IntegrityCode.UNSUPPORTED_RELIANCE,
            "claim-cache-cause",
            "approved decision relies on a subject that has no replayed status "
            "before the decision",
        )
    ]


def test_supported_reliance_follows_a_demotion() -> None:
    records = [
        *_demoted_history(REVIEWER),
        _decision("executed", "claim-cache-cause"),
    ]

    assert _issues(_policy(require_supported_reliance=True), records) == [
        (IntegrityCode.UNSUPPORTED_RELIANCE, "decision-cache-fix", "claim-cache-cause")
    ]


@pytest.mark.parametrize("disposition", ["rejected", "deferred", "escalated"])
def test_supported_reliance_ignores_decisions_that_do_not_act(disposition: str) -> None:
    records = _insert_after(
        _history(),
        "transition-cache-under-test",
        _decision(disposition, "claim-cache-cause"),
    )

    assert _policy(require_supported_reliance=True).errors(records) == []


def test_supported_reliance_reports_each_unsupported_subject() -> None:
    records = _insert_after(
        _history(),
        "transition-cache-supported",
        _decision("approved", "claim-cache-cause", "claim-elsewhere", "claim-later"),
    )
    records.append({**_history()[0], "id": "claim-later"})

    issues = _issues(
        _policy(require_supported_reliance=True),
        records,
        external_refs={"claim-elsewhere"},
    )

    assert issues == [
        (IntegrityCode.UNSUPPORTED_RELIANCE, "decision-cache-fix", "claim-later")
    ]


def test_supported_reliance_skips_only_declared_external_references() -> None:
    records = [*_history(), _decision("approved", "claim-elsewhere")]
    validator = _policy(require_supported_reliance=True)

    assert validator.errors(records, external_refs={"claim-elsewhere"}) == []
    assert _issues(validator, records) == [
        (
            IntegrityCode.UNRESOLVED_REFERENCE,
            "decision-cache-fix",
            "claim-elsewhere",
        ),
        (IntegrityCode.UNSUPPORTED_RELIANCE, "decision-cache-fix", "claim-elsewhere"),
    ]


def test_supported_reliance_checks_a_local_claim_also_declared_external() -> None:
    records = _insert_after(
        _history(),
        "transition-cache-under-test",
        _decision("approved", "claim-cache-cause"),
    )

    assert _issues(
        _policy(require_supported_reliance=True),
        records,
        external_refs={"claim-cache-cause"},
    ) == [
        (IntegrityCode.UNSUPPORTED_RELIANCE, "decision-cache-fix", "claim-cache-cause")
    ]


# Expiry at an as-of time.


def _scoped_until(valid_until: str) -> list[JsonObject]:
    # Verdicts require identical scopes, so the claim and its evidence share one.
    records = _history()
    for record in records:
        scope = record.get("scope")
        if isinstance(scope, dict):
            scope["valid_until"] = valid_until
    return records


def test_as_of_refuses_support_whose_scope_has_expired() -> None:
    records = _scoped_until(EXPIRY)

    issues = _policy(as_of=AT_EXPIRY).errors(records)

    assert _policy(as_of=BEFORE_EXPIRY).errors(records) == []
    assert [(issue.code, issue.record_id, issue.reference) for issue in issues] == [
        (IntegrityCode.EXPIRED_SUPPORT, "claim-cache-cause", None),
        (IntegrityCode.EXPIRED_SUPPORT, "claim-cache-cause", "evidence-cache-ablation"),
        (IntegrityCode.EXPIRED_SUPPORT, "claim-cache-cause", "evidence-cache-review"),
    ]
    assert issues[0].message == (
        "corroborated_within_scope status has expired: scope valid_until "
        "2026-08-01T00:00:00Z is at or before 2026-08-01T00:00:00+00:00"
    )
    assert issues[1].message == (
        "corroborated_within_scope status rests on evidence whose scope "
        "valid_until 2026-08-01T00:00:00Z is at or before 2026-08-01T00:00:00+00:00"
    )


def test_as_of_compares_instants_across_offsets() -> None:
    records = _scoped_until("2026-08-01T02:00:00+02:00")
    plus_five = timezone(timedelta(hours=5))

    assert (
        _policy(as_of=datetime(2026, 8, 1, 4, 59, tzinfo=plus_five)).errors(records)
        == []
    )
    assert {
        issue.code
        for issue in _policy(as_of=datetime(2026, 8, 1, 5, tzinfo=plus_five)).errors(
            records
        )
    } == {IntegrityCode.EXPIRED_SUPPORT}


def test_as_of_refuses_support_resting_on_expired_evidence() -> None:
    records = _history()
    _record(records, "evidence-cache-review")["expires_at"] = EXPIRY

    issues = _policy(as_of=AT_EXPIRY).errors(records)

    assert _policy(as_of=BEFORE_EXPIRY).errors(records) == []
    assert [(issue.code, issue.record_id, issue.reference) for issue in issues] == [
        (IntegrityCode.EXPIRED_SUPPORT, "claim-cache-cause", "evidence-cache-review")
    ]
    assert issues[0].message == (
        "corroborated_within_scope status rests on evidence whose expires_at "
        "2026-08-01T00:00:00Z is at or before 2026-08-01T00:00:00+00:00"
    )


def _corroborated_by_review_only() -> list[JsonObject]:
    records = _history()
    for record_id in ("verdict-cache-corroborated", "transition-cache-corroborated"):
        _record(records, record_id)["evidence_refs"] = ["evidence-cache-review"]
    return records


def test_as_of_checks_only_the_transition_behind_the_current_status() -> None:
    records = _corroborated_by_review_only()
    _record(records, "evidence-cache-ablation")["expires_at"] = EXPIRY

    assert BundleValidator().errors(records) == []
    assert _policy(as_of=AT_EXPIRY).errors(records) == []
    _record(records, "evidence-cache-review")["expires_at"] = EXPIRY
    assert _issues(_policy(as_of=AT_EXPIRY), records) == [
        (IntegrityCode.EXPIRED_SUPPORT, "claim-cache-cause", "evidence-cache-review")
    ]


def test_as_of_checks_supported_status() -> None:
    records = _scoped_until(EXPIRY)
    position = [record["id"] for record in records].index("transition-cache-supported")

    assert _issues(_policy(as_of=AT_EXPIRY), records[: position + 1]) == [
        (IntegrityCode.EXPIRED_SUPPORT, "claim-cache-cause", None),
        (IntegrityCode.EXPIRED_SUPPORT, "claim-cache-cause", "evidence-cache-ablation"),
    ]


@pytest.mark.parametrize(
    "last", ["transition-cache-under-test", "transition-cache-stale"]
)
def test_as_of_ignores_subjects_that_are_not_supported(last: str) -> None:
    records = [
        *_scoped_until(EXPIRY),
        _transition(
            "transition-cache-stale", "corroborated_within_scope", "stale", REVIEWER
        ),
    ]
    for record in records:
        if record["kind"] == "evidence":
            record["expires_at"] = EXPIRY
    position = [record["id"] for record in records].index(last)

    assert _policy(as_of=AT_EXPIRY).errors(records[: position + 1]) == []


@pytest.mark.parametrize(
    ("value", "instant", "expected"),
    [
        ("2026-08-01T00:00:00Z", AT_EXPIRY, True),
        ("2026-08-01T00:00:00Z", BEFORE_EXPIRY, False),
        ("2026-08-01t00:00:00z", AT_EXPIRY, True),
        ("2026-08-01T02:00:00+02:00", AT_EXPIRY, True),
        ("2026-08-01T02:00:00+02:00", BEFORE_EXPIRY, False),
        ("2026-07-31T20:00:00-04:00", AT_EXPIRY, True),
        ("2026-07-31T23:59:59.999999Z", BEFORE_EXPIRY, True),
        ("2026-07-31T23:59:59.9999990Z", BEFORE_EXPIRY, True),
        ("2026-07-31T23:59:59.9999991Z", BEFORE_EXPIRY, False),
        ("2026-07-31T23:59:59.9999991Z", AT_EXPIRY, True),
        ("2026-07-31T23:59:60Z", BEFORE_EXPIRY, False),
        ("2026-07-31T23:59:60.5Z", AT_EXPIRY, True),
        ("2026-02-29T00:00:00Z", datetime(2026, 3, 1, tzinfo=UTC), True),
        ("2024-02-29T23:59:59Z", datetime(2024, 2, 29, 23, 59, 58, tzinfo=UTC), False),
        ("0000-01-01T00:00:00+23:59", datetime.min.replace(tzinfo=UTC), True),
        ("9999-12-31T23:59:59-23:59", datetime.max.replace(tzinfo=UTC), False),
    ],
)
def test_expiry_timestamps_compare_as_instants(
    value: str, instant: datetime, expected: bool
) -> None:
    assert _at_or_before(value, "expires_at", instant) is expected


# The policy applies wherever a bundle validator does.


def test_ledger_applies_the_policy_to_each_append() -> None:
    records = _insert_after(
        _history(),
        "transition-cache-under-test",
        _decision("approved", "claim-cache-cause"),
    )
    position = [record["id"] for record in records].index("decision-cache-fix")
    ledger = Ledger(
        records[:position],
        validator=_policy(require_supported_reliance=True),
    )

    with pytest.raises(BundleIntegrityError) as raised:
        ledger.append(records[position])

    assert [issue.code for issue in raised.value.issues] == [
        IntegrityCode.UNSUPPORTED_RELIANCE
    ]
    assert len(ledger) == position


def test_ledger_store_applies_the_policy_to_stored_history(tmp_path: Path) -> None:
    path = tmp_path / "ledger.jsonl"
    records = _history()
    _record(records, "evidence-cache-review")["expires_at"] = EXPIRY
    JsonlLedgerStore(path).extend(records)
    stored = path.read_bytes()

    with pytest.raises(BundleIntegrityError) as raised:
        JsonlLedgerStore(path, validator=_policy(as_of=AT_EXPIRY)).append(
            _decision("rejected")
        )

    assert [issue.code for issue in raised.value.issues] == [
        IntegrityCode.EXPIRED_SUPPORT
    ]
    assert path.read_bytes() == stored
    assert len(
        JsonlLedgerStore(path, validator=_policy(as_of=BEFORE_EXPIRY)).load()
    ) == (len(records))
