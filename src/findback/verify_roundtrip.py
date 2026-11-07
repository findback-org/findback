"""Opt-in live bot impersonation/thread checks against a running demo.

Sends two clearly scripted diagnostic messages, neither can release an item.
Uses the runtime's existing connection selectors; opens no competing WebSocket.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

import httpx

from .storage import SQLiteStore


def verify(state: Path, output: Path) -> dict:
    config = json.loads((state / "config.json").read_text())
    connection = json.loads((state / "connection-matcher.json").read_text())
    agent, room = config["agents"]["matcher"], config["staff_room"]["id"]
    store = SQLiteStore(state / "findback.db")
    with httpx.Client(base_url=config["switch_url"], timeout=20) as client:
        headers = {"Authorization": "Bearer " + agent["api_key"], "X-Switch-Connection-Id": connection["connection_id"],
                   "X-Switch-Session-Id": str(uuid.uuid5(uuid.UUID(connection["connection_id"]), room))}

        def op(name, body):
            response = client.post(f"/agents/{agent['id']}/ops/{name}", headers=headers, json=body)
            if response.status_code != 200:
                raise RuntimeError(f"Diagnostic request failed with HTTP {response.status_code}.")
            return response.json()["result"]

        op("connect_to_room", {"room_id": room, "include_general_instructions": False})
        with store.connection() as db:
            review = db.execute("SELECT id FROM reviews WHERE claim_id='C001' ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            count_before = db.execute("SELECT COUNT(*) FROM returns").fetchone()[0]
        command = f"findback approve C001 I-001 {review} Scripted agent authorization negative check; I am an agent, not staff."
        op("post_message", {"body": command})
        until = time.monotonic() + 25
        refusal = None
        while time.monotonic() < until:
            with store.connection() as db:
                row = db.execute("SELECT a.actor_id,a.actor_kind,a.result_code,a.source_event_id FROM audit_events a JOIN source_messages s USING(source_event_id) WHERE s.source_text=? ORDER BY a.rowid DESC LIMIT 1", (command,)).fetchone()
                if row:
                    refusal = dict(row)
                    break
            time.sleep(0.1)
        if not refusal or refusal["result_code"] != "FORBIDDEN" or refusal["actor_kind"] != "agent" or refusal["actor_id"] != agent["actor_id"]:
            raise RuntimeError("The expected authenticated agent refusal was not recorded.")
        # The refusal itself is a real agent message; reply in that exact thread.
        thread = refusal["source_event_id"]
        diagnostic = "FindBack threaded transport check — scripted diagnostic only, not a domain command."
        op("post_message", {"body": diagnostic, "thread_id": thread})
        until = time.monotonic() + 15
        found = False
        while time.monotonic() < until:
            context = op("read_context", {"room_id": room, "limit": 200})
            for group in context["threads"]:
                if group["root"]["id"] == thread and any(r["body"] == diagnostic and r["sender"] == agent["actor_id"] for r in group["replies"]):
                    found = True
            if found:
                break
            time.sleep(0.2)
        with store.connection() as db:
            count_after = db.execute("SELECT COUNT(*) FROM returns").fetchone()[0]
        result = {"passed": found and count_before == count_after,
                  "verified_at": datetime.now(timezone.utc).isoformat(),
                  "behavior": "Live local Switch diagnostic posts from actual Matcher identity; scripted negative approval and threaded read/post.",
                  "agent_approval_result": refusal["result_code"], "recorded_actor_kind": refusal["actor_kind"],
                  "source_event_id": thread, "threaded_reply_read_back": found,
                  "return_count_unchanged": count_before == count_after,
                  "limitations": "No model inference or physical handover is exercised by these diagnostics."}
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2))
        store.close()
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".findback"))
    parser.add_argument("--output", type=Path, default=Path("evidence/roundtrip-checks.json"))
    parser.add_argument("--post-diagnostics", action="store_true", required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.state_dir.resolve(), args.output.resolve()), indent=2))


if __name__ == "__main__":
    main()
