"""Capture domain state, then verify a manual runtime restart and live read.

--capture is read-only. --verify-after sends a scripted status query from the
fictional claimant account; it does not mutate an authoritative case record.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import httpx

from .storage import SQLiteStore


def snapshot(store: SQLiteStore) -> dict:
    tables = ["items", "claims", "reviews", "approvals", "reservations", "returns"]
    with store.connection() as db:
        records = {table: [dict(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY rowid")] for table in tables}
        return {"domain_digest": hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
                "counts": {table: len(records[table]) for table in tables},
                "cases": [{key: row[key] for key in ("id", "status", "revision", "evidence_revision")} for row in records["claims"]],
                "notices_pending": db.execute("SELECT COUNT(*) FROM outbox WHERE delivery_state!='SENT'").fetchone()[0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".findback"))
    parser.add_argument("--output", type=Path, default=Path("evidence/restart-checks.json"))
    phase = parser.add_mutually_exclusive_group(required=True)
    phase.add_argument("--capture", action="store_true")
    phase.add_argument("--verify-after", action="store_true")
    args = parser.parse_args()
    state = args.state_dir.resolve()
    store = SQLiteStore(state / "findback.db")
    checkpoint = state / "restart-before.json"
    try:
        if args.capture:
            checkpoint.write_text(json.dumps(snapshot(store), indent=2))
            checkpoint.chmod(0o600)
            print("Captured authoritative state. Stop and restart findback run, then use --verify-after.")
            return
        before = json.loads(checkpoint.read_text())
        config = json.loads((state / "config.json").read_text())
        case = config["cases"]["C001"]
        human = case["human"]
        headers = {"Authorization": "Bearer " + human["token"]}
        with httpx.Client(base_url=config["mattermost_url"], timeout=20) as client:
            response = client.post("/api/v4/posts", headers=headers, json={"channel_id": case["channel_id"], "message": "findback status"})
            response.raise_for_status()
            post = response.json()
            material = "\x1f".join([human["client_id"], config["bridge_id"], case["channel_id"], post["id"], "0"])
            event_id = "sw_" + hashlib.sha256(material.encode()).hexdigest()[:32]
            until = time.monotonic() + 30
            delivered = None
            while time.monotonic() < until:
                with store.connection() as db:
                    delivered = db.execute("SELECT remote_id FROM runtime_messages WHERE id=? AND state='SENT'", (event_id + ":result",)).fetchone()
                if delivered:
                    break
                time.sleep(0.15)
            if not delivered:
                raise RuntimeError("Restarted runtime did not deliver the authenticated status reply.")
            posts = client.get(f"/api/v4/channels/{case['channel_id']}/posts", headers=headers, params={"per_page": 200})
            posts.raise_for_status()
            marker = f"[findback:{event_id}:result]"
            messages = [post["message"] for post in posts.json()["posts"].values() if marker in post["message"]]
            after = snapshot(store)
            result = {"passed": before == after and len(messages) == 1 and "RETURNED" in messages[0],
                      "verified_at": datetime.now(timezone.utc).isoformat(),
                      "behavior": "Actual local runtime stopped/restarted; authenticated fictional claimant status command delivered live afterward.",
                      "before": before, "after": after, "status_reply_count": len(messages),
                      "status_reports_returned": bool(messages and "RETURNED" in messages[0]),
                      "limitations": "Live restart checked final state. Restart during active reservation is covered by domain tests."}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2))
            print(json.dumps(result, indent=2))
            if not result["passed"]:
                raise SystemExit(1)
    finally:
        store.close()


if __name__ == "__main__":
    main()
