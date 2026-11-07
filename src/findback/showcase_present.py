"""Operator presentation pass for the actual local Mattermost conversation.

Keeps all human posts and authority records untouched. Only our own bot delivery
footers and our own setup notice are formatted; the tour is posted via Switch.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid

import httpx

from .bootstrap import _API, _metadata


def present(state: Path) -> dict:
    config = json.loads((state / "config.json").read_text())
    core = json.loads((state / "provisioning.json").read_text())
    story = json.loads((state / "showcase-story.json").read_text())
    browser_origin = config["showcase"].get("browser_origin", config["mattermost_url"])
    if not story.get("passed"):
        raise RuntimeError("Finish the real showcase before publishing its tour.")
    bridge = _metadata("bridge", bridge_id=config["bridge_id"])["connection_config"]
    mm = _API(config["mattermost_url"])
    try:
        mm.mm_login(bridge["admin_user"], bridge["admin_password"])
        users = mm.json("GET", "/api/v4/users?page=0&per_page=200")
        bot_ids = {u["id"] for u in users if u.get("is_bot") and u["username"].startswith("findback-" + core["run_id"] + "-")}
        if len(bot_ids) != 6:
            raise RuntimeError("Expected six exact showcase bot accounts; no formatting changes made.")
        channels = [config["showcase"]["community_room"]["channel_id"], config["staff_room"]["channel_id"],
                    *(case["channel_id"] for case in config["cases"].values())]
        changed = []
        links_fixed = 0
        marker_footer = re.compile(r"\n\n(\[findback:[^\]\n]+\])\s*$")
        for channel in channels:
            posts = mm.json("GET", f"/api/v4/channels/{channel}/posts?per_page=200")["posts"]
            for post in posts.values():
                if post["user_id"] not in bot_ids:
                    continue
                text = re.sub(r"(?:mattermost|http)://(?:localhost|127\.0\.0\.1):\d+", browser_origin, post["message"])
                if text != post["message"]:
                    links_fixed += 1
                match = marker_footer.search(text)
                if match:
                    text = text[:match.start()] + f'\n\n[\u2060](# "{match[1]}")'
                if text != post["message"]:
                    mm.json("PUT", f"/api/v4/posts/{post['id']}/patch", {"message": text})
                    changed.append(post["id"])
        opening = story["steps"]["sam-opening"]["post_id"]
        start = browser_origin + "/switch/pl/" + opening
        notice = ("### FindBack · a campus that helps you get it back\n"
                  "**12 fictional people. Real chat, Switch orchestration and live AI.**\n\n"
                  "Someone finds it. The owner describes it privately. Mira checks the evidence, authorizes collection, and both sides confirm the return.\n\n"
                  f"▶ **[Start with the campus conversation]({start})**\n\n"
                  "Demo observers can inspect the private rooms; ordinary claimants see only their own. All objects and handovers in this demonstration are fictional.")
        mm.json("PUT", f"/api/v4/posts/{config['showcase']['notice_post_id']}/patch", {"message": notice})
    finally:
        mm.close()
    with sqlite3.connect(state / "findback.db") as db:
        db.row_factory = sqlite3.Row
        cases = {r["id"]: r["status"] for r in db.execute("SELECT id,status FROM claims")}
        if cases != {"C001": "RETURNED", "C002": "NEEDS_INFO", "C003": "RETURNED", "C004": "RETURNED"}:
            raise RuntimeError("The actual case states do not support the completed-demo tour.")
        card = db.execute("""SELECT r.remote_id FROM showcase_cards c JOIN runtime_messages r ON r.id=c.message_key
            JOIN reviews v ON v.id=c.record_id WHERE c.kind='review' AND c.claim_id='C001' AND v.evidence_revision>0
            AND r.state='SENT' ORDER BY v.rowid DESC LIMIT 1""").fetchone()
        key = "showcase-tour:" + core["run_id"]
        existing = db.execute("SELECT remote_id FROM runtime_messages WHERE id=? AND state='SENT'", (key,)).fetchone()
    body = (
        "### Three things back with their people. One case safely held.\n\n"
        "🎧 **Priya’s headphone case — returned.** Her own identifying details helped Mira distinguish two lookalikes.\n"
        "💧 **Maya’s bottle — returned.** A private detail, checked at the desk.\n"
        "📕 **Leo’s book — returned.** The owner described what was inside before the desk disclosed it.\n"
        "⏳ **Alex’s claim — still open.** The other black case stays with Mira while Alex looks for more evidence.\n\n"
        f"**Follow the real chat:** [Campus story]({start}) · [Priya’s private verification]({config['cases']['C001']['url']}) · "
        f"[Mira’s review desk]({config['staff_room']['url']}) · [Alex’s unresolved claim]({config['cases']['C002']['url']})\n\n"
        "Every completed return has Mira’s handover and the claimant’s own receipt. "
        "The people and objects are simulated; the messages, AI calls, authorization checks and saved receipts ran through the app."
    )
    # The service's normal outbox handles posting and marker reconciliation.
    from .storage import SQLiteStore
    from .runtime import RuntimeJournal
    store = SQLiteStore(state / "findback.db")
    RuntimeJournal(store).queue(key, config["showcase"]["community_room"]["id"], body)
    store.close()
    result = {"verified_at": datetime.now(timezone.utc).isoformat(), "human_posts_edited": 0,
              "authority_records_changed": 0, "bot_delivery_footers_formatted": len(changed),
              "bot_posts_with_browser_links_fixed": links_fixed,
              "tour_queued_through_switch": True, "community_url": config["showcase"]["community_room"]["url"],
              "start_url": start, "tour_message_key": key,
              "scope": "Only presentation metadata on our six showcase bots/setup notice. All human messages and domain records preserved."}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".findback/showcase"))
    parser.add_argument("--output", type=Path, default=Path("evidence/showcase-presentation.json"))
    args = parser.parse_args()
    result = present(args.state_dir.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
