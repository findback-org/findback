"""Durability, reconciliation and payload-free operator recovery under outages."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys

import pytest

from findback import cli, delivery
from findback.runtime import RuntimeJournal
from findback.storage import SQLiteStore
from findback.switch_client import SwitchHTTPError, SwitchUnknownOutcomeError
from test_runtime import runtime
from test_showcase import showcase


def enqueue(store, key="notice", *, source="source", topic="notice", body="Private claimant statement"):
    with store.transaction() as db:
        db.execute("INSERT INTO outbox(id,source_event_id,topic,destination_room_id,visibility,payload_json,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", (key, source, topic, "priya-room", "claimant", json.dumps({"text": body}), "2026-11-08T00:00:00Z"))


def row(store, table, key):
    with store.connection() as db:
        return dict(db.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone())


def test_backoff_survives_restart_and_excludes_not_due_rows(tmp_path):
    clock = [10_000]
    path = tmp_path / "queue.db"
    store = SQLiteStore(path, delivery_clock=lambda: clock[0])
    enqueue(store)
    store.mark_outbox_failed("notice", OSError("SECRET token and claimant detail"))
    store.close()
    resumed = SQLiteStore(path, delivery_clock=lambda: clock[0])
    assert resumed.pending_outbox(due_only=True) == []
    saved = row(resumed, "outbox", "notice")
    assert (saved["attempts"], saved["consecutive_failures"], saved["next_attempt_at_ms"]) == (1, 1, 12_000)
    assert saved["last_error"] == "NETWORK_UNAVAILABLE"
    clock[0] = 12_000
    assert [r["id"] for r in resumed.pending_outbox(due_only=True)] == ["notice"]
    resumed.mark_outbox_failed("notice", OSError("offline"))
    assert row(resumed, "outbox", "notice")["next_attempt_at_ms"] == 16_000
    resumed.close()


def test_retry_limit_is_persistent_and_explicit_reset_preserves_history(scenario):
    store = scenario.store
    clock = [20_000]
    store._delivery_clock = lambda: clock[0]
    enqueue(store)
    for failure in range(delivery.MAX_FAILURES):
        store.mark_outbox_failed("notice", SwitchUnknownOutcomeError("untrusted server prose"))
        saved = row(store, "outbox", "notice")
        if failure < delivery.MAX_FAILURES - 1:
            assert saved["next_attempt_at_ms"] - clock[0] == 2_000 * 2 ** failure
            clock[0] = saved["next_attempt_at_ms"]
    assert (saved["delivery_state"], saved["blocked_reason"], saved["attempts"]) == ("BLOCKED", "RETRY_LIMIT", 8)
    assert store.pending_outbox(due_only=True) == []
    # Repeated polling/failure reports cannot silently reset the exhausted budget.
    store.mark_outbox_failed("notice", OSError("again"))
    assert row(store, "outbox", "notice")["attempts"] == 8
    with store.transaction() as db:
        assert delivery.retry_delivery(db, "outbox", "notice") == {"outbox": 1}
    reset = row(store, "outbox", "notice")
    assert (reset["attempts"], reset["manual_retries"], reset["consecutive_failures"]) == (8, 1, 0)
    assert reset["id"] == saved["id"] and reset["payload_json"] == saved["payload_json"]
    store.mark_outbox_sent("notice", "actual-remote-id")
    store.mark_outbox_sent("notice", "must-not-replace-id")
    with store.transaction() as db:
        assert delivery.retry_delivery(db, "outbox") == {"outbox": 0}
    final = row(store, "outbox", "notice")
    assert (final["attempts"], final["remote_event_id"], final["delivery_state"]) == (9, "actual-remote-id", "SENT")


@pytest.mark.parametrize("status,code", [(401, "AUTHORIZATION_REQUIRED"), (403, "AUTHORIZATION_REQUIRED"),
    (404, "DESTINATION_UNAVAILABLE"), (422, "REQUEST_REJECTED")])
def test_definitive_http_refusals_block_without_hammering_other_notices(scenario, status, code):
    store = scenario.store
    enqueue(store, "refused")
    store.mark_outbox_failed("refused", SwitchHTTPError(status))
    enqueue(store, "fresh")
    assert [r["id"] for r in store.pending_outbox(due_only=True)] == ["fresh"]
    assert row(store, "outbox", "refused")["blocked_reason"] == code


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limit_and_transient_http_read_failure_remain_scheduled(scenario, status):
    enqueue(scenario.store)
    scenario.store.mark_outbox_failed("notice", SwitchHTTPError(status))
    saved = row(scenario.store, "outbox", "notice")
    assert saved["delivery_state"] == "FAILED" and saved["blocked_reason"] is None
    assert saved["next_attempt_at_ms"] > saved["last_attempt_at_ms"]


class UncertainProvider:
    """A POST reaches the remote room before the local process learns its result."""
    def __init__(self, sender, *, cancel=False):
        self.sender, self.cancel = sender, cancel
        self.messages, self.posts, self.reads = [], [], []

    async def read_context(self, room, **kwargs):
        self.reads.append(room)
        return {"messages": list(self.messages), "truncated": False}

    async def post_message(self, room, body, thread_id=None):
        self.posts.append((room, body, thread_id))
        self.messages.append({"sender": self.sender, "body": body, "id": "remote-committed"})
        if self.cancel:
            raise asyncio.CancelledError()
        raise SwitchUnknownOutcomeError("response lost after commit")


@pytest.mark.parametrize("queue", ["outbox", "runtime_messages"])
@pytest.mark.parametrize("cancel", [False, True])
def test_post_unknown_outcome_or_cancellation_reconciles_after_restart_without_duplicate(runtime, queue, cancel):
    clock = [1_000]
    runtime.store._delivery_clock = lambda: clock[0]
    if queue == "outbox":
        enqueue(runtime.store, "delivery-key")
        speaker, sender = "service", "service-agent"
    else:
        runtime.journal.queue("delivery-key", "priya-room", "Safe claimant message", "intake-C001", "actual-thread")
        speaker, sender = "intake-C001", "intake-priya"
    provider = UncertainProvider(sender, cancel=cancel)
    runtime.clients = {"service": provider, speaker: provider}
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(runtime._delivery_pass())
    else:
        asyncio.run(runtime._delivery_pass())
    assert len(provider.posts) == 1
    state = row(runtime.store, queue, "delivery-key")
    assert state["delivery_state" if queue == "outbox" else "state"] == ("PENDING" if cancel else "FAILED")
    assert "delivery-key" in provider.posts[0][1]
    # New journal/store simulate restart; the remote provider retains its actual post.
    runtime.store.close()
    runtime.store = SQLiteStore(runtime.state_dir / "findback.db", delivery_clock=lambda: clock[0])
    runtime.journal = RuntimeJournal(runtime.store)
    if not cancel:
        asyncio.run(runtime._delivery_pass())
        assert len(provider.reads) == 1  # Persisted due time suppresses premature retry.
    clock[0] = 3_000
    asyncio.run(runtime._delivery_pass())
    final = row(runtime.store, queue, "delivery-key")
    assert final["delivery_state" if queue == "outbox" else "state"] == "SENT"
    assert final["remote_event_id" if queue == "outbox" else "remote_id"] == "remote-committed"
    assert len(provider.posts) == 1


def test_showcase_uses_shared_retries_and_blocks_notice_when_review_display_is_blocked(showcase):
    clock = [0]
    showcase.store._delivery_clock = lambda: clock[0]
    enqueue(showcase.store, "review-notice", source="review-source", topic="review-ready")
    showcase.journal.queue("review-source:review", "staff-room", "Private exact comparison", "matcher")
    attempts = []

    async def refused(*args, **kwargs):
        attempts.append(args[0])
        raise SwitchHTTPError(403)

    showcase._send_once = refused
    # Exercise the actual override, not a parallel test-only delivery implementation.
    async def once(**kwargs):
        kwargs.pop("poll_seconds")
        await showcase._delivery_pass(**kwargs)
    showcase._delivery_loop = once
    asyncio.run(showcase._deliver())
    assert attempts == ["review-source:review"]
    assert row(showcase.store, "outbox", "review-notice")["attempts"] == 0  # Waiting is not a send failure.
    clock[0] = 1_000
    asyncio.run(showcase._deliver())
    assert attempts == ["review-source:review"]
    assert row(showcase.store, "outbox", "review-notice")["blocked_reason"] == "DEPENDENCY_BLOCKED"
    assert row(showcase.store, "runtime_messages", "review-source:review")["blocked_reason"] == "AUTHORIZATION_REQUIRED"


def test_scheduled_failures_cannot_hide_fresh_notice_behind_batch_limit(scenario):
    store = scenario.store
    store._delivery_clock = lambda: 1_000
    with store.transaction() as db:
        db.executemany("INSERT INTO outbox(id,source_event_id,topic,destination_room_id,visibility,payload_json,created_at,delivery_state,next_attempt_at_ms) "
            "VALUES (?,?, 'notice','priya-room','claimant','{}','2026-11-07','FAILED',999999)",
            [(f"old-{i}", f"old-source-{i}") for i in range(100)])
    enqueue(store, "fresh-due")
    assert [r["id"] for r in store.pending_outbox(due_only=True)] == ["fresh-due"]


def test_legacy_migration_backfills_budget_and_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE outbox (id TEXT PRIMARY KEY,delivery_state TEXT,attempts INTEGER,last_error TEXT)")
        db.execute("INSERT INTO outbox VALUES ('old','FAILED',9,'legacy secret')")
        delivery.ensure_columns(db, "outbox")
    with sqlite3.connect(path) as db:
        delivery.ensure_columns(db, "outbox")
        saved = db.execute("SELECT delivery_state,attempts,consecutive_failures,blocked_reason FROM outbox").fetchone()
    assert saved == ("BLOCKED", 9, 9, "RETRY_LIMIT")


def test_migration_rolls_back_partial_alter_and_backfill_on_interruption(tmp_path, monkeypatch):
    path = tmp_path / "interrupted.db"
    db = sqlite3.connect(path, isolation_level=None)
    db.execute("CREATE TABLE outbox (id TEXT PRIMARY KEY,delivery_state TEXT,attempts INTEGER,last_error TEXT)")
    db.execute("INSERT INTO outbox VALUES ('old','FAILED',9,'legacy secret')")
    original = delivery._ensure_columns

    def interrupted(connection, table):
        original(connection, table)
        raise RuntimeError("simulated crash before migration commit")

    monkeypatch.setattr(delivery, "_ensure_columns", interrupted)
    with pytest.raises(RuntimeError, match="simulated crash"):
        delivery.ensure_columns(db, "outbox")
    assert {r[1] for r in db.execute("PRAGMA table_info(outbox)")} == {"id", "delivery_state", "attempts", "last_error"}
    assert db.execute("SELECT delivery_state FROM outbox").fetchone()[0] == "FAILED"
    monkeypatch.setattr(delivery, "_ensure_columns", original)
    delivery.ensure_columns(db, "outbox")
    assert db.execute("SELECT delivery_state,blocked_reason FROM outbox").fetchone() == ("BLOCKED", "RETRY_LIMIT")
    db.close()


def test_health_is_read_only_payload_free_and_uses_one_snapshot(scenario, monkeypatch):
    secret = "PRIVATE_PAYLOAD_TOKEN_email@example.test"
    store = scenario.store
    enqueue(store, secret, body=secret)
    journal = RuntimeJournal(store)
    journal.queue("runtime-private-id", secret, secret)
    with store.transaction() as db:
        db.execute("UPDATE outbox SET delivery_state='FAILED',last_error=?,next_attempt_at_ms=999999 WHERE id=?", (secret, secret))
    traces = []
    connect = delivery.sqlite3.connect

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(traces.append)
        return connection

    monkeypatch.setattr(delivery.sqlite3, "connect", traced)
    report = delivery.health(scenario.path, clock_ms=1)
    assert secret not in json.dumps(report)
    assert report["queues"]["outbox"]["safe_error_codes"] == {"LEGACY_OR_UNKNOWN": 1}
    assert report["queues"]["outbox"]["scheduled"] == 1
    assert report["scope"] == "durable_queues_only" and report["process_liveness_checked"] is False
    assert traces.index("BEGIN") < next(i for i, sql in enumerate(traces) if "COUNT(*)" in sql)
    assert not any(sql.startswith(("UPDATE", "ALTER", "INSERT", "CREATE")) for sql in traces)


def test_health_cli_never_creates_state_or_loads_credentials(tmp_path, monkeypatch, capsys):
    state = tmp_path / "absent-state"
    monkeypatch.setattr(sys, "argv", ["findback", "--state-dir", str(state), "health"])
    monkeypatch.setattr(cli, "load_config", lambda _: pytest.fail("health must not load credentials"))
    cli.main()
    assert json.loads(capsys.readouterr().out)["status"] == "not_initialized"
    assert not state.exists()


def test_health_counts_remain_one_snapshot_when_live_writer_sends_between_queries(scenario, monkeypatch):
    enqueue(scenario.store, "concurrent-notice")
    original_connect = sqlite3.connect
    changed = []

    def traced(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        def concurrent_send(sql):
            if "COUNT(*)" in sql and "next_attempt_at_ms" in sql and not changed:
                with original_connect(scenario.path) as writer:
                    writer.execute("UPDATE outbox SET delivery_state='SENT' WHERE id='concurrent-notice'")
                changed.append(True)
        connection.set_trace_callback(concurrent_send)
        return connection

    monkeypatch.setattr(delivery.sqlite3, "connect", traced)
    report = delivery.health(scenario.path, clock_ms=1)
    queue = report["queues"]["outbox"]
    assert changed and queue["states"]["PENDING"] == 1 and queue["due"] == 1 and queue["scheduled"] == 0
    assert row(scenario.store, "outbox", "concurrent-notice")["delivery_state"] == "SENT"


def test_retry_cli_can_target_one_queue_without_replaying_domain_or_touching_sent(scenario, monkeypatch, capsys):
    journal = RuntimeJournal(scenario.store)
    enqueue(scenario.store, "domain-notice")
    scenario.store.mark_outbox_failed("domain-notice", SwitchHTTPError(403))
    journal.queue("retry-me", "priya-room", "PRIVATE_BODY")
    journal.mark_delivery_failed("retry-me", SwitchHTTPError(403))
    journal.queue("already-sent", "priya-room", "PRIVATE_BODY")
    journal.mark_delivery_sent("already-sent", "actual-remote")
    monkeypatch.setattr(sys, "argv", ["findback", "--state-dir", str(scenario.path.parent), "retry-delivery", "--queue", "runtime", "--id", "retry-me"])
    cli.main()
    report = json.loads(capsys.readouterr().out)
    assert report["requeued"] == {"runtime_messages": 1}
    assert "PRIVATE_BODY" not in json.dumps(report)
    assert row(scenario.store, "runtime_messages", "retry-me")["manual_retries"] == 1
    assert row(scenario.store, "outbox", "domain-notice")["delivery_state"] == "BLOCKED"
    assert row(scenario.store, "runtime_messages", "already-sent")["remote_id"] == "actual-remote"
