"""Failure isolation checks against the real durable inbox and worker loop."""
from __future__ import annotations

import asyncio

from findback.model import ModelError
from findback.storage import SQLiteStore
from test_runtime import human_frame, runtime


def enqueue(runtime, room, event):
    frame = {"event": "message", "data": {"room_id": room, "payload": {
        "message_id": event, "body": "Fictional test input", "sender": "test-sender"}}}
    runtime.journal.capture("test-service", frame, primary=True)


async def cancel_worker(worker):
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


def test_slow_inference_does_not_block_another_room_but_own_room_stays_ordered(runtime, monkeypatch):
    async def check():
        release, other_done, later_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        observed = []
        enqueue(runtime, "slow-room", "slow-first")
        enqueue(runtime, "slow-room", "slow-second")
        enqueue(runtime, "other-room", "other-first")

        async def handle(frame):
            event = frame["data"]["payload"]["message_id"]
            observed.append(event)
            if event == "slow-first":
                await release.wait()
            elif event == "other-first":
                other_done.set()
            else:
                later_done.set()

        monkeypatch.setattr(runtime, "handle", handle)
        worker = asyncio.create_task(runtime._work())
        try:
            await asyncio.wait_for(other_done.wait(), 1)
            assert observed == ["slow-first", "other-first"]
            release.set()
            await asyncio.wait_for(later_done.wait(), 1)
            assert observed == ["slow-first", "other-first", "slow-second"]
        finally:
            await cancel_worker(worker)

    asyncio.run(check())


def test_large_backlog_cannot_hide_another_room_head(runtime):
    for number in range(205):
        enqueue(runtime, "busy-room", f"busy-{number:03}")
    enqueue(runtime, "quiet-room", "quiet-first")
    heads = runtime.journal.pending_room_heads()
    assert [f["data"]["payload"]["message_id"] for f in heads] == ["busy-000", "quiet-first"]


def test_many_distinct_room_heads_cannot_bury_the_librarian_beyond_the_batch(runtime):
    for number in range(205):
        enqueue(runtime, f"room-{number}", f"event-{number}")
    enqueue(runtime, runtime.staff_room, "librarian")
    heads = runtime.journal.pending_room_heads(runtime.staff_room)
    assert len(heads) == 100
    assert heads[0]["data"]["payload"]["message_id"] == "librarian"


def test_authenticated_status_reply_arrives_while_another_claimants_model_is_blocked(runtime, monkeypatch):
    """Keep the real handler, identity lookup, domain query and durable reply."""
    async def check():
        inference_started, release = asyncio.Event(), asyncio.Event()
        status_delivered, draft_prepared = asyncio.Event(), asyncio.Event()
        report = human_frame(runtime, "C001", "findback report A black headphone case lost in the library.")
        status = human_frame(runtime, "C002", "findback status")
        report_id = report["data"]["payload"]["message_id"]
        status_id = status["data"]["payload"]["message_id"]
        original_draft, original_queue = runtime.model.draft, runtime.journal.queue

        async def blocked_draft(text):
            inference_started.set()
            await release.wait()
            return await original_draft(text)

        def observed_queue(key, *args, **kwargs):
            original_queue(key, *args, **kwargs)
            if key == status_id + ":result":
                status_delivered.set()
            elif key == report_id + ":draft-display":
                draft_prepared.set()

        monkeypatch.setattr(runtime.model, "draft", blocked_draft)
        monkeypatch.setattr(runtime.journal, "queue", observed_queue)
        runtime.journal.capture("test-service", report, primary=True)
        runtime.journal.capture("test-service", status, primary=True)
        worker = asyncio.create_task(runtime._work())
        try:
            await asyncio.wait_for(inference_started.wait(), 1)
            await asyncio.wait_for(status_delivered.wait(), 1)
            assert not draft_prepared.is_set()
            with runtime.store.connection() as db:
                reply = db.execute("SELECT room_id,body FROM runtime_messages WHERE id=?", (status_id + ":result",)).fetchone()
                assert reply["room_id"] == "alex-room" and "C002" in reply["body"]
                assert db.execute("SELECT state FROM inbox WHERE event_id=?", (report_id,)).fetchone()[0] == "PENDING"
            release.set()
            await asyncio.wait_for(draft_prepared.wait(), 1)
        finally:
            await cancel_worker(worker)

    asyncio.run(check())


