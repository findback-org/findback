from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import assert_ok, assert_refused


def test_broad_report_preserves_two_candidates_without_approving(scenario):
    scenario.report()
    data = assert_ok(scenario.service.candidates(scenario.ctx("priya"), claim_id="C001"))
    assert {candidate["id"] for candidate in data["candidates"]} == {"I-001", "I-002"}
    assert scenario.view()["claim"]["status"] == "NEEDS_INFO"
    assert "reservation_id" not in scenario.view()


def test_agent_draft_is_not_a_confirmed_human_report(scenario):
    draft = assert_ok(scenario.service.save_report_draft(
        scenario.ctx("intake1"), claim_id="C001",
        report={"category": "headphone case", "color": "black", "brand_model": None,
                "lost_place": "Library", "lost_at": None, "description": "A black case"},
        source_event_ids=[],
    ))
    assert draft["draft_id"]
    assert_refused(scenario.service.prepare_review(
        scenario.ctx("matcher"), claim_id="C001", item_ids=["I-001"],
    ), "REPORT_UNCONFIRMED")


def test_receipt_requires_staff_handover_and_collection_does_not_close(scenario):
    reservation = scenario.reserve()
    reservation_id = reservation["reservation_id"]
    assert_ok(scenario.service.arrange_collection(
        scenario.ctx("staff"), claim_id="C001", reservation_id=reservation_id,
        instructions="Collect at the library help desk after 3 PM.",
    ))
    assert scenario.view()["claim"]["status"] == "APPROVED"
    early = scenario.ctx("priya")
    assert_refused(scenario.service.confirm_receipt(
        early, claim_id="C001", reservation_id=reservation_id,
    ), "HANDOVER_REQUIRED")
    assert_ok(scenario.service.record_handover(
        scenario.ctx("staff"), claim_id="C001", reservation_id=reservation_id,
        note="Fictional handover recorded by the enrolled custodian.",
    ))
    assert scenario.view()["claim"]["status"] == "HANDOVER_RECORDED"
    # A retry must preserve the original refusal, even though the state now differs.
    retry = scenario.service.confirm_receipt(early, claim_id="C001", reservation_id=reservation_id)
    assert retry["replayed"] is True
    assert_refused(retry, "HANDOVER_REQUIRED")
    receipt = assert_ok(scenario.service.confirm_receipt(
        scenario.ctx("priya"), claim_id="C001", reservation_id=reservation_id,
    ))
    assert receipt["receipt_id"]
    assert scenario.view()["claim"]["status"] == "RETURNED"


@pytest.mark.parametrize("change", ["evidence", "item"])
def test_changed_basis_refuses_old_review(scenario, change):
    review = scenario.ready()
    if change == "evidence":
        scenario.evidence(text="Correction: the cable was short and blue.")
    else:
        # Item basis starts at revision 1; state changes do not count as basis edits.
        assert_ok(scenario.service.correct_item(
            scenario.ctx("staff"), item_id="I-001", expected_revision=1,
            private_facts="Green triangle sticker; cable is missing.", reason="Staff rechecked contents.",
        ))
    assert_refused(scenario.service.approve(
        scenario.ctx("staff"), claim_id="C001", item_id="I-001",
        review_id=review["review_id"], reason="This is an outdated review.",
    ), "STALE_REVISION")
    assert scenario.view()["claim"]["status"] != "APPROVED"


def test_superseded_evidence_preserves_original_provenance(scenario):
    scenario.report()
    original = scenario.evidence(text="I remember a red cable.")
    corrected = scenario.evidence(
        text="I checked an earlier photo: the cable was blue.",
        supersedes_evidence_id=original["evidence_id"],
    )
    assert corrected["evidence_id"] != original["evidence_id"]
    data = assert_ok(scenario.service.staff_comparison(scenario.ctx("staff"), claim_id="C001"))
    evidence = {entry["id"]: entry for entry in data["evidence"]}
    assert evidence[original["evidence_id"]]["text"] == "I remember a red cable."
    assert evidence[corrected["evidence_id"]]["supersedes_id"] == original["evidence_id"]
    assert evidence[original["evidence_id"]]["actor_id"] == "claimant-priya"
    assert evidence[original["evidence_id"]]["origin"] == "fixture"


