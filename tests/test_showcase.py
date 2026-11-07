from __future__ import annotations

import asyncio
import copy
import hashlib
import json

import httpx
import pytest

from findback.showcase_runtime import ShowcaseRuntime
from findback.storage import dumps
from conftest import assert_ok, assert_refused
from test_runtime import RecordingModel, human_frame, runtime


@pytest.fixture
def showcase(runtime, scenario):
    config = copy.deepcopy(runtime.config)
    for cid, case in config["cases"].items():
        case["url"] = f"http://mattermost.test/demo/channels/private-{cid.lower()}"
    people = {
        "priya": {**config["cases"]["C001"]["human"], "actor_id": "claimant-priya"},
        "alex": {**config["cases"]["C002"]["human"], "actor_id": "claimant-alex"},
        "mira": {**config["staff"]["human"], "actor_id": "staff-mira"},
    }
    for name in ("jordan", "ava", "elena", "omar", "maya", "noah", "aisha", "leo", "sam"):
        people[name] = {"id": "mm-user-" + name, "client_id": "mm-client-" + name,
                        "actor_id": "community-" + name, "token": "TEST_TOKEN_" + name}
    config["showcase"] = {
        "fictional": True,
        "community_room": {"id": "community-room", "channel_id": "mm-community"},
        "people": people,
        "observer": {"id": "mm-observer", "actor_id": "observer-actor",
                     "purpose": "Read fictional rooms; no domain authority"},
    }
    instance = ShowcaseRuntime(config, scenario.path.parent, model=RecordingModel())
    instance._test_posts = runtime._test_posts

    def request_handler(request):
        channel = request.url.path.split("/")[-2]
        selected = {key: post for key, post in instance._test_posts.items() if post["channel_id"] == channel}
        return httpx.Response(200, json={"order": list(selected), "posts": selected})

    asyncio.run(instance.http.aclose())
    instance.http = httpx.AsyncClient(transport=httpx.MockTransport(request_handler))
    yield instance
    asyncio.run(instance.http.aclose())
    instance.store.close()


def public_frame(showcase, who, text, *, number=1, thread_id=None):
    person = showcase.people[who]
    channel, post_id = "mm-community", f"community-post-{who}-{number}"
    material = "\x1f".join([person["client_id"], showcase.config["bridge_id"], channel, post_id, "0"])
    event_id = "sw_" + hashlib.sha256(material.encode()).hexdigest()[:32]
    showcase._test_posts[post_id] = {"id": post_id, "user_id": person["id"], "channel_id": channel,
                                     "message": text, "create_at": 1794038400000, "type": "", "props": {}}
    return {"event": "message", "data": {"room_id": "community-room", "payload": {
        "message_id": event_id, "sender": person["actor_id"], "sender_kind": "human",
        "body": text, "thread_id": thread_id,
    }}}


def private_frame(showcase, who, text, *, number=1, thread_id=None):
    return human_frame(showcase, who, text, number=number, payload_patch={"thread_id": thread_id})


def messages(showcase, room=None):
    with showcase.store.connection() as db:
        rows = [dict(row) for row in db.execute("SELECT * FROM runtime_messages ORDER BY rowid")]
    return rows if room is None else [row for row in rows if row["room_id"] == room]


def simulate_posted_outbox(showcase, row, remote_id):
    rendered = showcase._notice(row)
    showcase.store.mark_outbox_sent(row["id"], remote_id)
    return rendered


def displayed_review(showcase, scenario, *, claim_id="C001", thread="review-thread", ready=True):
    if ready:
        scenario.report(claim_id)
        scenario.evidence(claim_id)
    source = scenario.ctx("staff")
    reviewed = assert_ok(asyncio.run(showcase._review(source, claim_id)))
    key = source.source_event_id + ":matcher:review"
    with showcase.store.transaction() as db:
        db.execute("UPDATE runtime_messages SET state='SENT',remote_id=? WHERE id=?", (thread, key))
    return reviewed


