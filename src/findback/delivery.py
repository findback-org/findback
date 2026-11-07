"""Persistent delivery retries and payload-free local operator diagnostics."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import sqlite3
import time
from typing import Any

import httpx

from .switch_client import (SwitchError, SwitchHTTPError, SwitchProtocolError,
                            SwitchRefusedError, SwitchUnknownOutcomeError)


MAX_FAILURES = 8
BASE_DELAY_MS = 2_000
MAX_DELAY_MS = 300_000
STATES = ("PENDING", "FAILED", "BLOCKED", "SENT")
SAFE_CODES = frozenset({
    "AUTHORIZATION_REQUIRED", "DESTINATION_UNAVAILABLE", "REQUEST_REJECTED",
    "CONNECTION_REFUSED", "RECONCILIATION_REQUIRED", "UNKNOWN_POST_OUTCOME",
    "NETWORK_UNAVAILABLE", "DELIVERY_FAILED", "RETRY_LIMIT", "DEPENDENCY_BLOCKED",
    "LEGACY_OR_UNKNOWN",
})
QUEUES = {"outbox": "delivery_state", "runtime_messages": "state"}


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_code(value: Any) -> str:
    return value if isinstance(value, str) and value in SAFE_CODES else "LEGACY_OR_UNKNOWN"


def classify(error: Exception | str) -> tuple[str, bool]:
    """Return an allowlisted code and whether operator action is required.

    Exception prose can contain credentials or claimant details. It is never
    persisted or returned by health, including for legacy string callers.
    """
    if isinstance(error, SwitchHTTPError):
        if error.status in {401, 403}:
            return "AUTHORIZATION_REQUIRED", True
        if error.status in {404, 410}:
            return "DESTINATION_UNAVAILABLE", True
        if error.status in {400, 405, 409, 422}:
            return "REQUEST_REJECTED", True
        return "NETWORK_UNAVAILABLE", False
    if isinstance(error, SwitchUnknownOutcomeError):
        return "UNKNOWN_POST_OUTCOME", False
    if isinstance(error, SwitchProtocolError):
        return "RECONCILIATION_REQUIRED", True
    if isinstance(error, SwitchRefusedError):
        return "CONNECTION_REFUSED", True
    if isinstance(error, (SwitchError, httpx.TransportError, TimeoutError, OSError)):
        return "NETWORK_UNAVAILABLE", False
    if isinstance(error, RuntimeError):
        # Includes the adapter's fail-closed history bounds. A blind retry
        # cannot repair incomplete or non-progressing reconciliation history.
        return "RECONCILIATION_REQUIRED", True
    if isinstance(error, str) and error in SAFE_CODES:
        return error, error in {"AUTHORIZATION_REQUIRED", "DESTINATION_UNAVAILABLE",
            "REQUEST_REJECTED", "CONNECTION_REFUSED", "RECONCILIATION_REQUIRED", "DEPENDENCY_BLOCKED"}
    return "DELIVERY_FAILED", False


def ensure_columns(db: sqlite3.Connection, table: str) -> None:
    """Serialize and atomically commit migration/backfill, reusing a caller's transaction."""
    own_transaction = not db.in_transaction
    if own_transaction:
        db.execute("BEGIN IMMEDIATE")
    try:
        _ensure_columns(db, table)
    except BaseException:
        if own_transaction:
            db.rollback()
        raise
    else:
        if own_transaction:
            db.commit()


def _ensure_columns(db: sqlite3.Connection, table: str) -> None:
    state = QUEUES[table]  # Only these hardcoded table/column names enter SQL.
    columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    fields = {"attempts": "INTEGER NOT NULL DEFAULT 0", "last_error": "TEXT",
        "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
        "manual_retries": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at_ms": "INTEGER NOT NULL DEFAULT 0", "last_attempt_at_ms": "INTEGER",
        "blocked_reason": "TEXT"}
    for name, definition in fields.items():
        if name not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    if "consecutive_failures" not in columns:
        db.execute(f"UPDATE {table} SET consecutive_failures=attempts WHERE {state}='FAILED'")
        db.execute(f"UPDATE {table} SET {state}='BLOCKED',blocked_reason='RETRY_LIMIT' "
                   f"WHERE {state}='FAILED' AND consecutive_failures>=?", (MAX_FAILURES,))
    db.execute(f"CREATE INDEX IF NOT EXISTS {table}_delivery_due ON {table}({state},next_attempt_at_ms)")


