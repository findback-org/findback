from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from findback.domain import ActorContext, DomainService
from findback.storage import SQLiteStore


ACTORS = {
    "staff": ("staff-mira", "human", "staff-room"),
    "priya": ("claimant-priya", "human", "priya-room"),
    "alex": ("claimant-alex", "human", "alex-room"),
    "intake1": ("intake-priya", "agent", "priya-room"),
    "intake2": ("intake-alex", "agent", "alex-room"),
    "matcher": ("staff-matcher", "agent", "staff-room"),
    "stranger": ("unrelated-human", "human", "unrelated-room"),
}

PRIVATE_FACTS = (
    "Green triangle sticker beneath the lid; short blue charging cable inside.",
    "Red thread attached to the zipper; initials RK on the inner label.",
)


def assert_ok(result):
    assert result["ok"], result
    assert isinstance(result["data"], dict)
    json.dumps(result)
    return result["data"]


def assert_refused(result, *codes):
    assert result["ok"] is False, result
    assert result["code"] in codes, result
    assert result["audit_id"], result
    json.dumps(result)
    return result


@dataclass
class Scenario:
    path: Path
    store: SQLiteStore
    service: DomainService
    counter: object = field(default_factory=lambda: itertools.count(1))

    def ctx(self, role, *, event_id=None, room=None, actor_id=None,
            actor_kind=None, text="A fixture message", origin="fixture"):
        enrolled_id, enrolled_kind, enrolled_room = ACTORS[role]
        return ActorContext(
            actor_id=actor_id or enrolled_id,
            actor_kind=actor_kind or enrolled_kind,
            room_id=room or enrolled_room,
            source_event_id=event_id or f"test-event-{next(self.counter)}",
            source_text=text,
            occurred_at="2026-11-07T10:00:00+05:30",
            origin=origin,
        )

    def reopen(self):
        store = SQLiteStore(self.path)
        return Scenario(self.path, store, DomainService(store), self.counter)

    def report(self, claim_id="C001", *, description="Black headphone case"):
        claimant, intake = ("priya", "intake1") if claim_id == "C001" else ("alex", "intake2")
        draft = assert_ok(self.service.save_report_draft(
            self.ctx(intake), claim_id=claim_id,
            report={
                "category": "headphone case", "color": "black",
                "brand_model": None, "lost_place": "Library",
                "lost_at": None, "description": description,
            }, source_event_ids=[],
        ))
        assert_ok(self.service.confirm_report(
            self.ctx(claimant), claim_id=claim_id, draft_id=draft["draft_id"],
        ))
        return draft

    def evidence(self, claim_id="C001", text="A green triangle and a blue cable", **kwargs):
        role = "priya" if claim_id == "C001" else "alex"
        return assert_ok(self.service.record_evidence(
            self.ctx(role, text=text), claim_id=claim_id, text=text, **kwargs,
        ))

    def review(self, claim_id="C001", item_ids=None, **kwargs):
        return assert_ok(self.service.prepare_review(
            self.ctx("matcher"), claim_id=claim_id,
            item_ids=item_ids or ["I-001", "I-002"], **kwargs,
        ))

    def ready(self, claim_id="C001"):
        self.report(claim_id)
        self.evidence(claim_id)
        return self.review(claim_id)

    def reserve(self, claim_id="C001", item_id="I-001", review=None, ctx=None):
        review = review or self.ready(claim_id)
        result = self.service.approve(
            ctx or self.ctx("staff"), claim_id=claim_id, item_id=item_id,
            review_id=review["review_id"], reason="Staff inspected the claimant's evidence.",
        )
        return assert_ok(result)

    def view(self, claim_id="C001", role="staff"):
        return assert_ok(self.service.case_view(self.ctx(role), claim_id=claim_id))


@pytest.fixture
def scenario(tmp_path):
    path = tmp_path / "findback.db"
    store = SQLiteStore(path)
    store.configure_enrollment(
        staff_room_id="staff-room", staff_actor_ids=["staff-mira"],
        matcher_agent_ids=["staff-matcher"], cases=[
            {"claim_id": "C001", "claimant_actor_id": "claimant-priya",
             "case_room_id": "priya-room", "intake_agent_ids": ["intake-priya"]},
            {"claim_id": "C002", "claimant_actor_id": "claimant-alex",
             "case_room_id": "alex-room", "intake_agent_ids": ["intake-alex"]},
        ],
    )
    s = Scenario(path, store, DomainService(store))
    for item_id, place, private_facts in (
        ("I-001", "Library Desk A", PRIVATE_FACTS[0]),
        ("I-002", "Library Study Room 2", PRIVATE_FACTS[1]),
    ):
        assert_ok(s.service.register_item(
            s.ctx("staff"), item_id=item_id,
            coarse={"category": "headphone case", "color": "black",
                    "brand_model": None, "found_place": place,
                    "found_at": "2026-11-07T14:00:00+05:30"},
            private_facts=private_facts,
        ))
    return s