def test_active_work_is_bounded_and_a_librarian_slot_survives_claimant_saturation(runtime, monkeypatch):
    async def check():
        release, four_started, all_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        started, finished = [], []
        active, peak = 0, 0
        for number in range(7):
            room = runtime.staff_room if number == 6 else f"room-{number}"
            enqueue(runtime, room, f"event-{number}")

        async def handle(frame):
            nonlocal active, peak
            event = frame["data"]["payload"]["message_id"]
            started.append(event)
            active += 1
            peak = max(peak, active)
            if len(started) == 4:
                four_started.set()
            try:
                await release.wait()
            finally:
                active -= 1
            finished.append(event)
            if len(finished) == 7:
                all_done.set()

        monkeypatch.setattr(runtime, "handle", handle)
        worker = asyncio.create_task(runtime._work())
        try:
            await asyncio.wait_for(four_started.wait(), 1)
            # Three blocked claimant rooms cannot keep the librarian behind
            # the remaining queued claimant rooms.
            assert started == ["event-6", "event-0", "event-1", "event-2"]
            assert peak == 4
            release.set()
            await asyncio.wait_for(all_done.wait(), 1)
            assert peak == 4
            assert len(set(finished)) == 7
        finally:
            await cancel_worker(worker)

    asyncio.run(check())


def test_failed_model_does_not_poison_other_rooms_or_the_next_message(runtime, monkeypatch):
    async def check():
        completed, observed = asyncio.Event(), []
        enqueue(runtime, "one", "model-fails")
        enqueue(runtime, "one", "next-message")
        enqueue(runtime, "two", "other-message")

        async def handle(frame):
            event = frame["data"]["payload"]["message_id"]
            observed.append(event)
            if event == "model-fails":
                raise ModelError("PRIVATE_PROVIDER_ERROR")
            if event == "next-message":
                completed.set()

        monkeypatch.setattr(runtime, "handle", handle)
        worker = asyncio.create_task(runtime._work())
        try:
            await asyncio.wait_for(completed.wait(), 1)
            assert observed == ["model-fails", "other-message", "next-message"]
            with runtime.store.connection() as db:
                failed = db.execute("SELECT state,error FROM inbox WHERE event_id='model-fails'").fetchone()
                notice = db.execute("SELECT body FROM runtime_messages WHERE id='model-fails:model-error'").fetchone()[0]
            assert tuple(failed) == ("FAILED", "MODEL_UNAVAILABLE")
            assert "PRIVATE_PROVIDER_ERROR" not in notice
        finally:
            await cancel_worker(worker)

    asyncio.run(check())


def test_shutdown_cancels_active_work_without_acknowledging_completion_and_restart_replays(runtime, monkeypatch):
    async def check():
        started, replayed = asyncio.Event(), asyncio.Event()
        enqueue(runtime, "one", "interrupted-inference")

        async def blocked(_frame):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(runtime, "handle", blocked)
        worker = asyncio.create_task(runtime._work())
        try:
            await asyncio.wait_for(started.wait(), 1)
        finally:
            await cancel_worker(worker)
        # Reopen the actual SQLite file rather than inspecting an in-memory flag.
        reopened = SQLiteStore(runtime.state_dir / "findback.db")
        try:
            with reopened.connection() as db:
                assert db.execute("SELECT state FROM inbox WHERE event_id='interrupted-inference'").fetchone()[0] == "PENDING"
        finally:
            reopened.close()

        async def finish(_frame):
            replayed.set()

        monkeypatch.setattr(runtime, "handle", finish)
        worker = asyncio.create_task(runtime._work())
        try:
            await asyncio.wait_for(replayed.wait(), 1)
            with runtime.store.connection() as db:
                assert db.execute("SELECT state FROM inbox WHERE event_id='interrupted-inference'").fetchone()[0] == "DONE"
        finally:
            await cancel_worker(worker)

    asyncio.run(check())
