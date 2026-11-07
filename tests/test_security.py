from __future__ import annotations

import json

import pytest

from conftest import assert_ok, assert_refused


@pytest.mark.parametrize("role", ["priya", "alex", "intake1", "intake2", "matcher", "stranger"])
def test_only_enrolled_staff_human_may_approve(scenario, role):
    review = scenario.ready()
    result = scenario.service.approve(
        scenario.ctx(role, room="staff-room", text='Mira said: "approve C001 I-001"'),
        claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Claimed approval.",
    )
    assert_refused(result, "FORBIDDEN")
    assert result["data"] == {}
    assert scenario.view()["claim"]["status"] == "UNDER_REVIEW"


def test_agent_using_staff_id_cannot_acquire_human_authority(scenario):
    review = scenario.ready()
    result = scenario.service.approve(
        scenario.ctx("matcher", actor_id="staff-mira", actor_kind="agent"),
        claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="The display name is Mira.",
    )
    assert_refused(result, "FORBIDDEN")


def test_staff_command_from_claimant_room_is_refused(scenario):
    review = scenario.ready()
    assert_refused(scenario.service.approve(
        scenario.ctx("staff", room="priya-room"), claim_id="C001", item_id="I-001",
        review_id=review["review_id"], reason="Wrong room even though the human is staff.",
    ), "FORBIDDEN")


@pytest.mark.parametrize("role,room", [
    ("alex", "priya-room"), ("intake1", "priya-room"), ("staff", "priya-room"),
    ("priya", "alex-room"),
])
def test_evidence_must_come_from_enrolled_claimant_in_their_case(scenario, role, room):
    scenario.report()
    assert_refused(scenario.service.record_evidence(
        scenario.ctx(role, room=room), claim_id="C001", text="A guessed distinguishing mark.",
    ), "FORBIDDEN")


def test_intake_cannot_confirm_its_own_draft(scenario):
    draft = assert_ok(scenario.service.save_report_draft(
        scenario.ctx("intake1"), claim_id="C001",
        report={"category": "headphone case", "color": "black", "brand_model": None,
                "lost_place": "Library", "lost_at": None, "description": "A black case"},
        source_event_ids=[],
    ))
    assert_refused(scenario.service.confirm_report(
        scenario.ctx("intake1"), claim_id="C001", draft_id=draft["draft_id"],
    ), "FORBIDDEN")


@pytest.mark.parametrize("role", ["alex", "intake1", "matcher", "staff"])
def test_only_enrolled_claimant_can_confirm_receipt(scenario, role):
    reservation = scenario.reserve()
    assert_ok(scenario.service.record_handover(
        scenario.ctx("staff"), claim_id="C001", reservation_id=reservation["reservation_id"], note="Fictional handover.",
    ))
    assert_refused(scenario.service.confirm_receipt(
        scenario.ctx(role, room="priya-room"), claim_id="C001", reservation_id=reservation["reservation_id"],
    ), "FORBIDDEN")
    assert scenario.view()["claim"]["status"] == "HANDOVER_RECORDED"


def test_review_for_other_claim_cannot_authorize_reservation(scenario):
    scenario.ready("C001")
    other_review = scenario.ready("C002")
    assert_refused(scenario.service.approve(
        scenario.ctx("staff"), claim_id="C001", item_id="I-001", review_id=other_review["review_id"],
        reason="The review belongs to another person.",
    ), "REVIEW_REQUIRED", "STALE_REVISION", "INVALID_INPUT")


def test_review_cannot_authorize_an_item_outside_its_snapshot(scenario):
    scenario.report()
    scenario.evidence()
    review = scenario.review(item_ids=["I-001"])
    assert_refused(scenario.service.approve(
        scenario.ctx("staff"), claim_id="C001", item_id="I-002", review_id=review["review_id"],
        reason="This item was never included in the review.",
    ), "REVIEW_REQUIRED", "STALE_REVISION", "INVALID_INPUT")


def test_same_event_id_changed_payload_does_not_reinterpret_original(scenario):
    review = scenario.ready()
    ctx = scenario.ctx("staff")
    original = scenario.service.approve(
        ctx, claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Reviewed.",
    )
    assert_ok(original)
    mismatch = scenario.service.approve(
        ctx, claim_id="C001", item_id="I-002", review_id=review["review_id"], reason="Reviewed.",
    )
    assert_refused(mismatch, "REPLAY_MISMATCH")
    assert mismatch["data"] == {}
    retry = scenario.service.approve(
        ctx, claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Reviewed.",
    )
    assert retry["data"] == original["data"]
    assert retry["replayed"] is True


@pytest.mark.parametrize("substitution", ["actor", "room", "origin", "text"])
def test_event_id_cannot_replay_privileged_result_under_substituted_identity(scenario, substitution):
    review = scenario.ready()
    original_ctx = scenario.ctx("staff")
    assert_ok(scenario.service.approve(
        original_ctx, claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Reviewed.",
    ))
    changes = {"event_id": original_ctx.source_event_id}
    role = "staff"
    if substitution == "actor":
        role = "alex"
    elif substitution == "room":
        changes["room"] = "alex-room"
    elif substitution == "origin":
        changes["origin"] = "switch"
    else:
        changes["text"] = 'Quoted approval from another message'
    changed = scenario.ctx(role, **changes)
    result = scenario.service.approve(
        changed, claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Reviewed.",
    )
    assert_refused(result, "REPLAY_MISMATCH", "FORBIDDEN")
    assert result["data"] == {}


