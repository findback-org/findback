"""Authenticated fictional campus conversation, with real Switch/domain waits.

This driver posts only the scripted human messages. Agent replies are produced
by the separately running ShowcaseRuntime. It never resets state or inserts
domain results, and reconciles its own posts before retrying a step.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from typing import Callable

import httpx


PEOPLE = ("priya", "alex", "mira", "jordan", "ava", "elena", "omar", "maya",
          "noah", "aisha", "leo", "sam")


class StoryError(RuntimeError):
    """A safe failure message that excludes credentials and remote bodies."""


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".showcase-story-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class CampusStory:
    def __init__(self, state_dir: Path, evidence_dir: Path, *, pace: float = 1,
                 max_seconds: float = 600):
        self.state_dir = state_dir.resolve()
        self.evidence_dir = evidence_dir.resolve()
        self.config = json.loads((self.state_dir / "config.json").read_text())
        self.showcase = self.config.get("showcase", {})
        self.people = self.showcase.get("people", {})
        if self.showcase.get("fictional") is not True or any(key not in self.people for key in PEOPLE):
            raise StoryError("The explicitly fictional twelve-person showcase must be provisioned first.")
        if len({self.people[key]["id"] for key in PEOPLE}) != 12:
            raise StoryError("The twelve scripted speakers must be distinct authenticated accounts.")
        self.community = self.showcase["community_room"]["channel_id"]
        self.staff = self.config["staff_room"]["channel_id"]
        self.client = httpx.Client(base_url=self.config["mattermost_url"].rstrip("/"), timeout=30)
        self.pace = max(0, pace)
        self.started = time.monotonic()
        self.deadline = self.started + max_seconds
        self._cancelled = threading.Event()
        self.journal_path = self.state_dir / "showcase-story.json"
        self.journal = json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {
            "schema": 1, "community_channel_id": self.community,
            "started_at": datetime.now(timezone.utc).isoformat(), "steps": {},
            "disclosure": "Twelve scripted fictional people; real authenticated Mattermost posts, Switch transport and models. No physical handover."}
        if self.journal.get("community_channel_id") != self.community:
            raise StoryError("The story journal belongs to a different community room.")

    def close(self):
        self.client.close()

    def stop(self):
        """Cooperatively stop a threaded driver when its real runtime ends."""
        self._cancelled.set()

    def pause(self, seconds: float):
        if self._cancelled.wait(seconds):
            raise StoryError("The live showcase stopped; existing posts and case history were preserved.")

    def row(self, sql: str, args: tuple = ()) -> dict | None:
        # A normal read-only connection includes committed WAL records. No
        # immutable=1 shortcut and no database setup writes are used here.
        uri = (self.state_dir / "findback.db").as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=15) as database:
            database.row_factory = sqlite3.Row
            database.execute("PRAGMA query_only=ON")
            result = database.execute(sql, args).fetchone()
            return dict(result) if result else None

    def wait(self, label: str, predicate: Callable, timeout: float = 210):
        until = min(self.deadline, time.monotonic() + timeout)
        while time.monotonic() < until:
            if self._cancelled.is_set():
                raise StoryError("The live runtime stopped before the expected result; the story did not advance.")
            result = predicate()
            if result:
                return result
            self.pause(.25)
        raise StoryError("Timed out waiting for the actual " + label + ". The story stopped without advancing it.")

    def api(self, person: str, method: str, path: str, data=None):
        if self._cancelled.is_set():
            raise StoryError("The live showcase stopped; no further messages were posted.")
        if time.monotonic() >= self.deadline:
            raise StoryError("The showcase time budget expired; existing posts and case history were preserved.")
        human = self.people[person]
        try:
            response = self.client.request(method, path,
                headers={"Authorization": "Bearer " + human["token"]}, json=data)
        except httpx.HTTPError:
            raise StoryError("A local Mattermost request failed; rerun to reconcile the existing story.") from None
        if response.status_code >= 300:
            raise StoryError(f"Mattermost refused the {person} story step (HTTP {response.status_code}).")
        return response.json() if response.content else None

    def _own_post(self, person: str, channel: str, key: str) -> dict | None:
        for page in range(8):
            batch = self.api(person, "GET", f"/api/v4/channels/{channel}/posts?page={page}&per_page=100")
            posts = batch.get("posts", {})
            for post in posts.values():
                if (post.get("user_id") == self.people[person]["id"] and
                        post.get("channel_id") == channel and
                        post.get("props", {}).get("findback_story") == key):
                    return post
            if len(batch.get("order", [])) < 100:
                return None
        raise StoryError("Post reconciliation exceeded its history bound; inspect the existing room before rerunning.")

    def post(self, label: str, person: str, channel: str, text: str, *, root_id: str | None = None):
        key = "campus-v1:" + self.community + ":" + label
        prior = self.journal["steps"].get(label)
        if prior:
            if (prior["person"], prior["channel_id"], prior["text"], prior.get("root_id")) != (person, channel, text, root_id):
                raise StoryError("A recorded story step changed; use the original script or a fresh showcase.")
            remote = self.api(person, "GET", "/api/v4/posts/" + prior["post_id"])
        else:
            remote = self._own_post(person, channel, key)
            if remote is None:
                self.api(person, "POST", "/api/v4/users/" + self.people[person]["id"] + "/typing",
                         {"channel_id": channel, "parent_id": root_id or ""})
                self.pause(1.15 * self.pace)
                remote = self.api(person, "POST", "/api/v4/posts", {
                    "channel_id": channel, "message": text, "root_id": root_id or "",
                    "props": {"findback_story": key, "fictional_scripted_input": True}})
        if (remote.get("user_id") != self.people[person]["id"] or remote.get("channel_id") != channel or
                remote.get("message") != text or remote.get("root_id", "") != (root_id or "") or
                remote.get("props", {}).get("findback_story") != key):
            raise StoryError("A reconciled story post does not match its authenticated speaker and exact content.")
        source = "\x1f".join([self.config["bridge_id"], channel, remote["id"]])
        event = "sw_" + hashlib.sha256("\x1f".join([self.people[person]["client_id"], source, "0"]).encode()).hexdigest()[:32]
        record = {"person": person, "channel_id": channel, "text": text, "root_id": root_id,
                  "post_id": remote["id"], "switch_event_id": event,
                  "created_at_ms": remote.get("create_at"),
                  "recorded_elapsed_seconds": round(remote["create_at"] / 1000 - datetime.fromisoformat(self.journal["started_at"]).timestamp(), 3)}
        self.journal["steps"][label] = record
        _save(self.journal_path, self.journal)
        if not prior:
            print(f"{label}: {self.people[person]['display_name']}", flush=True)
            self.pause(.7 * self.pace)
        return record

    def inbox(self, post: dict):
        result = self.wait("Switch handling of " + post["person"] + "'s message",
            lambda: self.row("SELECT state,error FROM inbox WHERE event_id=? AND state IN ('DONE','FAILED')",
                             (post["switch_event_id"],)))
        if result["state"] != "DONE":
            raise StoryError("The live runtime failed a scripted message; inspect the local runtime log.")
        return result

    def case(self, claim_id: str):
        return self.row("SELECT * FROM claims WHERE id=?", (claim_id,))

    def private_channel(self, claim_id: str) -> str:
        return self.config["cases"][claim_id]["channel_id"]

    def verify_public_boundary(self) -> dict:
        identifying_answers = ("green triangle", "short blue charging cable", "red thread",
                               "initials rk", "star engraving", "for l, keep exploring")
        seen = set()
        for page in range(8):
            batch = self.api("sam", "GET", f"/api/v4/channels/{self.community}/posts?page={page}&per_page=100")
            for post in batch.get("posts", {}).values():
                if post.get("channel_id") != self.community:
                    raise StoryError("A public-room read returned a post from another room.")
                seen.add(post["id"])
                if any(answer in post.get("message", "").casefold() for answer in identifying_answers):
                    raise StoryError("A private identifying answer appeared in the public community room.")
            if len(batch.get("order", [])) < 100:
                return {"passed": True, "actual_public_posts_checked": len(seen),
                        "private_identifying_answers_found": 0}
        raise StoryError("The public-boundary check exceeded its bounded history window.")

    def fact(self, key: str, value=None):
        facts = self.journal.setdefault("facts", {})
        if key in facts:
            return facts[key]
        if value is None:
            return None
        facts[key] = value
        _save(self.journal_path, self.journal)
        return value

    def card(self, record_id: str, channel: str, person: str) -> dict:
        saved = self.fact("card:" + record_id)
        if saved:
            return saved
        notice = self.wait("delivered agent card", lambda: self.row("""
            SELECT c.message_key, c.kind, c.claim_id, c.record_id,
                   COALESCE(m.remote_id,o.remote_event_id) AS remote_id
              FROM showcase_cards c
              LEFT JOIN runtime_messages m ON m.id=c.message_key
              LEFT JOIN outbox o ON o.id=c.message_key
             WHERE c.record_id=? AND (m.state='SENT' OR o.delivery_state='SENT')
             ORDER BY c.rowid DESC LIMIT 1""", (record_id,)))
        marker = "[findback:" + notice["message_key"] + "]"
        human_ids = {p["id"] for p in self.people.values()}
        human_ids.add(self.showcase["observer"]["id"])

        def lookup():
            for page in range(8):
                batch = self.api(person, "GET", f"/api/v4/channels/{channel}/posts?page={page}&per_page=100")
                for post in batch.get("posts", {}).values():
                    if marker in post.get("message", "") and post.get("channel_id") == channel:
                        if post.get("user_id") in human_ids:
                            raise StoryError("An agent card marker appeared under a human account; no decision was posted.")
                        return {**notice, "mattermost_post_id": post["id"],
                                "mattermost_user_id": post["user_id"], "message": post["message"]}
                if len(batch.get("order", [])) < 100:
                    return None
            raise StoryError("The actual agent card exceeded the bounded Mattermost history lookup.")

        return self.fact("card:" + record_id, self.wait("actual Mattermost agent card", lookup))

    def confirmed(self, claim_id: str, person: str, report: dict, label: str):
        self.inbox(report)
        draft_id = self.fact(label + ":draft")
        if not draft_id:
            claim = self.wait("private Intake draft", lambda: (self.case(claim_id) or {}).get("current_draft_id"))
            draft_id = self.fact(label + ":draft", claim)
        draft_card = self.card(draft_id, self.private_channel(claim_id), person)
        confirmation = self.post(label + "-confirm", person, self.private_channel(claim_id),
                                 "Yes, that's right.", root_id=draft_card["mattermost_post_id"])
        self.inbox(confirmation)
        return self.review_for(claim_id, confirmation, label + ":initial-review")

    def review_for(self, claim_id: str, source: dict, label: str):
        review_id = self.fact(label)
        if not review_id:
            # A real matcher may have requested more information instead of
            # creating a card. After a software correction, ask staff to request
            # a fresh real model review; never invent a card or rebind an old
            # model job to changed inputs. The stable human step is reconciled
            # on restart just like every other authored message.
            processed = self.row("SELECT result_json FROM processed_events WHERE source_event_id=?",
                                 (source["switch_event_id"] + ":matcher",))
            if (label.endswith(":initial-review") and processed and
                    json.loads(processed["result_json"]).get("code") == "INFORMATION_REQUESTED"):
                names = {"C001": "Priya", "C002": "Alex", "C003": "Maya", "C004": "Leo"}
                source = self.post(label + "-retry", "mira", self.staff,
                                   "Review " + names[claim_id] + " again.")
                self.inbox(source)
            review = self.wait("real Matcher review", lambda: self.row(
                "SELECT id FROM reviews WHERE claim_id=? AND source_event_id LIKE ? ORDER BY rowid DESC LIMIT 1",
                (claim_id, source["switch_event_id"] + "%")))
            review_id = self.fact(label, review["id"])
        return self.card(review_id, self.staff, "mira")

    def return_case(self, *, person: str, claim_id: str, alias: str, name: str,
                    evidence: str, reason: str, collection: str):
        private = self.private_channel(claim_id)
        assertion = self.post(person + "-private-evidence", person, private, evidence)
        self.inbox(assertion)
        review = self.review_for(claim_id, assertion, person + ":evidence-review")
        root = review["mattermost_post_id"]
        approval = self.post(person + "-staff-approval", "mira", self.staff,
            f"Approve {alias} for {name}. {reason}", root_id=root)
        self.inbox(approval)
        reservation = self.wait("authorized reservation", lambda: self.row(
            "SELECT * FROM reservations WHERE claim_id=? AND status IN ('ACTIVE','HANDOVER_RECORDED','RETURNED') ORDER BY rowid DESC LIMIT 1",
            (claim_id,)))
        self.fact(person + ":reservation", reservation["id"])
        arranged = self.post(person + "-collection", "mira", self.staff,
            "Collection: " + collection, root_id=root)
        self.inbox(arranged)
        handover = self.post(person + "-handover", "mira", self.staff,
            f"Handed over to {name}. Simulated desk handover for this fictional showcase; no physical object moved.", root_id=root)
        self.inbox(handover)
        self.wait("staff handover record", lambda: self.row(
            "SELECT * FROM returns WHERE reservation_id=? AND staff_handover_event_id IS NOT NULL",
            (reservation["id"],)))
        # Bind the natural receipt to the exact handover it answers. A later
        # reservation must never inherit an old queued "got it back" message.
        handover_card = self.card(reservation["id"], private, person)
        receipt = self.post(person + "-receipt", person, private, "I've got it back, thank you!",
                            root_id=handover_card["mattermost_post_id"])
        self.inbox(receipt)
        returned = self.wait("claimant-confirmed return receipt", lambda: self.row(
            "SELECT * FROM returns WHERE reservation_id=? AND receipt_id IS NOT NULL AND returned_at IS NOT NULL",
            (reservation["id"],)))
        if (self.case(claim_id) or {}).get("status") != "RETURNED":
            raise StoryError("A receipt exists without the expected completed claim state.")
        self.fact(person + ":receipt", returned["receipt_id"])
        return returned

    def run(self) -> dict:
        if self.journal.get("passed"):
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            _save(self.evidence_dir / "showcase-run.json", self.journal)
            print("This actual showcase is already complete; inspect its existing Mattermost conversation.", flush=True)
            return self.journal
        if not {"C001", "C002", "C003", "C004"}.issubset(self.config["cases"]):
            raise StoryError("This four-case story requires the Maya and Leo enrollments as well as Priya and Alex.")
        self.wait("showcase runtime database", lambda: self.row(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='showcase_cards'"))
        if not self.journal["steps"] and self.row("SELECT id FROM claims WHERE revision>0 LIMIT 1"):
            raise StoryError("This fresh story cannot reuse already-advanced cases; it never resets previous history.")

        community = self.community
        opening = self.post("sam-opening", "sam", community,
            "Library desk is staffed now. Lost something? Put a broad description here and FindBack will help privately. Please keep unique marks and contents out of the group. (Today's campus is our pinned fictional demo.)")
        found_a = self.post("jordan-found-case", "jordan", community,
            "Found a black headphone case by Library Desk A. Bringing it to the desk rather than leaving it on the table.")
        self.post("elena-thanks-finder", "elena", community,
            "Thanks, Jordan. Mira is there now — she'll log it and keep the identifying details off this channel.", root_id=found_a["post_id"])
        found_b = self.post("ava-found-case", "ava", community,
            "There's another black headphone case in Study Room 2. I'll bring that over too. They look pretty similar from outside.")
        self.post("noah-queue", "noah", community,
            "I'm by the bike racks and can cover the desk queue for a minute while you carry it over.", root_id=found_b["post_id"])

        registrations = [
            ("inventory-case-a", "Log case A: black headphone case. Found at Library Desk A. Private details: green triangle sticker under lid; short blue charging cable inside.", "I-001"),
            ("inventory-case-b", "Log case B: black headphone case. Found at Library Study Room 2. Private details: red thread on zipper; initials RK inside.", "I-002"),
            ("inventory-bottle", "Log bottle: blue insulated bottle. Found at Library Desk B. Private details: small star engraving under base.", "I-003"),
            ("inventory-book", "Log book: red paperback. Found at cafe window. Private details: handwritten dedication For L, keep exploring on page 3.", "I-004"),
        ]
        for label, text, item_id in registrations:
            entry = self.post(label, "mira", self.staff, text)
            self.inbox(entry)
            self.wait("staff-created inventory item", lambda item=item_id: self.row("SELECT id FROM items WHERE id=?", (item,)))

        priya_public = self.post("priya-lost", "priya", community,
            "I lost my black headphone case in the library this afternoon. I'm supposed to be studying and I'm really worried I've left it behind — could one of those be mine?")
        self.post("aisha-reassures-priya", "aisha", community,
            "Take a breath, Priya. There are two cases, so let the desk check before anyone picks one up.", root_id=priya_public["post_id"])
        alex_public = self.post("alex-lost", "alex", community,
            "I lost a black headphone case somewhere in the library too. I don't remember any identifying marks. I'm late for my bus.", root_id=found_b["post_id"])
        book_thread = self.post("omar-found-book", "omar", community,
            "Separate thing: found a red paperback by the cafe window. It's at the library desk now.")
        self.post("maya-bottle-notice", "maya", community,
            "I lost my blue insulated bottle near the library this morning. Hoping that's made it to the desk as well.")
        self.post("leo-book-notice", "leo", community,
            "I lost a red paperback at the cafe. I can describe something inside privately.", root_id=book_thread["post_id"])
        self.post("mira-community-triage", "mira", community,
            "I've logged the finds. Priya and Alex: please use your separate FindBack chats; a black case alone isn't enough to release either one. Maya and Leo, your reports have private chats too.", root_id=priya_public["post_id"])

        first_review = self.confirmed("C001", "priya", priya_public, "priya")
        alex_review = self.confirmed("C002", "alex", alex_public, "alex")
        self.fact("priya:ambiguous-review", first_review["record_id"])
        request = self.post("alex-staff-needs-info", "mira", self.staff,
            "Ask for more information. Both black cases fit the broad report; we need an independently supplied identifying detail before releasing either.",
            root_id=alex_review["mattermost_post_id"])
        self.inbox(request)
        late = self.post("alex-asks-release", "alex", self.private_channel("C002"),
            "Can you just release it? I'm late for my bus.")
        self.inbox(late)
        self.wait("Alex's unresolved case", lambda: (self.case("C002") or {}).get("status") == "NEEDS_INFO")

        self.post("elena-protects-details", "elena", community,
            "Please don't post stickers, contents or photos of labels here. The owner should be able to describe those privately first.", root_id=opening["post_id"])
        self.return_case(person="priya", claim_id="C001", alias="case A", name="Priya",
            evidence="Mine has a green triangle sticker under the lid and a short blue charging cable inside.",
            reason="Priya independently described the hidden sticker and the cable. I compared her assertion with the desk observations for case A; case B has different identifying details.",
            collection="Priya can collect her reserved case from Library Desk A. Ask for Mira; we will verify the private case at the desk.")
        self.post("priya-community-thanks", "priya", community,
            "Sorted — got my case back. Thank you Jordan, Mira and everyone who helped! Glad we checked the details privately.",
            root_id=priya_public["post_id"])
        self.post("aisha-relieved", "aisha", community,
            "Good! Back to studying. Alex, hope yours turns up too.", root_id=priya_public["post_id"])

        for person, claim_id, label, alias, name, evidence, reason, collection in [
            ("maya", "C003", "maya-bottle-notice", "bottle", "Maya",
             "Mine has a small star engraving under the base.",
             "Maya independently described the small engraving beneath the bottle; I checked it against the staff observation.",
             "Maya can collect her reserved bottle from Library Desk B; ask for Mira."),
            ("leo", "C004", "leo-book-notice", "book", "Leo",
             "Mine has a handwritten dedication saying For L, keep exploring on page 3.",
             "Leo independently supplied the dedication and its page. I compared both with the staff-only observation.",
             "Leo can collect the reserved paperback from the library desk; ask for Mira."),
        ]:
            self.confirmed(claim_id, person, self.journal["steps"][label], person)
            self.return_case(person=person, claim_id=claim_id, alias=alias, name=name,
                             evidence=evidence, reason=reason, collection=collection)
            thanks = ("Bottle reunited, thank you!" if person == "maya" else "That's my book back. Thanks Omar and the desk team!")
            self.post(person + "-community-thanks", person, community, thanks,
                      root_id=self.journal["steps"][label].get("root_id") or self.journal["steps"][label]["post_id"])

        self.post("alex-community-followup", "alex", community,
            "Glad you got yours, Priya. I'll look for an old photo of mine and follow up privately — better than taking the wrong one.", root_id=priya_public["post_id"])
        self.post("ava-still-helping", "ava", community,
            "Thanks for checking carefully. The other case is safe with the desk while its owner is being verified.", root_id=found_b["post_id"])
        self.post("sam-close", "sam", community,
            "Thanks, everyone. Three fictional recoveries are now recorded with staff handovers and claimant receipts. The other black case is still held safely; nobody was given the private identifying answers in this group.", root_id=opening["post_id"])
        self.wait("all durable notices delivered", lambda: not self.row("SELECT id FROM outbox WHERE delivery_state!='SENT' LIMIT 1"), timeout=90)
        self.wait("all agent messages delivered", lambda: not self.row("SELECT id FROM runtime_messages WHERE state!='SENT' LIMIT 1"), timeout=90)
        if {step["person"] for step in self.journal["steps"].values()} != set(PEOPLE):
            raise StoryError("The showcase did not contain actual posts from every enrolled fictional person.")
        returns = self.row("SELECT COUNT(*) AS n FROM returns WHERE returned_at IS NOT NULL AND receipt_id IS NOT NULL")["n"]
        if returns != 3 or self.case("C002")["status"] != "NEEDS_INFO":
            raise StoryError("The live domain outcome differs from the intended three returns and unresolved Alex case.")
        public_boundary = self.verify_public_boundary()
        self.journal.update({"passed": True, "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round((datetime.now(timezone.utc) - datetime.fromisoformat(self.journal["started_at"])).total_seconds(), 3),
            "final_driver_session_seconds": round(time.monotonic() - self.started, 3), "actual_people_count": 12,
            "actual_return_receipts": returns, "alex_status": self.case("C002")["status"],
            "community_url": self.showcase["community_room"]["url"],
            "case_urls": {key: value.get("url") for key, value in self.config["cases"].items()},
            "staff_url": self.config["staff_room"].get("url"), "public_boundary": public_boundary})
        _save(self.journal_path, self.journal)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        _save(self.evidence_dir / "showcase-run.json", self.journal)
        print("Actual Mattermost showcase complete: 12 scripted people, 3 durable receipts, Alex still needs evidence.", flush=True)
        print(self.showcase["community_room"]["url"], flush=True)
        return self.journal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".findback/showcase"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    parser.add_argument("--pace", type=float, default=1, help="Scale typing and message pacing; real model/domain waits are never skipped")
    parser.add_argument("--max-seconds", type=float, default=600)
    parser.add_argument("--scripted-roleplay", action="store_true", required=True,
                        help="Acknowledge these are twelve fictional scripted accounts and simulated handovers")
    args = parser.parse_args()
    story = CampusStory(args.state_dir, args.evidence_dir, pace=args.pace, max_seconds=args.max_seconds)
    try:
        story.run()
    finally:
        story.close()


if __name__ == "__main__":
    main()