def record_failure(db: sqlite3.Connection, table: str, message_id: str,
                   error: Exception | str, clock_ms: int) -> None:
    state = QUEUES[table]
    row = db.execute(f"SELECT {state},consecutive_failures FROM {table} WHERE id=?", (message_id,)).fetchone()
    if row is None or row[0] in {"SENT", "BLOCKED"}:
        return
    code, permanent = classify(error)
    failures = row[1] + 1
    blocked = permanent or failures >= MAX_FAILURES
    reason = code if permanent else "RETRY_LIMIT" if blocked else None
    due = 0 if blocked else clock_ms + min(BASE_DELAY_MS * 2 ** (failures - 1), MAX_DELAY_MS)
    db.execute(f"UPDATE {table} SET {state}=?,attempts=attempts+1,consecutive_failures=?,"
               "last_error=?,last_attempt_at_ms=?,next_attempt_at_ms=?,blocked_reason=? WHERE id=?",
               ("BLOCKED" if blocked else "FAILED", failures, code, clock_ms, due, reason, message_id))


def retry_delivery(db: sqlite3.Connection, queue: str = "all", message_id: str | None = None) -> dict[str, int]:
    names = list(QUEUES) if queue == "all" else [queue]
    result = {}
    for table in names:
        state = QUEUES[table]
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not exists:
            result[table] = 0
            continue
        ensure_columns(db, table)
        clause, values = (" AND id=?", (message_id,)) if message_id else ("", ())
        result[table] = db.execute(f"UPDATE {table} SET {state}='PENDING',consecutive_failures=0,"
            "next_attempt_at_ms=0,last_error=NULL,blocked_reason=NULL,manual_retries=manual_retries+1 "
            f"WHERE {state} IN ('FAILED','BLOCKED'){clause}", values).rowcount
    return result


def health(db_path: str | Path, *, clock_ms: int | None = None) -> dict:
    """Read committed state without migrating, creating a DB, or loading config.

    No payload, destination, event ID, account identity or credential is selected.
    Raw errors are never returned; legacy strings become fixed safe labels.
    """
    path = Path(db_path).resolve()
    if not path.exists():
        return {"database_exists": False, "status": "not_initialized", "scope": "durable_queues_only",
                "process_liveness_checked": False, "queues": {}, "inbox": {}}
    clock_ms = now_ms() if clock_ms is None else clock_ms
    queues, inbox = {}, {}
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")  # One committed WAL snapshot for all aggregate counts.
        for table, state in QUEUES.items():
            columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
            if not columns:
                continue
            counts = dict.fromkeys(STATES, 0)
            for value, count in db.execute(f"SELECT {state},COUNT(*) FROM {table} GROUP BY {state}"):
                label = value if value in STATES else "OTHER"
                counts[label] = counts.get(label, 0) + count
            metadata = {"next_attempt_at_ms", "blocked_reason", "consecutive_failures"} <= columns
            due_clause = " AND next_attempt_at_ms<=?" if metadata else ""
            values = (clock_ms,) if metadata else ()
            due = db.execute(f"SELECT COUNT(*) FROM {table} WHERE {state} IN ('PENDING','FAILED'){due_clause}", values).fetchone()[0]
            errors = {}
            if "last_error" in columns:
                for error, count in db.execute(f"SELECT last_error,COUNT(*) FROM {table} WHERE {state}!='SENT' AND last_error IS NOT NULL GROUP BY last_error"):
                    code = safe_code(error)
                    errors[code] = errors.get(code, 0) + count
            queues[table] = {"states": counts, "due": due, "scheduled": counts["PENDING"] + counts["FAILED"] - due,
                             "safe_error_codes": errors, "persistent_retry_metadata": metadata}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inbox'").fetchone():
            for value, count in db.execute("SELECT state,COUNT(*) FROM inbox GROUP BY state"):
                label = value if value in {"PENDING", "FAILED", "DONE", "REJECTED"} else "OTHER"
                inbox[label] = inbox.get(label, 0) + count
    blocked = sum(queue["states"]["BLOCKED"] for queue in queues.values())
    failed = sum(queue["states"]["FAILED"] for queue in queues.values())
    status = "attention_required" if blocked or inbox.get("FAILED") else "retry_scheduled" if failed else "healthy"
    return {"database_exists": True, "status": status, "scope": "durable_queues_only",
            "process_liveness_checked": False, "queues": queues, "inbox": inbox}