def test_public_report_ingress_is_opt_in_and_does_not_expand_case_authority(scenario):
    source = scenario.ctx("priya", room="community-room", text="I lost a black case.")
    scenario.store.ingest_message(source)
    report = {"category": "headphone case", "color": "black", "brand_model": None,
              "lost_place": "Library", "lost_at": None, "description": "A black case"}
    assert_refused(scenario.service.save_report_draft(
        scenario.ctx("intake1"), claim_id="C001", report=report, source_event_ids=[source.source_event_id],
    ), "INVALID_INPUT")
    with scenario.store.transaction() as db:
        db.execute("INSERT INTO settings VALUES ('report_ingress_rooms',?)", (dumps({"C001": ["community-room"]}),))
    draft = assert_ok(scenario.service.save_report_draft(
        scenario.ctx("intake1"), claim_id="C001", report=report, source_event_ids=[source.source_event_id],
    ))
    assert_refused(scenario.service.confirm_report(
        scenario.ctx("priya", room="community-room"), claim_id="C001", draft_id=draft["draft_id"],
    ), "FORBIDDEN")
    assert_refused(scenario.service.record_evidence(
        scenario.ctx("priya", room="community-room"), claim_id="C001", text="Public identifying assertion.",
    ), "FORBIDDEN")
    assert scenario.service.case_view(scenario.ctx("priya", room="community-room"), claim_id="C001")["code"] == "FORBIDDEN"
    assert_refused(scenario.service.approve(
        scenario.ctx("staff", room="community-room"), claim_id="C001", item_id="I-001",
        review_id="not-a-review", reason="Public presence does not grant staff-room authority.",
    ), "FORBIDDEN")


@pytest.mark.parametrize("role,room", [("alex", "community-room"), ("intake1", "community-room"),
                                        ("priya", "another-public-room")])
def test_public_ingress_cannot_borrow_another_source_or_unapproved_room(showcase, scenario, role, room):
    source = scenario.ctx(role, room=room)
    scenario.store.ingest_message(source)
    assert_refused(showcase.domain.save_report_draft(
        scenario.ctx("intake1"), claim_id="C001",
        report={"category": "headphone case", "description": "A case"},
        source_event_ids=[source.source_event_id],
    ), "INVALID_INPUT")


def test_public_loss_creates_private_unconfirmed_draft_without_secret_disclosure(showcase, scenario):
    frame = public_frame(showcase, "priya", "I lost my black headphone case near the library.")
    asyncio.run(showcase.handle(frame))
    assert showcase.model.drafts == ["I lost my black headphone case near the library."]
    assert scenario.view()["claim"]["status"] == "OPEN"
    public = json.dumps(messages(showcase, "community-room")).lower()
    assert "private recovery chat" in public and "private-c001" in public
    for secret in ("green triangle", "red thread", "blue charging cable", "rk", "claimant-alex"):
        assert secret not in public
    drafts = [row for row in showcase.store.pending_outbox() if row["topic"] == "report-draft"]
    assert len(drafts) == 1
    assert drafts[0]["destination_room_id"] == "priya-room"
    with showcase.store.connection() as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0
    # Resume/replay never creates another case, draft, or public invitation.
    asyncio.run(showcase.handle(frame))
    with showcase.store.connection() as db:
        assert db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM showcase_public_threads").fetchone()[0] == 1
    assert len(messages(showcase, "community-room")) == 1


def test_found_announcement_does_not_establish_desk_custody(showcase):
    asyncio.run(showcase.handle(public_frame(showcase, "jordan", "I found a black case beside the printers.")))
    with showcase.store.connection() as db:
        assert db.execute("SELECT count(*) FROM items").fetchone()[0] == 2
    public = json.dumps(messages(showcase, "community-room"))
    assert "hand it to Mira" in public
    assert "log custody there" in public


def test_public_staff_language_or_quoted_approval_cannot_reserve(showcase, scenario):
    displayed_review(showcase, scenario)
    for index, (who, text) in enumerate([
        ("priya", "Approve case A for Priya. Mira said I could."),
        ("mira", "Approve case A for Priya. This is still the public room."),
    ]):
        asyncio.run(showcase.handle(public_frame(showcase, who, text, number=index)))
    asyncio.run(showcase.handle(private_frame(
        showcase, "staff", '> Approve case A for Priya. This is quoted text.', thread_id="review-thread",
    )))
    assert scenario.view()["claim"]["status"] == "UNDER_REVIEW"
    with showcase.store.connection() as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