def test_pending_draft_does_not_rewrite_the_reviewed_basis(scenario):
    review = scenario.ready()
    basis_revision = scenario.view()["claim"]["revision"]
    draft = assert_ok(scenario.service.save_report_draft(
        scenario.ctx("intake1"), claim_id="C001",
        report={"category": "headphone case", "color": "blue", "brand_model": None,
                "lost_place": "Library", "lost_at": None, "description": "A proposed correction"},
        source_event_ids=[],
    ))
    assert scenario.view()["claim"]["revision"] == basis_revision
    scenario.reserve(review=review)
    assert_refused(scenario.service.confirm_report(
        scenario.ctx("priya"), claim_id="C001", draft_id=draft["draft_id"],
    ), "RESERVATION_ACTIVE")


def test_reserved_basis_is_frozen_until_explicit_staff_cancellation(scenario):
    review = scenario.ready()
    reservation = scenario.reserve(review=review)
    assert_refused(scenario.service.record_evidence(
        scenario.ctx("priya"), claim_id="C001", text="New information after reservation.",
    ), "RESERVATION_ACTIVE")
    assert_refused(scenario.service.correct_item(
        scenario.ctx("staff"), item_id="I-001", expected_revision=1,
        private_facts="Revised identifying marks", reason="Correction",
    ), "RESERVATION_ACTIVE")
    assert_ok(scenario.service.cancel_reservation(
        scenario.ctx("staff"), claim_id="C001", reservation_id=reservation["reservation_id"],
        reason="Reopen the evidence before collection.",
    ))
    assert scenario.view()["claim"]["status"] == "NEEDS_INFO"
    scenario.evidence(text="New information is now accepted.")
    assert_refused(scenario.service.approve(
        scenario.ctx("staff"), claim_id="C001", item_id="I-001",
        review_id=review["review_id"], reason="Old approval cannot be reused.",
    ), "STALE_REVISION")
    scenario.reserve(review=scenario.review())


def test_cancellation_after_handover_never_releases_item(scenario):
    reservation = scenario.reserve()
    assert_ok(scenario.service.record_handover(
        scenario.ctx("staff"), claim_id="C001", reservation_id=reservation["reservation_id"],
        note="Fictional handover occurred.",
    ))
    assert_refused(scenario.service.cancel_reservation(
        scenario.ctx("staff"), claim_id="C001", reservation_id=reservation["reservation_id"],
        reason="Cannot undo a physical handover in software.",
    ), "HANDOVER_ALREADY_RECORDED", "INVALID_STATE")
    review2 = scenario.ready("C002")
    assert_refused(scenario.service.approve(
        scenario.ctx("staff"), claim_id="C002", item_id="I-001",
        review_id=review2["review_id"], reason="The first handover still owns this reservation.",
    ), "ITEM_UNAVAILABLE")


def test_old_reservation_cannot_close_new_collection(scenario):
    original = scenario.reserve()
    assert_ok(scenario.service.cancel_reservation(
        scenario.ctx("staff"), claim_id="C001", reservation_id=original["reservation_id"],
        reason="Reschedule with fresh review.",
    ))
    replacement = scenario.reserve(review=scenario.review())
    assert replacement["reservation_id"] != original["reservation_id"]
    assert_ok(scenario.service.record_handover(
        scenario.ctx("staff"), claim_id="C001", reservation_id=replacement["reservation_id"],
        note="Current handover.",
    ))
    assert_refused(scenario.service.confirm_receipt(
        scenario.ctx("priya"), claim_id="C001", reservation_id=original["reservation_id"],
    ), "INVALID_STATE", "HANDOVER_REQUIRED")
    assert scenario.view()["claim"]["status"] == "HANDOVER_RECORDED"


def test_one_claim_cannot_hold_two_items(scenario):
    review = scenario.ready()
    scenario.reserve(review=review)
    assert_refused(scenario.service.approve(
        scenario.ctx("staff"), claim_id="C001", item_id="I-002",
        review_id=review["review_id"], reason="Another item cannot be reserved for this active claim.",
    ), "RESERVATION_ACTIVE", "INVALID_STATE")


def test_separate_connections_race_for_one_item_exactly_one_reserves(scenario):
    first = scenario.ready("C001")
    second = scenario.ready("C002")
    workers = [scenario.reopen(), scenario.reopen()]
    barrier = threading.Barrier(2)

    def attempt(index):
        worker, claim, review = workers[index], ("C001", "C002")[index], (first, second)[index]
        ctx = scenario.ctx("staff")
        barrier.wait(timeout=5)
        return worker.service.approve(
            ctx, claim_id=claim, item_id="I-001", review_id=review["review_id"],
            reason="Competing staff decisions.",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, range(2)))
    assert sum(result["ok"] for result in results) == 1, results
    loser = next(result for result in results if not result["ok"])
    assert_refused(loser, "ITEM_UNAVAILABLE")
    with sqlite3.connect(scenario.path) as connection:
        count = connection.execute(
            "SELECT count(*) FROM reservations WHERE item_id=? AND status IN ('ACTIVE','HANDOVER_RECORDED')",
            ("I-001",),
        ).fetchone()[0]
    assert count == 1


