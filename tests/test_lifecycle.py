from __future__ import annotations

import asyncio
import fcntl

import pytest

from findback.runtime import render_read
from findback.switch_client import SwitchClient, SwitchProtocolError
from test_runtime import runtime  # Reuse the fake-HTTP runtime fixture; no live credentials.


@pytest.mark.parametrize("cancelled", [False, True])
def test_partial_runtime_entry_unwinds_clients_lock_and_http(runtime, monkeypatch, cancelled):
    created = []

    class StartupClient:
        def __init__(self, _url, agent_id, *_args):
            self.agent_id, self.closed = agent_id, False
            created.append(self)

        async def __aenter__(self):
            if self.agent_id.endswith("staff-matcher"):
                # A failed child context must clean up its own incomplete entry.
                self.closed = True
                if cancelled:
                    raise asyncio.CancelledError()
                raise SwitchProtocolError("Simulated second-client startup failure")
            return self

        async def __aexit__(self, *_):
            self.closed = True

    monkeypatch.setattr("findback.runtime.SwitchClient", StartupClient)

    async def attempt():
        failure = None
        try:
            try:
                await runtime.__aenter__()
            except BaseException as exc:
                failure = exc
            observed = (all(client.closed for client in created),
                        runtime._process_lock.closed, runtime.http.is_closed)
        finally:
            if hasattr(runtime, "stack"):
                await runtime.stack.aclose()
            if hasattr(runtime, "_process_lock"):
                runtime._process_lock.close()
            await runtime.http.aclose()
        return failure, observed

    failure, observed = asyncio.run(attempt())
    assert isinstance(failure, asyncio.CancelledError if cancelled else SwitchProtocolError)
    assert len(created) == 2
    assert observed == (True, True, True)


def test_runtime_lock_rejection_also_closes_unused_http_client(runtime):
    held = (runtime.state_dir / "runner.lock").open("a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    async def attempt():
        try:
            with pytest.raises(RuntimeError, match="already owns"):
                await runtime.__aenter__()
            return runtime.http.is_closed
        finally:
            held.close()
            await runtime.http.aclose()

    assert asyncio.run(attempt())


def test_secondary_child_cleanup_failure_cannot_skip_runtime_resource_cleanup(runtime, monkeypatch):
    class FaultyClient:
        def __init__(self, _url, agent_id, *_args):
            self.agent_id = agent_id

        async def __aenter__(self):
            if self.agent_id.endswith("staff-matcher"):
                raise SwitchProtocolError("Initial startup refusal")
            return self

        async def __aexit__(self, *_):
            raise RuntimeError("Secondary cleanup failure")

    monkeypatch.setattr("findback.runtime.SwitchClient", FaultyClient)

    async def attempt():
        failure = None
        try:
            try:
                await runtime.__aenter__()
            except BaseException as exc:
                failure = exc
            observed = (runtime._process_lock.closed, runtime.http.is_closed)
        finally:
            if hasattr(runtime, "stack"):
                await runtime.stack.aclose()
            runtime._process_lock.close()
            await runtime.http.aclose()
        return failure, observed

    failure, observed = asyncio.run(attempt())
    assert isinstance(failure, SwitchProtocolError), "Preserve the initial startup failure for diagnosis."
    assert observed == (True, True)


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_switch_client_entry_joins_reader_and_closes_owned_http(monkeypatch, cancelled):
    client = SwitchClient("http://switch.test", "agent-test", "test-key", ["staff-room"])

    async def fail_after_reader_started():
        client._reader_task = asyncio.create_task(asyncio.Event().wait())
        if cancelled:
            raise asyncio.CancelledError()
        raise SwitchProtocolError("Simulated startup refusal")

    monkeypatch.setattr(client, "start", fail_after_reader_started)

    async def attempt():
        failure = None
        try:
            try:
                await client.__aenter__()
            except BaseException as exc:
                failure = exc
            observed = (client._reader_task.done(), client._http.is_closed)
        finally:
            await client.close()
        return failure, observed

    failure, observed = asyncio.run(attempt())
    assert isinstance(failure, asyncio.CancelledError if cancelled else SwitchProtocolError)
    assert observed == (True, True)


def test_oversize_unicode_notice_is_explicit_excerpt_and_full_record_is_retained(runtime):
    class CaptureClient:
        def __init__(self):
            self.posts = []

        async def read_context(self, *_args, **_kwargs):
            return {"messages": [], "truncated": False}

        async def post_message(self, room, body):
            self.posts.append((room, body))
            assert len(body.encode("utf-8")) < 12000
            return {"event_id": "posted-excerpt"}

    body = "Long staff-only record: " + "記録" * 10000
    runtime.journal.queue("large-record", "staff-room", body)
    client = CaptureClient()
    runtime.clients = {"service": client}
    assert asyncio.run(runtime._send_once("large-record", "staff-room", body, "service")) == "posted-excerpt"
    assert len(client.posts) == 1
    room, posted = client.posts[0]
    assert room == "staff-room"
    assert "Excerpt" in posted and "full record retained" in posted
    assert posted.endswith("[findback:large-record]")
    assert "\ufffd" not in posted  # UTF-8 truncation must not corrupt a partial character.
    with runtime.store.connection() as db:
        retained = db.execute("SELECT body FROM runtime_messages WHERE id='large-record'").fetchone()[0]
    assert retained == body


def test_chat_status_is_a_bounded_summary_of_latest_records():
    data = {
        "claim": {"id": "C001", "status": "UNDER_REVIEW", "revision": 21, "evidence_revision": 20},
        "evidence": [{"id": f"E-{i:03d}", "text": "detail " * 200,
                      "source_event_id": f"source-{i}"} for i in range(20)],
        "items": [{"id": f"I-{i:03d}", "revision": 1, "status": "AVAILABLE",
                   "private_facts": ["Staff-supplied mark"]} for i in range(20)],
        "reviews": [{"review_id": f"R-{i:03d}", "claim_revision": i,
                     "evidence_revision": i, "invalidated_at": None} for i in range(20)],
    }
    body = render_read("status", data)
    assert "C001" in body and "UNDER_REVIEW" in body
    assert "E-019" in body and "E-000" not in body
    assert "I-007" in body and "I-008" not in body
    assert "R-019" in body and "R-000" not in body
    assert "12 additional inventory records" in body
    assert len(body.encode("utf-8")) < 12000


def test_chat_audit_summary_does_not_expand_private_details_or_unbounded_history():
    data = {"events": [{"id": f"A-{i:03d}", "action": "record_evidence",
                        "result_code": "EVIDENCE_RECORDED", "occurred_at": "2026-11-07T10:00:00Z",
                        "origin": "fixture", "details": {"private_reason": "DO_NOT_RENDER_RAW_DETAILS"}}
                       for i in range(100)]}
    body = render_read("audit", data)
    assert "100 events" in body and "latest 15" in body
    assert "A-099" in body and "A-000" not in body
    assert "DO_NOT_RENDER_RAW_DETAILS" not in body
    assert len(body.encode("utf-8")) < 12000