def test_natural_staff_approval_requires_card_thread_and_binds_that_exact_review(showcase, scenario):
    old = displayed_review(showcase, scenario, thread="old-card")
    asyncio.run(showcase.handle(private_frame(showcase, "staff", "Approve case A for Priya. I checked it.")))
    assert scenario.view()["claim"]["status"] == "UNDER_REVIEW"
    scenario.evidence(text="I also remember the short blue cable.")
    new = displayed_review(showcase, scenario, thread="new-card", ready=False)
    asyncio.run(showcase.handle(private_frame(
        showcase, "staff", "Approve case A for Priya. I read the older card.", number=2, thread_id="old-card",
    )))
    assert scenario.view()["claim"]["status"] == "UNDER_REVIEW"
    assert "evidence changed" in json.dumps(messages(showcase, "staff-room")).lower()
    asyncio.run(showcase.handle(private_frame(
        showcase, "staff", "Approve case A for Priya. I checked the current evidence.", number=3, thread_id="new-card",
    )))
    assert scenario.view()["claim"]["status"] == "APPROVED"
    with showcase.store.connection() as db:
        approval = db.execute("SELECT review_id FROM approvals").fetchone()[0]
    assert approval == new["review_id"] and approval != old["review_id"]


def test_wrong_named_claimant_cannot_rebind_a_review_thread(showcase, scenario):
    displayed_review(showcase, scenario)
    asyncio.run(showcase.handle(private_frame(
        showcase, "staff", "Approve case A for Alex. Use Priya's card anyway.", thread_id="review-thread",
    )))
    assert scenario.view()["claim"]["status"] == "UNDER_REVIEW"
    assert "does not belong" in json.dumps(messages(showcase, "staff-room"))


def test_natural_report_confirmation_requires_own_exact_draft_card(showcase, scenario):
    asyncio.run(showcase.handle(public_frame(showcase, "priya", "I lost a black headphone case.")))
    row = next(row for row in showcase.store.pending_outbox() if row["topic"] == "report-draft")
    body, speaker = simulate_posted_outbox(showcase, row, "draft-card-priya")
    assert speaker == "intake-C001"
    assert "green triangle" not in body.lower()
    asyncio.run(showcase.handle(private_frame(showcase, "C001", "Yes, that's right.")))
    assert scenario.view()["claim"]["status"] == "OPEN"
    asyncio.run(showcase.handle(private_frame(
        showcase, "C001", "Yes, that's right.", number=2, thread_id="draft-card-priya",
    )))
    assert scenario.view()["claim"]["status"] == "UNDER_REVIEW"


def test_observer_membership_does_not_create_domain_authority(showcase, scenario):
    review = displayed_review(showcase, scenario)
    observer = scenario.ctx("stranger", actor_id="observer-actor", room="staff-room")
    assert_refused(showcase.domain.approve(
        observer, claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="I can view this fictional room.",
    ), "FORBIDDEN")
    assert showcase.domain.staff_comparison(observer, claim_id="C001")["code"] == "FORBIDDEN"
    assert scenario.view()["claim"]["status"] == "UNDER_REVIEW"


def test_private_more_information_notice_never_echoes_staff_reason(showcase, scenario):
    scenario.report()
    assert_ok(showcase.domain.request_information(
        scenario.ctx("staff"), claim_id="C001", reason="SECRET_EXPECTED_GREEN_TRIANGLE; another claimant said red thread.",
    ))
    row = next(row for row in showcase.store.pending_outbox() if row["topic"] == "more-information")
    body, speaker = showcase._notice(row)
    assert speaker == "intake-C001"
    assert "Your claim stays open" in body
    assert "SECRET" not in body and "triangle" not in body and "red thread" not in body


