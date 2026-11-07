"""Opt-in authenticated scripted roleplay. Never confuses fixtures with users.

Run only against the private demo enrollment, with FindBack's runtime listening.
The script will advance those fictional cases. It never clears existing state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timezone

import httpx

from .storage import SQLiteStore


def run(state: Path, evidence_dir: Path) -> dict:
    config = json.loads((state / "config.json").read_text())
    store = SQLiteStore(state / "findback.db")
    client = httpx.Client(base_url=config["mattermost_url"], timeout=30)
    records = []

    def row(sql, args=()):
        with store.connection() as db:
            value = db.execute(sql, args).fetchone()
            return dict(value) if value else None

    def wait(label, predicate, timeout=210):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            value = predicate()
            if value:
                return value
            time.sleep(0.25)
        raise RuntimeError("Timed out waiting for " + label)

    def post(role, command, label):
        case = config["cases"].get(role)
        human = case["human"] if case else config["staff"]["human"]
        channel = case["channel_id"] if case else config["staff_room"]["channel_id"]
        response = client.post("/api/v4/posts", headers={"Authorization": "Bearer " + human["token"]},
                               json={"channel_id": channel, "message": command})
        response.raise_for_status()
        remote = response.json()
        source = "\x1f".join([config["bridge_id"], channel, remote["id"]])
        event_id = "sw_" + hashlib.sha256("\x1f".join([human["client_id"], source, "0"]).encode()).hexdigest()[:32]
        wait("durable inbox: " + label, lambda: row("SELECT * FROM inbox WHERE event_id=? AND state IN ('DONE','FAILED')", (event_id,)))
        inbox = row("SELECT state,error FROM inbox WHERE event_id=?", (event_id,))
        if inbox["state"] == "FAILED":
            raise RuntimeError(f"Runtime failed {label}: {inbox['error']}")
        mutation = row("SELECT result_json FROM processed_events WHERE source_event_id=?", (event_id,))
        result = json.loads(mutation["result_json"]) if mutation else None
        records.append({"label": label, "role": role, "behavior": "scripted fictional input through an authenticated Mattermost account",
                        "mattermost_post_id": remote["id"], "switch_event_id": event_id, "command": command,
                        "result_code": result["code"] if result else "PROPOSAL_OR_READ"})
        print(label + ": " + records[-1]["result_code"], flush=True)
        return event_id, result

    try:
        if row("SELECT revision FROM claims WHERE id='C001'")["revision"] != 0:
            raise RuntimeError("This enrolled demo has already advanced. Inspect it; the script never resets history.")
        # Label the room before any scripted assertions or staff actions.
        for role in ("C001", "C002", "staff"):
            case = config["cases"].get(role)
            human = case["human"] if case else config["staff"]["human"]
            channel = case["channel_id"] if case else config["staff_room"]["channel_id"]
            response = client.post("/api/v4/posts", headers={"Authorization": "Bearer " + human["token"]}, json={
                "channel_id": channel,
                "message": "**Fictional FindBack live integration.** The following messages are scripted roleplay from distinct enrolled demo accounts. Switch transport, Codex inference and database transitions run live. No physical object is being returned."})
            response.raise_for_status()
        post("C001", "findback report I lost my black headphone case at the library this afternoon.", "Priya report / live Intake model")
        draft = row("SELECT current_draft_id FROM claims WHERE id='C001'")["current_draft_id"]
        post("C001", "findback confirm " + draft, "Priya confirms exact draft / live Matcher model")
        first = row("SELECT * FROM reviews WHERE claim_id='C001' ORDER BY rowid DESC LIMIT 1")
        comparison = json.loads(first["deterministic_comparison_json"])
        assert len(comparison["candidates"]) == 2, "Broad black-case report must keep both candidates"
        post("C002", "findback report I lost a black headphone case in the library. I do not remember any identifying marks.", "Alex report / separate live Intake model")
        draft = row("SELECT current_draft_id FROM claims WHERE id='C002'")["current_draft_id"]
        post("C002", "findback confirm " + draft, "Alex confirms / insufficient evidence comparison")
        post("staff", "findback ask C002 Both black cases fit only the coarse description; no independently identifying evidence yet.", "Mira requests more information for Alex")
        _, refusal = post("C001", f"findback approve C001 I-001 {first['id']} I approve myself.", "Claimant authorization attempt")
        assert refusal["code"] == "FORBIDDEN"
        post("C001", "findback evidence Mine has a green triangle sticker under the lid and a short blue charging cable inside.", "Priya independent assertion / refreshed live Matcher model")
        current = row("SELECT * FROM reviews WHERE claim_id='C001' ORDER BY rowid DESC LIMIT 1")
        _, stale = post("staff", f"findback approve C001 I-001 {first['id']} Attempt old comparison after new evidence.", "Old review is refused")
        assert stale["code"] == "STALE_REVISION"
        _, approved = post("staff", f"findback approve C001 I-001 {current['id']} Priya independently described both the hidden sticker and cable; I reviewed the source assertion against I-001 observations.", "Mira approves exact evidence and reserves")
        assert approved["code"] == "RESERVED"
        rs = approved["data"]["reservation_id"]
        post("staff", f"findback collect C001 {rs} Fictional collection at Library Desk A, 17:00. Ask the custodian for your reserved case.", "Private collection instructions")
        assert row("SELECT status FROM claims WHERE id='C001'")["status"] == "APPROVED"
        _, early = post("C001", f"findback receipt {rs}", "Premature receipt refused")
        assert early["code"] == "HANDOVER_REQUIRED"
        post("staff", f"findback handover C001 {rs} Scripted fictional handover for the integration demo; no physical object moved.", "Enrolled custodian records fictional handover")
        _, returned = post("C001", f"findback receipt {rs}", "Enrolled claimant confirms fictional receipt")
        assert returned["code"] == "RETURNED"
        _, repeat = post("C001", f"findback receipt {rs}", "Second receipt message reuses one return")
        assert repeat["data"]["receipt_id"] == returned["data"]["receipt_id"]
        wait("all domain notices delivered", lambda: not row("SELECT id FROM outbox WHERE delivery_state!='SENT'"), timeout=90)
        wait("all adapter notices delivered", lambda: not row("SELECT id FROM runtime_messages WHERE state!='SENT'"), timeout=90)
        totals = {table: row(f"SELECT COUNT(*) AS n FROM {table}")["n"] for table in ("claims", "reviews", "reservations", "returns", "audit_events", "model_proposals")}
        alex = row("SELECT status FROM claims WHERE id='C002'")["status"]
        assert alex == "NEEDS_INFO" and totals["returns"] == 1 and totals["reservations"] == 1
        with store.connection() as db:
            measurements = [json.loads(r[0]) for r in db.execute("SELECT metrics_json FROM model_proposals ORDER BY rowid")]
        result = {"passed": True, "recorded_at": datetime.now(timezone.utc).isoformat(),
                  "behavior": {"transport": "live local Switch + Mattermost", "models": "live signed-in Codex CLI", "human_input": "scripted authenticated fictional roleplay", "physical_handover": "simulated; no real property moved"},
                  "events": records, "totals": totals, "alex_status": alex,
                  "priya_status": "RETURNED", "receipt_id": returned["data"]["receipt_id"], "model_measurements": measurements}
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "live-run.json").write_text(json.dumps(result, indent=2))
        print("Live scripted roleplay passed; exactly one return receipt and Alex remains NEEDS_INFO.", flush=True)
        return result
    finally:
        client.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".findback"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    parser.add_argument("--scripted-roleplay", action="store_true", required=True,
                        help="Acknowledge that fictional account messages and handover are scripted")
    args = parser.parse_args()
    run(args.state_dir.resolve(), args.evidence_dir.resolve())


if __name__ == "__main__":
    main()
