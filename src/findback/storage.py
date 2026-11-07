"""SQLite persistence for FindBack. No transport credentials or model calls live here."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from uuid import uuid4

from .delivery import ensure_columns, now_ms, record_failure


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def loads(value: str | None, default: Any = None) -> Any:
    return json.loads(value) if value is not None else default


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS principals (actor_id TEXT PRIMARY KEY, actor_kind TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS case_bindings (
 claim_id TEXT PRIMARY KEY, claimant_actor_id TEXT NOT NULL, case_room_id TEXT NOT NULL UNIQUE,
 intake_agent_ids_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS items (
 id TEXT PRIMARY KEY, coarse_json TEXT NOT NULL, private_facts_json TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('AVAILABLE','RESERVED','RETURNED','WITHDRAWN')),
 revision INTEGER NOT NULL DEFAULT 1, state_version INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
 id TEXT PRIMARY KEY REFERENCES case_bindings(claim_id), claimant_actor_id TEXT NOT NULL,
 case_room_id TEXT NOT NULL, confirmed_report_json TEXT, current_draft_id TEXT,
 status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, evidence_revision INTEGER NOT NULL DEFAULT 0,
 state_version INTEGER NOT NULL DEFAULT 1, selected_item_id TEXT REFERENCES items(id),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_messages (
 source_event_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, actor_kind TEXT NOT NULL,
 room_id TEXT NOT NULL, source_text TEXT NOT NULL, occurred_at TEXT NOT NULL,
 origin TEXT NOT NULL, thread_id TEXT);
CREATE TABLE IF NOT EXISTS drafts (
 id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claims(id), fields_json TEXT NOT NULL,
 source_event_ids_json TEXT NOT NULL, created_by TEXT NOT NULL, source_event_id TEXT NOT NULL,
 source_kind TEXT NOT NULL, draft_revision INTEGER NOT NULL, created_at TEXT NOT NULL, confirmed_at TEXT);
CREATE TABLE IF NOT EXISTS evidence (
 id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claims(id), text TEXT NOT NULL,
 attachment_ref TEXT, actor_id TEXT NOT NULL, actor_kind TEXT NOT NULL,
 source_event_id TEXT NOT NULL, origin TEXT NOT NULL, occurred_at TEXT NOT NULL,
 supersedes_id TEXT REFERENCES evidence(id));
CREATE TABLE IF NOT EXISTS reviews (
 id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claims(id), claim_revision INTEGER NOT NULL,
 evidence_revision INTEGER NOT NULL, item_revisions_json TEXT NOT NULL,
 deterministic_comparison_json TEXT NOT NULL, proposed_analysis_json TEXT,
 authored_by TEXT NOT NULL, source_event_id TEXT NOT NULL, created_at TEXT NOT NULL,
 invalidated_at TEXT, invalidated_reason TEXT);
CREATE TABLE IF NOT EXISTS approvals (
 id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claims(id), claim_revision INTEGER NOT NULL,
 evidence_revision INTEGER NOT NULL, item_id TEXT NOT NULL REFERENCES items(id), item_revision INTEGER NOT NULL,
 review_id TEXT NOT NULL REFERENCES reviews(id), staff_actor_id TEXT NOT NULL,
 source_event_id TEXT NOT NULL UNIQUE, reason TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reservations (
 id TEXT PRIMARY KEY, approval_id TEXT NOT NULL REFERENCES approvals(id),
 claim_id TEXT NOT NULL REFERENCES claims(id), item_id TEXT NOT NULL REFERENCES items(id),
 status TEXT NOT NULL CHECK(status IN ('ACTIVE','HANDOVER_RECORDED','RETURNED','CANCELLED')),
 collection_instructions TEXT, created_at TEXT NOT NULL, cancelled_at TEXT, cancellation_reason TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_item_reservation ON reservations(item_id)
 WHERE status IN ('ACTIVE','HANDOVER_RECORDED');
CREATE UNIQUE INDEX IF NOT EXISTS one_active_claim_reservation ON reservations(claim_id)
 WHERE status IN ('ACTIVE','HANDOVER_RECORDED');
CREATE TABLE IF NOT EXISTS returns (
 reservation_id TEXT PRIMARY KEY REFERENCES reservations(id),
 staff_handover_event_id TEXT NOT NULL UNIQUE, staff_actor_id TEXT NOT NULL,
 handed_over_at TEXT NOT NULL, handover_note TEXT NOT NULL,
 claimant_receipt_event_id TEXT UNIQUE, claimant_actor_id TEXT,
 receipt_id TEXT UNIQUE, returned_at TEXT);
CREATE TABLE IF NOT EXISTS audit_events (
 id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL, origin TEXT NOT NULL,
 actor_id TEXT NOT NULL, actor_kind TEXT NOT NULL, room_id TEXT NOT NULL, action TEXT NOT NULL,
 claim_id TEXT, item_id TEXT, revisions_json TEXT NOT NULL, previous_json TEXT NOT NULL,
 next_json TEXT NOT NULL, result_code TEXT NOT NULL, details_json TEXT NOT NULL, occurred_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_by_claim ON audit_events(claim_id, occurred_at);
CREATE TABLE IF NOT EXISTS processed_events (
 source_event_id TEXT PRIMARY KEY, request_fingerprint TEXT NOT NULL,
 result_json TEXT NOT NULL, processed_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL, topic TEXT NOT NULL,
 destination_room_id TEXT NOT NULL, visibility TEXT NOT NULL, payload_json TEXT NOT NULL,
 delivery_state TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,
 remote_event_id TEXT, last_error TEXT, created_at TEXT NOT NULL, sent_at TEXT);
"""


