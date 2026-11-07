"""Live, conversational Mattermost showcase over FindBack's real authority.

People are enrolled fictional accounts. All bot posts travel through Switch.
Natural human decisions are anchored to the exact displayed review thread.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

from .domain import ActorContext
from .runtime import FindBackRuntime, GENERIC_QUESTION
from .storage import dumps, loads


ITEMS = {
    "case a": ("I-001", "headphone_case", "black", "black headphone case"),
    "case b": ("I-002", "headphone_case", "black", "black headphone case"),
    "bottle": ("I-003", "bottle", "blue", "blue insulated bottle"),
    "book": ("I-004", "book", "red", "red paperback"),
}
CASE_NAMES = {"C001": "Priya", "C002": "Alex", "C003": "Maya", "C004": "Leo"}
CASE_ITEMS = {"C001": "black headphone case", "C002": "black headphone case", "C003": "blue bottle", "C004": "red book"}
LOG_ITEM = re.compile(r"^Log (case A|case B|bottle|book): ([^.]+)\. Found at ([^.]+)\. Private details: (.+)$", re.DOTALL)
APPROVE = re.compile(r"^Approve (case A|case B|bottle|book) for (Priya|Alex|Maya|Leo)\. (.+)$", re.DOTALL)
HANDOVER = re.compile(r"^Handed over to (Priya|Alex|Maya|Leo)\. (.+)$", re.DOTALL)
CONFIRMATIONS = {"Yes, that's right.", "Yes, that’s right.", "That's right.", "That’s right."}
RECEIPTS = {"I've got it back, thank you!", "I’ve got it back, thank you!"}


class ShowcaseRuntime(FindBackRuntime):
    def __init__(self, config: dict, state_dir: Path, *, model=None):
        config = dict(config)
        self.community = config["showcase"]["community_room"]
        config["additional_service_rooms"] = [self.community["id"]]
        config["compact_delivery_markers"] = True
        super().__init__(config, state_dir, model=model)
        self.people = config["showcase"]["people"]
        with self.store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS showcase_cards (
              message_key TEXT PRIMARY KEY, kind TEXT NOT NULL,
              claim_id TEXT NOT NULL, record_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS showcase_public_threads (
              claim_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL);
            """)
        self.store.configure_enrollment(
            staff_room_id=self.staff_room, staff_actor_ids=[config["staff"]["actor_id"]],
            matcher_agent_ids=[config["agents"]["matcher"]["actor_id"]],
            cases=[{"claim_id": cid, "claimant_actor_id": case["claimant_actor_id"],
                    "case_room_id": case["room_id"], "intake_agent_ids": [case["intake_agent"]["actor_id"]]}
                   for cid, case in self.cases.items()])
        with self.store.transaction() as db:
            db.execute("INSERT INTO settings VALUES ('report_ingress_rooms',?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                       (dumps({cid: [self.community["id"]] for cid in self.cases}),))
            for notice in db.execute("SELECT id,payload_json FROM outbox WHERE topic='handover-recorded'").fetchall():
                payload = loads(notice["payload_json"])
                db.execute("INSERT OR IGNORE INTO showcase_cards VALUES (?,?,?,?)",
                           (notice["id"], "receipt", payload["claim_id"], payload["reservation_id"]))

    def _card(self, key, kind, cid, record_id):
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO showcase_cards VALUES (?,?,?,?)", (key, kind, cid, record_id))

    def card_for_thread(self, thread_id, kind):
        if not thread_id:
            return None
        with self.store.connection() as db:
            row = db.execute("""SELECT c.* FROM showcase_cards c
              LEFT JOIN runtime_messages r ON r.id=c.message_key
              LEFT JOIN outbox o ON o.id=c.message_key
              WHERE c.kind=? AND (r.remote_id=? OR o.remote_event_id=?)""", (kind, thread_id, thread_id)).fetchone()
            return dict(row) if row else None

    def public_update(self, key, cid, message):
        with self.store.connection() as db:
            row = db.execute("SELECT thread_id FROM showcase_public_threads WHERE claim_id=?", (cid,)).fetchone()
        self.journal.queue(key, self.community["id"], message, thread_id=row[0] if row else None)

    async def _verified_context(self, frame):
        if frame["data"].get("room_id") != self.community["id"]:
            return await super()._verified_context(frame)
        payload = frame["data"].get("payload", {})
        if payload.get("on_behalf_of") or payload.get("sender_kind") in {"agent", "platform"}:
            return None
        human = next((p for p in self.people.values() if p["actor_id"] == payload.get("sender")), None)
        if human is None:
            return None
        response = await self.http.get(self.config["mattermost_url"] + f"/api/v4/channels/{self.community['channel_id']}/posts",
                                       params={"per_page": 200}, headers={"Authorization": "Bearer " + human["token"]})
        if response.status_code != 200:
            return None
        for post in response.json().get("posts", {}).values():
            material = "\x1f".join([human["client_id"], self.config["bridge_id"], self.community["channel_id"], post["id"], "0"])
            expected = "sw_" + hashlib.sha256(material.encode()).hexdigest()[:32]
            if expected != payload.get("message_id"):
                continue
            if post["user_id"] != human["id"] or post["channel_id"] != self.community["channel_id"] or post["message"] != payload.get("body"):
                return None
            if post.get("type") or post.get("props", {}).get("from_webhook") or post.get("props", {}).get("from_bot"):
                return None
            return ActorContext(human["actor_id"], "human", self.community["id"], expected, post["message"],
                                datetime.fromtimestamp(post["create_at"] / 1000, timezone.utc).isoformat(),
                                "switch", payload.get("thread_id"))
        return None

    async def _review(self, source, claim_id):
        result = await super()._review(source, claim_id)
        if not result["ok"] or "review_id" not in result["data"]:
            return result
        key = source.source_event_id + ":matcher:review"
        data = result["data"]
        comparison = data["comparison"]
        with self.store.connection() as db:
            row = db.execute("SELECT proposed_analysis_json FROM reviews WHERE id=?", (data["review_id"],)).fetchone()
        analysis = loads(row[0], {})
        name = CASE_NAMES[claim_id]
        lines = [f"### {name} · evidence ready for your judgment", "**Private staff comparison · AI proposal, human decision**", analysis.get("summary", "Please compare the recorded observations and claimant assertions.")]
        lines.append("**What the desk recorded**")
        for item in comparison["items"]:
            alias = next(alias for alias, value in ITEMS.items() if value[0] == item["id"])
            lines.append(f"- **{alias.title()}** — " + "; ".join(item["private_facts"]))
        lines.append("**What the claimant independently said**")
        for evidence in comparison["claimant_assertions"]:
            lines.append("> " + evidence["text"].replace("\n", "\n> "))
        if not comparison["claimant_assertions"]:
            lines.append("No distinguishing details yet. A broad match is not ownership evidence.")
        for label, field in (("Agrees", "agreements"), ("Doesn't agree", "contradictions"), ("Still needed", "missing_information")):
            values = analysis.get(field, [])
            if values:
                lines.append(f"**{label}:** " + "; ".join(values))
        aliases = [next(alias for alias, value in ITEMS.items() if value[0] == item["id"]) for item in comparison["items"]]
        hint = aliases[0] if len(aliases) == 1 else "<case A or case B>"
        lines.append("**Mira: reply in this card's thread.** If the evidence is incomplete, use `Ask for more information. <what's missing>`. "
                     f"If you judge it sufficient, use `Approve {hint} for {name}. <your reason>`.")
        lines.append("Approval reserves the reviewed item. Collection and confirmed handover come next.")
        self._card(key, "review", claim_id, data["review_id"])
        with self.store.transaction() as db:
            db.execute("UPDATE runtime_messages SET body=? WHERE id=? AND state='PENDING'", ("\n\n".join(lines), key))
        return result

    async def handle(self, frame):
        payload = frame["data"].get("payload", {})
        ctx = await self._verified_context(frame)
        if ctx is None:
            return
        self.store.ingest_message(ctx)
        text = ctx.source_text.strip()
        if ctx.room_id == self.community["id"]:
            cid = next((cid for cid, case in self.cases.items() if case["claimant_actor_id"] == ctx.actor_id), None)
            if cid and re.match(r"^(I lost\b|I'm missing\b|Has anyone seen\b)", text, re.I):
                with self.store.transaction() as db:
                    db.execute("INSERT INTO showcase_public_threads VALUES (?,?) ON CONFLICT(claim_id) DO NOTHING", (cid, ctx.thread_id or ctx.source_event_id))
                name = CASE_NAMES[cid]
                self.public_update(ctx.source_event_id + ":welcome", cid,
                    f"{name}, I can help. I’ve opened [your private recovery chat]({self.cases[cid]['url']}). I’ll ask you there, so the identifying details stay between you and the library desk. Everyone else can keep posting here.")
                report = await self._proposal(ctx.source_event_id + ":draft", "intake", {"text": text})
                actor = self._agent_context(cid, ctx, self.cases[cid]["room_id"])
                self.domain.save_report_draft(actor, claim_id=cid, report=report, source_event_ids=[ctx.source_event_id])
            elif re.match(r"^(Found[:! ]|I found\b|I’ve found\b|I've found\b)", text, re.I):
                self.journal.queue(ctx.source_event_id + ":found-ack", self.community["id"],
                    "Thank you for stopping to help. Please hand it to Mira at the library desk. We’ll log custody there; anyone who thinks it is theirs can describe what they lost here. Keep identifying marks and contents out of this channel.",
                    thread_id=ctx.thread_id or ctx.source_event_id)
            return

        if ctx.room_id == self.staff_room:
            review_request = re.fullmatch(r"Review (Priya|Alex|Maya|Leo) again\.", text)
            if review_request:
                cid = next(cid for cid, name in CASE_NAMES.items() if name == review_request[1])
                permitted = self.domain.staff_comparison(ctx, claim_id=cid)
                if permitted["ok"]:
                    await self._review(ctx, cid)
                else:
                    self._refusal(ctx, permitted)
                return
            registration = LOG_ITEM.fullmatch(text)
            if registration:
                alias, description, place, facts = registration.groups()
                item_id, category, color, expected = ITEMS[alias.lower()]
                if description.lower() != expected:
                    self.journal.queue(ctx.source_event_id + ":invalid-item", ctx.room_id, "Please clarify the item's category and color before logging it.")
                    return
                result = self.domain.register_item(ctx, item_id=item_id,
                    coarse={"category": category, "color": color, "brand_model": None, "found_place": place,
                            "found_at": datetime.now(timezone.utc).isoformat()},
                    private_facts=[f.strip().rstrip(".") for f in facts.split(";")])
                if result["ok"]:
                    self.journal.queue(ctx.source_event_id + ":logged", ctx.room_id,
                        f"**{alias.title()} is logged in desk custody.** Its identifying details stay in this room. I’ll compare incoming reports against this record.")
                else:
                    self._refusal(ctx, result)
                return
            card = self.card_for_thread(ctx.thread_id, "review")
            action = APPROVE.fullmatch(text)
            if action or text.startswith(("Collection: ", "Handed over to ", "Ask for more information. ", "Cancel this reservation. ")):
                if card is None:
                    self.journal.queue(ctx.source_event_id + ":need-card", ctx.room_id,
                        "Please reply inside the comparison card’s thread. That keeps your decision attached to exactly the evidence you reviewed.")
                    return
                cid = card["claim_id"]
                if action:
                    alias, name, reason = action.groups()
                    if name != CASE_NAMES[cid]:
                        self.journal.queue(ctx.source_event_id + ":wrong-person", ctx.room_id, "The named person does not belong to this comparison. Please use their own card.")
                        return
                    result = self.domain.approve(ctx, claim_id=cid, item_id=ITEMS[alias.lower()][0], review_id=card["record_id"], reason=reason)
                elif text.startswith("Ask for more information. "):
                    result = self.domain.request_information(ctx, claim_id=cid, reason=text.removeprefix("Ask for more information. "))
                else:
                    view = self.domain.case_view(ctx, claim_id=cid)
                    reservation = view.get("data", {}).get("reservation")
                    if not reservation:
                        self.journal.queue(ctx.source_event_id + ":no-reservation", ctx.room_id, "This case has no active reservation. Review and approve it first.")
                        return
                    # The action thread must be the review that authorized this
                    # reservation, never a newer or unrelated card.
                    with self.store.connection() as db:
                        approval = db.execute("SELECT review_id FROM approvals WHERE id=(SELECT approval_id FROM reservations WHERE id=?)", (reservation["id"],)).fetchone()
                    if not approval or approval[0] != card["record_id"]:
                        self.journal.queue(ctx.source_event_id + ":wrong-reservation-card", ctx.room_id, "Please use the comparison thread that authorized this reservation.")
                        return
                    if text.startswith("Collection: "):
                        result = self.domain.arrange_collection(ctx, claim_id=cid, reservation_id=reservation["id"], instructions=text.removeprefix("Collection: "))
                    elif text.startswith("Cancel this reservation. "):
                        result = self.domain.cancel_reservation(ctx, claim_id=cid, reservation_id=reservation["id"], reason=text.removeprefix("Cancel this reservation. "))
                    else:
                        handover = HANDOVER.fullmatch(text)
                        if not handover or handover[1] != CASE_NAMES[cid]:
                            self.journal.queue(ctx.source_event_id + ":wrong-handover", ctx.room_id, "Please name the claimant attached to this reservation.")
                            return
                        result = self.domain.record_handover(ctx, claim_id=cid, reservation_id=reservation["id"], note=handover[2])
                if result["ok"]:
                    phrase = {"RESERVED": f"Reserved for {CASE_NAMES[cid]}. I’ve told them privately; please arrange collection in this thread.",
                              "INFORMATION_REQUESTED": "I’ve asked privately for more detail. Nothing has been released.",
                              "COLLECTION_ARRANGED": "Collection instructions delivered privately. The return is still open.",
                              "HANDOVER_RECORDED": "Handover recorded. I’m waiting for the claimant’s own receipt confirmation.",
                              "RESERVATION_CANCELLED": "Reservation cancelled. A fresh comparison is needed."}.get(result["code"], result["message"])
                    self.journal.queue(ctx.source_event_id + ":decision", ctx.room_id, phrase, thread_id=ctx.thread_id)
                else:
                    self._refusal(ctx, result)
            return

        cid = self.rooms_to_cases.get(ctx.room_id)
        if not cid or ctx.actor_id != self.cases[cid]["claimant_actor_id"] or ctx.actor_kind != "human":
            return
        if text in CONFIRMATIONS:
            card = self.card_for_thread(ctx.thread_id, "draft")
            if not card or card["claim_id"] != cid:
                self.journal.queue(ctx.source_event_id + ":confirm-thread", ctx.room_id, "Please reply to your report draft so I know exactly which version you checked.", "intake-" + cid)
                return
            result = self.domain.confirm_report(ctx, claim_id=cid, draft_id=card["record_id"])
            if result["ok"]:
                await self._review(ctx, cid)
            else:
                self._refusal(ctx, result)
        elif text in RECEIPTS:
            card = self.card_for_thread(ctx.thread_id, "receipt")
            if not card or card["claim_id"] != cid:
                self.journal.queue(ctx.source_event_id + ":no-receipt", ctx.room_id,
                    "Please reply to Mira’s handover confirmation message, so your receipt closes that exact collection.", "intake-" + cid)
                return
            result = self.domain.confirm_receipt(ctx, claim_id=cid, reservation_id=card["record_id"])
            if not result["ok"]:
                self._refusal(ctx, result)
        elif re.match(r"^(Mine\b|It has\b|There's\b|There is\b|The inside\b|The dedication\b|I remember\b)", text, re.I):
            result = self.domain.record_evidence(ctx, claim_id=cid, text=text)
            if result["ok"]:
                await self._review(ctx, cid)
            else:
                self._refusal(ctx, result)
        elif text.startswith("Can you just release it?"):
            self.journal.queue(ctx.source_event_id + ":insufficient", ctx.room_id,
                "I know you’re in a hurry. Two people can own a black case, so color alone is not enough for Mira to release one. " + GENERIC_QUESTION + " Your claim will stay open while you remember or find more evidence.", "intake-" + cid)

    def _refusal(self, ctx, result):
        message = {"STALE_REVISION": "The evidence changed after that comparison. Please use the newest card before deciding.",
                   "FORBIDDEN": "Only Mira’s enrolled desk account can authorize release, from the private review room.",
                   "HANDOVER_REQUIRED": "Mira has not recorded handover yet. I’ll ask you to confirm after that happens.",
                   "RESERVATION_ACTIVE": "This item is reserved. Mira must cancel the reservation before the reviewed facts can change."}.get(result["code"], result["message"])
        self.journal.queue(ctx.source_event_id + ":refusal", ctx.room_id, message, thread_id=ctx.thread_id)

    def _notice(self, row):
        """A fixed audience/field allowlist; no model-written public summaries."""
        payload, topic = row["payload"], row["topic"]
        cid = payload.get("claim_id")
        name = CASE_NAMES.get(cid, "The claimant")
        speaker = "service"
        if topic == "report-draft":
            fields = payload["fields"]
            body = (f"### {name}, let’s get your {CASE_ITEMS[cid]} back\n\n"
                    "I’ve brought your report over from the campus channel. Here’s what I understood:\n\n" +
                    "\n".join(f"- **{label}:** {fields[key]}" for key, label in (("category", "Item"), ("color", "Color"), ("lost_place", "Where"), ("description", "Your description")) if fields.get(key)) +
                    "\n\n**Is that right?** Reply in this message’s thread: `Yes, that's right.`\n\nI won’t show you the desk’s identifying details. I’ll ask you to describe yours in your own words.")
            self._card(row["id"], "draft", cid, payload["draft_id"])
            return body, "intake-" + cid
        if topic == "report-confirmed":
            return "Thanks, that’s confirmed. " + GENERIC_QUESTION + " I’ll compare your report with what the desk holds while you think.", "intake-" + cid
        if topic == "staff-report":
            return f"**New recovery request · {name}**\nTheir report is confirmed. Matcher is preparing a comparison; please wait for the evidence card before deciding.", "service"
        if topic == "evidence-recorded":
            return "Got it. Your exact words are saved as your assertion, and I’m sending the desk a fresh comparison. Only Mira can decide whether the evidence is enough.", "intake-" + cid
        if topic == "staff-evidence":
            return f"**{name} supplied identifying details privately.** Matcher is checking the updated evidence now.", "service"
        if topic == "more-information":
            return "Mira needs a little more detail before releasing anything. " + GENERIC_QUESTION + " Your claim stays open.", "intake-" + cid
        if topic == "staff-more-information":
            return f"**{name}'s claim stays open.** I asked privately for more identifying evidence; the desk keeps custody until you decide.", "service"
        if topic == "reserved":
            return f"**Mira approved a possible match and reserved it for you.**\nYour {CASE_ITEMS[cid]} is waiting at the desk. I’ll send collection details next; this is not marked returned yet.", "service"
        if topic == "staff-reserved":
            return f"**{name}'s item is reserved.** Another claim cannot reserve it while this handover is pending.", "service"
        if topic == "collection-arranged":
            # This field was explicitly authored for this claimant by staff.
            with self.store.connection() as db:
                reservation = db.execute("SELECT collection_instructions FROM reservations WHERE id=?", (payload["reservation_id"],)).fetchone()
            return "**Collection arranged**\n\n" + reservation[0] + "\n\nMira will record handover, then I’ll ask you to confirm you actually received it.", "service"
        if topic == "handover-recorded":
            self._card(row["id"], "receipt", cid, payload["reservation_id"])
            return "Mira has recorded handing it to you. **Did you receive your item?**\nReply in this message’s thread: `I've got it back, thank you!` only if you have it.", "service"
        if topic == "returned":
            self.public_update(row["id"] + ":public", cid,
                f"✅ **Back with {name}.** The {CASE_ITEMS[cid]} was returned after a private identifying check, Mira’s approval, desk handover, and {name}’s own receipt confirmation. Thank you to everyone who helped.")
            return f"### Back with you. Case closed.\n\n{ name }, your receipt is saved. Mira’s handover and your confirmation are both recorded.\n\n**Receipt:** `{payload['receipt_id']}`\n\nI’m glad we found the right one. 💚", "service"
        if topic == "staff-returned":
            return f"✅ **{name} confirmed receipt.** The item is now returned; the reservation is closed and the audit receipt is saved.", "service"
        if topic == "reservation-cancelled":
            return "Mira cancelled the collection reservation. Please wait for a fresh review before collecting.", "service"
        return payload.get("text", "Your case was updated."), speaker

    async def _deliver(self):
        while not self._stop.is_set():
            for row in self.store.pending_outbox():
                try:
                    if row["topic"] == "review-ready":
                        key = row["source_event_id"] + ":review"
                        with self.store.connection() as db:
                            displayed = db.execute("SELECT remote_id FROM runtime_messages WHERE id=? AND state='SENT'", (key,)).fetchone()
                        if displayed:
                            self.store.mark_outbox_sent(row["id"], displayed[0])
                        continue
                    body, speaker = self._notice(row)
                    remote = await self._send_once(row["id"], row["destination_room_id"], body, speaker)
                    self.store.mark_outbox_sent(row["id"], remote)
                except Exception as exc:
                    self.store.mark_outbox_failed(row["id"], type(exc).__name__)
            with self.store.connection() as db:
                rows = [dict(r) for r in db.execute("SELECT * FROM runtime_messages WHERE state='PENDING' ORDER BY rowid")]
            for row in rows:
                try:
                    remote = await self._send_once(row["id"], row["room_id"], row["body"], row["speaker"], row.get("thread_id"))
                    with self.store.transaction() as db:
                        db.execute("UPDATE runtime_messages SET state='SENT',remote_id=? WHERE id=?", (remote, row["id"]))
                except Exception as exc:
                    # Keep pending. The next attempt reconciles the trusted marker.
                    print(f"Showcase delivery pending ({type(exc).__name__}).", flush=True)
            await asyncio.sleep(0.4)

    async def run_forever(self):
        print("FindBack showcase is live: 12 campus voices, four private cases, and one staff review desk.", flush=True)
        done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".findback/showcase"))
    args = parser.parse_args()
    state = args.state_dir.resolve()
    config = json.loads((state / "config.json").read_text())
    async def run():
        async with ShowcaseRuntime(config, state) as runtime:
            await runtime.run_forever()
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Showcase paused; all progress is saved.")


if __name__ == "__main__":
    main()