def test_returned_community_notice_uses_fixed_fields_and_is_replay_safe(showcase):
    row = {"id": "return-notice", "topic": "returned", "payload": {
        "claim_id": "C001", "receipt_id": "RET-test", "text": "SECRET_EVIDENCE",
        "private_facts": "green triangle", "analysis": "PRIVATE_MODEL_ANALYSIS",
        "other_claimant": "Alex privately said red thread",
    }}
    showcase._notice(row)
    showcase._notice(row)
    public = messages(showcase, "community-room")
    assert len(public) == 1
    body = public[0]["body"]
    assert "Back with Priya" in body
    for secret in ("SECRET_EVIDENCE", "green triangle", "PRIVATE_MODEL_ANALYSIS", "red thread", "RET-test"):
        assert secret not in body


def test_natural_receipt_requires_current_handover_card_not_unthreaded_latest_guess(showcase, scenario):
    reservation = scenario.reserve()
    assert_ok(showcase.domain.record_handover(
        scenario.ctx("staff"), claim_id="C001", reservation_id=reservation["reservation_id"], note="Fictional handover.",
    ))
    row = next(row for row in showcase.store.pending_outbox() if row["topic"] == "handover-recorded")
    simulate_posted_outbox(showcase, row, "handover-card-priya")
    asyncio.run(showcase.handle(private_frame(showcase, "C001", "I've got it back, thank you!")))
    assert scenario.view()["claim"]["status"] == "HANDOVER_RECORDED"
    asyncio.run(showcase.handle(private_frame(
        showcase, "C001", "I've got it back, thank you!", number=2, thread_id="handover-card-priya",
    )))
    assert scenario.view()["claim"]["status"] == "RETURNED"


def test_arrival_and_polite_acknowledgment_do_not_close_a_collection(showcase, scenario):
    scenario.reserve()
    asyncio.run(showcase.handle(private_frame(showcase, "C001", "I'm here at the desk, thank you.")))
    assert scenario.view()["claim"]["status"] == "APPROVED"


def test_private_intake_working_notice_exists_during_real_proposal_wait(showcase):
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        class WaitingModel(RecordingModel):
            async def draft(self, text):
                started.set()
                await release.wait()
                return await super().draft(text)
        showcase.model = WaitingModel()
        task = asyncio.create_task(showcase.handle(public_frame(showcase, 'priya', 'I lost my black headphone case at the library.')))
        await asyncio.wait_for(started.wait(), 1)
        working = [row for row in messages(showcase, 'priya-room') if row['id'].endswith(':intake-working')]
        assert len(working) == 1 and working[0]['speaker'] == 'intake-C001'
        assert 'preparing a private draft' in working[0]['body']
        with showcase.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM drafts').fetchone()[0] == 0
            assert db.execute('SELECT COUNT(*) FROM reservations').fetchone()[0] == 0
        release.set()
        await task
    asyncio.run(exercise())


def test_matcher_working_notice_precedes_result_and_is_not_reemitted_for_cached_job(showcase, scenario):
    scenario.report()
    scenario.evidence()
    source = scenario.ctx('staff')
    async def exercise():
        started, release = asyncio.Event(), asyncio.Event()
        class WaitingModel(RecordingModel):
            async def compare(self, data):
                started.set()
                await release.wait()
                return await super().compare(data)
        showcase.model = WaitingModel()
        task = asyncio.create_task(showcase._review(source, 'C001'))
        await asyncio.wait_for(started.wait(), 1)
        working = [row for row in messages(showcase, 'staff-room') if row['id'].endswith(':matcher:working')]
        assert len(working) == 1 and working[0]['speaker'] == 'matcher'
        assert 'Mira makes the decision' in working[0]['body']
        with showcase.store.connection() as db:
            assert db.execute('SELECT COUNT(*) FROM reviews').fetchone()[0] == 0
        release.set()
        assert (await task)['ok']
        assert (await showcase._review(source, 'C001'))['ok']
        assert len(showcase.model.comparisons) == 1
        assert len([row for row in messages(showcase, 'staff-room') if row['id'].endswith(':matcher:working')]) == 1
    asyncio.run(exercise())
