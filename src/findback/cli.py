"""Local operator entry point. Credentials are never printed by these commands."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import uuid

import httpx

from .domain import ActorContext, DomainService
from .fixtures import load_fixtures
from .storage import SQLiteStore
from .delivery import health as delivery_health, retry_delivery


def load_config(state: Path) -> dict:
    path = state / "config.json"
    if not path.exists():
        raise SystemExit("No local setup found. Run: findback bootstrap")
    return json.loads(path.read_text())


def enroll(config: dict, state: Path) -> None:
    store = SQLiteStore(state / "findback.db")
    load_fixtures(store, staff_room_id=config["staff_room"]["id"], staff_actor_id=config["staff"]["actor_id"],
                  claimant_ids={cid: c["claimant_actor_id"] for cid, c in config["cases"].items()},
                  case_room_ids={cid: c["room_id"] for cid, c in config["cases"].items()},
                  intake_agent_ids={cid: [c["intake_agent"]["actor_id"]] for cid, c in config["cases"].items()},
                  matcher_agent_ids=[config["agents"]["matcher"]["actor_id"]])
    store.close()


def offline_demo(state: Path) -> dict:
    """Real domain behavior, entirely fictional transport and model proposal."""
    state.mkdir(parents=True, exist_ok=True)
    path = state / ("offline-" + uuid.uuid4().hex[:8] + ".db")
    store = SQLiteStore(path)
    load_fixtures(store)
    domain = DomainService(store)
    events = []
    count = 0

    def ctx(actor, room, text="", kind="human"):
        nonlocal count
        count += 1
        return ActorContext(actor, kind, room, f"fixture-{count}", text, origin="fixture")

    def step(label, value):
        events.append({"step": label, "code": value["code"], "ok": value["ok"], "data": value["data"]})
        return value["data"]

    report = {"category": "headphone_case", "color": "black", "brand_model": None,
              "lost_place": "library", "lost_at": None, "description": "Black headphone case lost at the library this afternoon."}
    draft = step("Intake proposed draft (fixture)", domain.save_report_draft(ctx("intake-priya", "case-priya", kind="agent"), claim_id="C001", report=report))
    step("Priya confirms draft", domain.confirm_report(ctx("priya", "case-priya"), claim_id="C001", draft_id=draft["draft_id"]))
    step("Two coarse candidates; no ownership decision", domain.candidates(ctx("priya", "case-priya"), claim_id="C001"))
    wrong = domain.approve(ctx("priya", "case-priya"), claim_id="C001", item_id="I-001", review_id="invented", reason="I approve myself")
    step("Claimant cannot authorize release", wrong)
    text = "Mine has a green triangle sticker under the lid and a short blue charging cable inside."
    step("Independent claimant assertion", domain.record_evidence(ctx("priya", "case-priya", text), claim_id="C001", text=text))
    review = step("Matcher proposes comparison (fixture, not live inference)", domain.prepare_review(ctx("matcher", "staff", kind="agent"), claim_id="C001", item_ids=["I-001", "I-002"], analysis={"summary": "I-001 agrees with asserted marks; staff must decide."}))
    reservation = step("Mira approves exact review and reserves atomically", domain.approve(ctx("mira", "staff"), claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Claimant independently described both private identifying features."))
    rs = reservation["reservation_id"]
    step("Collection is still not a return", domain.arrange_collection(ctx("mira", "staff"), claim_id="C001", reservation_id=rs, instructions="Fictional Library Desk A at 17:00."))
    step("Receipt before handover refused", domain.confirm_receipt(ctx("priya", "case-priya"), claim_id="C001", reservation_id=rs))
    step("Fictional staff handover", domain.record_handover(ctx("mira", "staff"), claim_id="C001", reservation_id=rs, note="Scripted fictional handover; no physical object moved."))
    received = ctx("priya", "case-priya")
    step("Fictional claimant receipt closes return", domain.confirm_receipt(received, claim_id="C001", reservation_id=rs))
    replay = domain.confirm_receipt(received, claim_id="C001", reservation_id=rs)
    step("Duplicate event reuses the receipt", replay)
    with store.connection() as db:
        totals = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("returns", "reservations", "audit_events", "outbox")}
    result = {"behavior": "offline fixture demonstration, no live Switch/model or physical return", "database": str(path.resolve()), "events": events, "totals": totals,
              "passed": wrong["code"] == "FORBIDDEN" and replay["replayed"] and totals["returns"] == 1}
    output = state / "offline-demo-results.json"
    output.write_text(json.dumps(result, indent=2))
    output.chmod(0o600)
    store.close()
    return result


def main():
    parser = argparse.ArgumentParser(description="FindBack private lost-property recovery demo")
    parser.add_argument("--state-dir", type=Path, default=Path(os.getenv("FINDBACK_STATE_DIR", ".findback")))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="Check local Switch, Mattermost and Codex without printing credentials")
    sub.add_parser("bootstrap", help="Create fictional accounts, scoped bots and private demo rooms")
    sub.add_parser("run", help="Listen and process authenticated commands")
    sub.add_parser("offline-demo", help="Run a separate fixture-only database demonstration")
    status = sub.add_parser("status", help="Inspect a case locally as the configured operator")
    status.add_argument("claim_id", choices=["C001", "C002"])
    sub.add_parser("retry-failed", help="Requeue interrupted inbox work without deleting domain history")
    sub.add_parser("health", help="Read payload-free durable queue health; does not probe network/process liveness")
    retry = sub.add_parser("retry-delivery", help="Explicitly reset failed/blocked delivery retry budgets; retain history and reconcile markers")
    retry.add_argument("--queue", choices=["all", "outbox", "runtime"], default="all")
    retry.add_argument("--id", dest="message_id", help="Retry only this exact delivery ID")
    args = parser.parse_args()
    state = args.state_dir.resolve()
    if args.command != "health":
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        state.chmod(0o700)
    if args.command == "doctor":
        info = {"python": __import__("sys").version.split()[0], "codex": shutil.which("codex"), "behavior": "local environment check"}
        config = json.loads((state / "config.json").read_text()) if (state / "config.json").exists() else {}
        for key, url in (("switch", config.get("switch_url", "http://127.0.0.1:57897") + "/health"), ("mattermost", config.get("mattermost_url", "http://127.0.0.1:57898") + "/api/v4/system/ping")):
            try:
                info[key] = {"status": httpx.get(url, timeout=5).status_code, "url": url}
            except httpx.HTTPError:
                info[key] = {"status": "unreachable", "url": url}
        if info["codex"]:
            check = subprocess.run([info["codex"], "login", "status"], capture_output=True, text=True)
            info["codex_authenticated"] = check.returncode == 0
        print(json.dumps(info, indent=2))
    elif args.command == "bootstrap":
        from .bootstrap import bootstrap_local
        config = bootstrap_local(state)
        enroll(config, state)
        print("FindBack configured. Fictional accounts and three private rooms are ready.")
        print("Credentials are in the private state directory; they are never sent to a model.")
    elif args.command == "offline-demo":
        result = offline_demo(state)
        print(json.dumps({"passed": result["passed"], "behavior": result["behavior"], "totals": result["totals"], "evidence": str(state / "offline-demo-results.json")}, indent=2))
    elif args.command == "status":
        config = load_config(state)
        store = SQLiteStore(state / "findback.db")
        ctx = ActorContext(config["staff"]["actor_id"], "human", config["staff_room"]["id"], "local-inspection", origin="fixture")
        print(json.dumps(DomainService(store).case_view(ctx, claim_id=args.claim_id), indent=2))
    elif args.command == "health":
        print(json.dumps(delivery_health(state / "findback.db"), indent=2))
    elif args.command == "retry-delivery":
        if not (state / "findback.db").exists():
            raise SystemExit("No saved delivery queue found. Bootstrap a session first.")
        store = SQLiteStore(state / "findback.db")
        try:
            with store.transaction() as db:
                counts = retry_delivery(db, "runtime_messages" if args.queue == "runtime" else args.queue, args.message_id)
            print(json.dumps({"requeued": counts, "history_preserved": True, "next_attempt_reconciles_markers": True}, indent=2))
        finally:
            store.close()
    elif args.command == "retry-failed":
        store = SQLiteStore(state / "findback.db")
        with store.transaction() as db:
            count = db.execute("UPDATE inbox SET state='PENDING',error=NULL WHERE state='FAILED'").rowcount
        print(f"Requeued {count} interrupted messages. Existing domain actions remain deduplicated.")
    else:
        from .runtime import FindBackRuntime
        config = load_config(state)
        enroll(config, state)
        async def run():
            async with FindBackRuntime(config, state) as runtime:
                await runtime.run_forever()
        try:
            asyncio.run(run())
        except KeyboardInterrupt:
            print("FindBack stopped. State, inbox and outbox are saved.")


if __name__ == "__main__":
    main()