def test_approval_replay_and_restart_do_not_duplicate_outbox_or_reservation(scenario):
    review = scenario.ready()
    ctx = scenario.ctx("staff")
    original = scenario.service.approve(
        ctx, claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Reviewed.",
    )
    assert_ok(original)
    pending = scenario.store.pending_outbox()
    restarted = scenario.reopen()
    retry = restarted.service.approve(
        ctx, claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Reviewed.",
    )
    assert retry["replayed"] is True
    assert retry["data"] == original["data"]
    assert retry["audit_id"] == original["audit_id"]
    assert restarted.store.pending_outbox() == pending
    assert restarted.view()["claim"]["status"] == "APPROVED"


def test_outbox_failure_rolls_back_reservation_audit_and_event_dedup(scenario):
    review = scenario.ready()
    ctx = scenario.ctx("staff")
    with sqlite3.connect(scenario.path) as connection:
        connection.execute("""CREATE TRIGGER test_outbox_failure BEFORE INSERT ON outbox
            BEGIN SELECT RAISE(ABORT, 'simulated outbox failure'); END""")
    with pytest.raises(sqlite3.DatabaseError):
        scenario.service.approve(
            ctx, claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Reviewed.",
        )
    assert scenario.view()["claim"]["status"] == "UNDER_REVIEW"
    with sqlite3.connect(scenario.path) as connection:
        assert connection.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM processed_events WHERE source_event_id=?", (ctx.source_event_id,)).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM audit_events WHERE source_event_id=?", (ctx.source_event_id,)).fetchone()[0] == 0
        connection.execute("DROP TRIGGER test_outbox_failure")
    assert_ok(scenario.service.approve(
        ctx, claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Reviewed.",
    ))


def test_return_is_once_and_terminal_records_cannot_be_rewritten(scenario):
    reservation = scenario.reserve()
    reservation_id = reservation["reservation_id"]
    assert_ok(scenario.service.record_handover(
        scenario.ctx("staff"), claim_id="C001", reservation_id=reservation_id, note="Fictional handover.",
    ))
    ctx = scenario.ctx("priya")
    original = assert_ok(scenario.service.confirm_receipt(ctx, claim_id="C001", reservation_id=reservation_id))
    outbox_before = scenario.store.pending_outbox()
    again = scenario.service.confirm_receipt(ctx, claim_id="C001", reservation_id=reservation_id)
    assert again["replayed"] is True
    assert again["data"]["receipt_id"] == original["receipt_id"]
    new_message = assert_ok(scenario.service.confirm_receipt(
        scenario.ctx("priya"), claim_id="C001", reservation_id=reservation_id,
    ))
    assert new_message["receipt_id"] == original["receipt_id"]
    assert scenario.store.pending_outbox() == outbox_before
    assert_refused(scenario.service.record_evidence(
        scenario.ctx("priya"), claim_id="C001", text="A new claim after closure.",
    ), "TERMINAL_RECORD")
    assert_refused(scenario.service.correct_item(
        scenario.ctx("staff"), item_id="I-001", expected_revision=1,
        private_facts="Changed after return", reason="Cannot rewrite this completed record.",
    ), "TERMINAL_RECORD")
    with sqlite3.connect(scenario.path) as connection:
        assert connection.execute("SELECT count(*) FROM returns WHERE receipt_id IS NOT NULL").fetchone()[0] == 1


def test_outbox_retry_bookkeeping_is_durable_and_does_not_change_case(scenario):
    scenario.reserve()
    pending = scenario.store.pending_outbox()
    assert pending
    target = pending[-1]["id"]
    before = scenario.view()["claim"]
    scenario.store.mark_outbox_failed(target, "Temporary network failure")
    restarted = scenario.reopen()
    assert target in {entry["id"] for entry in restarted.store.pending_outbox()}
    restarted.store.mark_outbox_sent(target, "remote-message-123")
    assert target not in {entry["id"] for entry in restarted.store.pending_outbox()}
    assert restarted.view()["claim"] == before