class SQLiteStore:
    def __init__(self, db_path: str | Path, *, delivery_clock=None):
        self.db_path = str(db_path)
        self._delivery_clock = delivery_clock or now_ms
        self._anchor: sqlite3.Connection | None = None
        self._uri = self.db_path == ":memory:"
        if self._uri:
            self.db_path = f"file:findback-{uuid4().hex}?mode=memory&cache=shared"
            self._anchor = self.connect()
        else:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript(SCHEMA)
            ensure_columns(db, "outbox")

    def delivery_now_ms(self) -> int:
        return self._delivery_clock()

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=15, isolation_level=None, uri=self._uri)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        if not self._uri:
            db.execute("PRAGMA journal_mode=WAL")
        return db

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = self.connect()
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def close(self) -> None:
        if self._anchor:
            self._anchor.close()
            self._anchor = None

    def configure_enrollment(self, *, staff_room_id: str, staff_actor_ids: list[str],
                             matcher_agent_ids: list[str], cases: list[dict]) -> None:
        if not staff_room_id or not staff_actor_ids:
            raise ValueError("A staff room and at least one enrolled staff actor are required")
        with self.transaction() as db:
            for key, value in {"staff_room_id": staff_room_id, "staff_actor_ids": list(staff_actor_ids),
                               "matcher_agent_ids": list(matcher_agent_ids)}.items():
                db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                           (key, dumps(value)))
            def principal(actor_id: str, kind: str) -> None:
                prior = db.execute("SELECT actor_kind FROM principals WHERE actor_id=?", (actor_id,)).fetchone()
                if prior and prior[0] != kind:
                    raise ValueError("An enrolled principal cannot change human/agent kind")
                db.execute("INSERT OR IGNORE INTO principals VALUES (?,?)", (actor_id, kind))
            for actor in staff_actor_ids:
                principal(actor, "human")
            for actor in matcher_agent_ids:
                principal(actor, "agent")
            for case in cases:
                cid = case["claim_id"]
                claimant = case["claimant_actor_id"]
                room = case["case_room_id"]
                agents = case.get("intake_agent_ids", [])
                if isinstance(agents, str):
                    agents = [agents]
                old = db.execute("SELECT * FROM case_bindings WHERE claim_id=?", (cid,)).fetchone()
                if old and (old["claimant_actor_id"] != claimant or old["case_room_id"] != room):
                    raise ValueError("Existing case identity/room binding cannot be silently changed")
                principal(claimant, "human")
                for actor in agents:
                    principal(actor, "agent")
                db.execute("INSERT INTO case_bindings VALUES (?,?,?,?) ON CONFLICT(claim_id) DO UPDATE SET intake_agent_ids_json=excluded.intake_agent_ids_json",
                           (cid, claimant, room, dumps(agents)))
                now = utcnow()
                db.execute("INSERT OR IGNORE INTO claims(id,claimant_actor_id,case_room_id,status,created_at,updated_at) VALUES (?,?,?,'OPEN',?,?)",
                           (cid, claimant, room, now, now))

    @staticmethod
    def setting(db: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = db.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
        return loads(row[0], default) if row else default

    @staticmethod
    def ingest_in(db: sqlite3.Connection, ctx: Any) -> None:
        values = (ctx.source_event_id, ctx.actor_id, ctx.actor_kind, ctx.room_id,
                  ctx.source_text, ctx.occurred_at or utcnow(), ctx.origin, ctx.thread_id)
        old = db.execute("SELECT * FROM source_messages WHERE source_event_id=?", (ctx.source_event_id,)).fetchone()
        if old:
            # Timestamps may differ on redelivery; identity, source and content may not.
            if any(old[k] != getattr(ctx, k) for k in ("actor_id", "actor_kind", "room_id", "source_text", "origin")):
                raise ValueError("Source event identity/content mismatch")
            return
        db.execute("INSERT INTO source_messages VALUES (?,?,?,?,?,?,?,?)", values)

    def ingest_message(self, ctx: Any) -> None:
        with self.transaction() as db:
            self.ingest_in(db, ctx)

    def pending_outbox(self, limit: int = 100, *, due_only: bool = False) -> list[dict]:
        with self.connection() as db:
            clause = " AND next_attempt_at_ms<=?" if due_only else ""
            values = (self.delivery_now_ms(), limit) if due_only else (limit,)
            rows = db.execute("SELECT * FROM outbox WHERE delivery_state IN ('PENDING','FAILED')" + clause + " ORDER BY created_at,id LIMIT ?", values).fetchall()
            return [{**dict(row), "payload": loads(row["payload_json"])} for row in rows]

    def mark_outbox_sent(self, outbox_id: str, remote_event_id: str) -> None:
        with self.transaction() as db:
            db.execute("UPDATE outbox SET delivery_state='SENT',remote_event_id=?,sent_at=?,attempts=attempts+1,last_error=NULL,"
                       "consecutive_failures=0,next_attempt_at_ms=0,blocked_reason=NULL,last_attempt_at_ms=? WHERE id=? AND delivery_state!='SENT'",
                       (remote_event_id, utcnow(), self.delivery_now_ms(), outbox_id))

    def mark_outbox_failed(self, outbox_id: str, error: Exception | str) -> None:
        with self.transaction() as db:
            record_failure(db, "outbox", outbox_id, error, self.delivery_now_ms())

    def defer_outbox(self, outbox_id: str, delay_ms: int = 1000) -> None:
        with self.transaction() as db:
            db.execute("UPDATE outbox SET next_attempt_at_ms=? WHERE id=? AND delivery_state IN ('PENDING','FAILED')",
                       (self.delivery_now_ms() + delay_ms, outbox_id))
