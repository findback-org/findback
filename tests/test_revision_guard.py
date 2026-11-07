"""Regression checks for model-call races and cached-result authorization.

A model can take time to produce a comparison. Its answer must never be stamped
as if it had seen facts changed by another connection during that interval.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from conftest import ACTORS, assert_ok, assert_refused
from findback.domain import DomainService
from findback.storage import SQLiteStore


def _basis(data):
    return {
        "claim_revision": data["claim"]["revision"],
        "evidence_revision": data["claim"]["evidence_revision"],
        "item_revisions": {item["id"]: item["revision"] for item in data["items"]},
    }


def _counts(store):
    with store.connection() as db:
        return {
            table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("reviews", "reservations", "outbox")
        }


@pytest.mark.parametrize("intervening_change", ["evidence", "item"])
def test_model_comparison_cannot_bind_to_facts_it_did_not_see(scenario, intervening_change):
    scenario.report()
    scenario.evidence()
    original = assert_ok(scenario.service.staff_comparison(
        scenario.ctx("matcher"), claim_id="C001",
    ))
    # Treat this snapshot as the exact input supplied to a slow model call.
    expected = _basis(original)
    another_process = scenario.reopen()
    if intervening_change == "evidence":
        another_process.evidence(text="Correction supplied while the model was answering: the cable was absent.")
    else:
        assert_ok(another_process.service.correct_item(
            scenario.ctx("staff"), item_id="I-001", expected_revision=1,
            private_facts="Staff correction made while inference was running.",
            reason="The contents were rechecked.",
        ))
    before = _counts(scenario.store)
    stale_analysis = {"summary": "STALE_MODEL_ANSWER_MUST_NOT_BECOME_A_REVIEW"}
    refused = scenario.service.prepare_review(
        scenario.ctx("matcher"), claim_id="C001", item_ids=["I-001", "I-002"],
        analysis=stale_analysis, expected_basis=expected,
    )
    assert_refused(refused, "STALE_REVISION")
    assert refused["data"] == {}
    assert _counts(scenario.store) == before
    assert scenario.view()["claim"]["status"] == "NEEDS_INFO"
    assert stale_analysis["summary"] not in json.dumps(scenario.store.pending_outbox())
    # Fresh inference must be associated with a fresh input, rather than simply
    # relaxing the guard or silently assigning the previous answer a new revision.
    current = assert_ok(scenario.service.staff_comparison(
        scenario.ctx("matcher"), claim_id="C001",
    ))
    accepted = assert_ok(scenario.service.prepare_review(
        scenario.ctx("matcher"), claim_id="C001", item_ids=["I-001", "I-002"],
        analysis={"summary": "A fresh comparison based on the corrected source records."},
        expected_basis=_basis(current),
    ))
    assert accepted["claim_revision"] == current["claim"]["revision"]
    assert accepted["item_revisions"]["I-001"] == _basis(current)["item_revisions"]["I-001"]


def test_basis_guard_runs_after_acquiring_the_write_transaction(scenario):
    scenario.report()
    scenario.evidence()
    snapshot = assert_ok(scenario.service.staff_comparison(
        scenario.ctx("matcher"), claim_id="C001",
    ))
    worker_store = SQLiteStore(scenario.path)
    worker = DomainService(worker_store)
    attempting_write = threading.Event()
    connect = worker_store.connect

    def observed_connect():
        connection = connect()
        connection.set_trace_callback(
            lambda sql: attempting_write.set() if sql.strip().upper() == "BEGIN IMMEDIATE" else None
        )
        return connection

    worker_store.connect = observed_connect
    ctx = scenario.ctx("matcher")
    before = _counts(scenario.store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        # The second connection already owns the write lock. A preflight read
        # outside the review transaction could see the old committed revision.
        with scenario.store.transaction() as writer:
            writer.execute("UPDATE items SET revision=revision+1 WHERE id='I-001'")
            future = pool.submit(
                worker.prepare_review, ctx, claim_id="C001", item_ids=["I-001"],
                analysis={"summary": "Answer produced from the older model input."},
                expected_basis=_basis(snapshot),
            )
            assert attempting_write.wait(timeout=5), "The review never attempted its transaction"
            assert not future.done(), "The competing write lock should still hold the review"
        # Commit the changed item, then the queued transaction must detect it.
        refused = future.result(timeout=5)
    assert_refused(refused, "STALE_REVISION")
    assert _counts(scenario.store) == before
    with scenario.store.connection() as db:
        assert db.execute("SELECT revision FROM items WHERE id='I-001'").fetchone()[0] == 2
        assert db.execute("SELECT count(*) FROM reviews").fetchone()[0] == 0


def _enroll_staff(store, staff_ids):
    store.configure_enrollment(
        staff_room_id="staff-room", staff_actor_ids=staff_ids,
        matcher_agent_ids=[ACTORS["matcher"][0]],
        cases=[
            {"claim_id": "C001", "claimant_actor_id": ACTORS["priya"][0],
             "case_room_id": "priya-room", "intake_agent_ids": [ACTORS["intake1"][0]]},
            {"claim_id": "C002", "claimant_actor_id": ACTORS["alex"][0],
             "case_room_id": "alex-room", "intake_agent_ids": [ACTORS["intake2"][0]]},
        ],
    )


def test_revoked_actor_cannot_read_a_cached_successful_privileged_result(scenario):
    review = scenario.ready()
    original_ctx = scenario.ctx("staff")
    kwargs = dict(claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Compared the identifying evidence.")
    original = scenario.service.approve(original_ctx, **kwargs)
    data = assert_ok(original)
    assert data["items"][0]["private_facts"]  # A successful staff result contains restricted data.
    before = _counts(scenario.store)
    with scenario.store.connection() as db:
        stored_before = db.execute(
            "SELECT result_json FROM processed_events WHERE source_event_id=?",
            (original_ctx.source_event_id,),
        ).fetchone()[0]
    # Keep the old principal record but remove its current staff enrollment.
    _enroll_staff(scenario.store, ["replacement-custodian"])
    restarted = scenario.reopen()
    refused = restarted.service.approve(original_ctx, **kwargs)
    assert_refused(refused, "FORBIDDEN")
    assert refused["data"] == {}
    assert refused["replayed"] is False
    assert _counts(scenario.store) == before
    with scenario.store.connection() as db:
        stored_after = db.execute(
            "SELECT result_json FROM processed_events WHERE source_event_id=?",
            (original_ctx.source_event_id,),
        ).fetchone()[0]
        assert stored_after == stored_before  # Revocation must not rewrite the original decision.
        assert db.execute("SELECT status FROM items WHERE id='I-001'").fetchone()[0] == "RESERVED"
    _enroll_staff(scenario.store, [ACTORS["staff"][0]])
    restored = restarted.service.approve(original_ctx, **kwargs)
    assert restored["replayed"] is True
    assert restored["audit_id"] == original["audit_id"]
    assert restored["data"] == original["data"]