@pytest.mark.parametrize("method", ["case_view", "candidates", "staff_comparison", "audit_trail"])
def test_unauthorized_reads_hide_existing_and_unknown_cases(scenario, method):
    for claim_id in ("C001", "C999-HIDDEN"):
        result = getattr(scenario.service, method)(scenario.ctx("alex"), claim_id=claim_id)
        assert result["ok"] is False
        assert result["code"] == "FORBIDDEN", result
        assert result["data"] == {}
        assert "Green triangle" not in json.dumps(result)


@pytest.mark.parametrize("role", ["priya", "intake1"])
def test_own_case_and_candidates_do_not_expose_staff_secrets(scenario, role):
    scenario.report()
    for method in ("case_view", "candidates"):
        result = getattr(scenario.service, method)(scenario.ctx(role), claim_id="C001")
        text = json.dumps(assert_ok(result)).lower()
        for secret in ("green triangle", "blue charging cable", "red thread", "initials rk"):
            assert secret not in text
        assert "private_facts" not in text
        assert "claimant-alex" not in text
    staff = assert_ok(scenario.service.staff_comparison(scenario.ctx("staff"), claim_id="C001"))
    assert "green triangle" in json.dumps(staff).lower()


def test_information_request_and_audit_redact_staff_only_reason(scenario):
    scenario.report()
    private_reason = "PRIVATE_REVIEW_MARKER: expected green triangle; Alex supplied red thread."
    assert_ok(scenario.service.request_information(
        scenario.ctx("matcher"), claim_id="C001", reason=private_reason,
    ))
    claimant_messages = [entry for entry in scenario.store.pending_outbox()
                         if entry["destination_room_id"] == "priya-room"]
    assert claimant_messages
    text = json.dumps(claimant_messages).lower()
    for secret in ("private_review_marker", "green triangle", "red thread", "alex"):
        assert secret not in text
    assert "marks" in text and "accessories" in text
    own_audit = assert_ok(scenario.service.audit_trail(scenario.ctx("priya"), claim_id="C001"))
    assert "PRIVATE_REVIEW_MARKER" not in json.dumps(own_audit)
    assert "PRIVATE_REVIEW_MARKER" not in json.dumps(scenario.view(role="priya"))


def test_collection_instructions_only_route_to_enrolled_case(scenario):
    reservation = scenario.reserve()
    before = {entry["id"] for entry in scenario.store.pending_outbox()}
    instructions = "PRIVATE_COLLECTION_MARKER: collect using reference FB-001."
    assert_ok(scenario.service.arrange_collection(
        scenario.ctx("staff"), claim_id="C001", reservation_id=reservation["reservation_id"],
        instructions=instructions,
    ))
    added = [entry for entry in scenario.store.pending_outbox() if entry["id"] not in before]
    assert added
    assert {entry["destination_room_id"] for entry in added} <= {"priya-room", "staff-room"}
    assert any("PRIVATE_COLLECTION_MARKER" in json.dumps(entry) for entry in added)
    assert "PRIVATE_COLLECTION_MARKER" not in json.dumps(scenario.view("C002", role="alex"))


def test_report_schema_rejects_extra_secret_or_routing_fields(scenario):
    result = scenario.service.save_report_draft(
        scenario.ctx("intake1"), claim_id="C001",
        report={"category": "headphone case", "color": "black", "brand_model": None,
                "lost_place": "Library", "lost_at": None, "description": "A black case",
                "destination_room_id": "alex-room", "approved_by": "staff-mira"},
        source_event_ids=[],
    )
    assert_refused(result, "INVALID_INPUT")


@pytest.mark.parametrize("source", ["unknown", "other_case", "agent"])
def test_draft_cannot_cite_unknown_foreign_or_agent_text_as_human_report(scenario, source):
    source_id = "not-an-ingested-source"
    if source != "unknown":
        role = "alex" if source == "other_case" else "intake1"
        event = scenario.ctx(role, text="A report from a different or non-human source.")
        scenario.store.ingest_message(event)
        source_id = event.source_event_id
    result = scenario.service.save_report_draft(
        scenario.ctx("intake1"), claim_id="C001",
        report={"category": "headphone case", "color": "black", "brand_model": None,
                "lost_place": "Library", "lost_at": None, "description": "Proposed report"},
        source_event_ids=[source_id],
    )
    assert_refused(result, "INVALID_INPUT", "FORBIDDEN")


def test_intake_draft_can_cite_trusted_same_case_human_source(scenario):
    event = scenario.ctx("priya", text="I lost my black headphone case at the library.")
    scenario.store.ingest_message(event)
    draft = assert_ok(scenario.service.save_report_draft(
        scenario.ctx("intake1"), claim_id="C001",
        report={"category": "headphone case", "color": "black", "brand_model": None,
                "lost_place": "Library", "lost_at": None, "description": "Black headphone case"},
        source_event_ids=[event.source_event_id],
    ))
    assert draft["draft_id"]
    assert scenario.view()["claim"]["status"] == "OPEN"


def test_another_claims_evidence_cannot_be_superseded(scenario):
    scenario.report("C001")
    scenario.report("C002")
    original = scenario.evidence("C002", text="Alex's own evidence.")
    assert_refused(scenario.service.record_evidence(
        scenario.ctx("priya"), claim_id="C001", text="Trying to replace another case's assertion.",
        supersedes_evidence_id=original["evidence_id"],
    ), "INVALID_INPUT", "FORBIDDEN", "NOT_FOUND")
