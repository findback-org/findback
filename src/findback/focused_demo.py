"""Stage-controlled Alex demonstration over the existing, actual campus chat.

Inspection performs only HTTP GETs and read-only SQLite queries. Explicit
scripted stages use Alex's and Mira's own accounts and wait for real Switch,
model, and domain results. This tool has no handover or receipt operation.
Its journal is separate from the completed twelve-person showcase journal.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from .showcase_story import CampusStory, StoryError, _save


CLAIM_ID = "C002"
ITEM_ID = "I-002"
EVIDENCE = "I remember now. Mine has a red thread on the zipper and the initials RK on the inside label."
APPROVAL = ("Approve case B for Alex. Alex independently described the red thread on the zipper "
            "and the initials RK on the inside label. I checked both against the desk observations for case B.")
COLLECTION = ("Collection: Your headphone case is reserved at the library desk. "
              "Please ask for Mira and show this private recovery chat.")
LABELS = {"evidence": "focused-alex-evidence-v1", "approval": "focused-alex-approval-v1",
          "collection": "focused-alex-collection-v1"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FocusedDemo(CampusStory):
    def __init__(self, state_dir: Path, evidence_dir: Path, *, pace: float = .7,
                 max_seconds: float = 240):
        super().__init__(state_dir, evidence_dir, pace=pace, max_seconds=max_seconds)
        self.original_journal_path = self.state_dir / "showcase-story.json"
        original = json.loads(self.original_journal_path.read_text())
        self.references = {key: original["steps"][key] for key in (
            "ava-found-case", "alex-lost", "alex-staff-needs-info")}
        self.run_id = json.loads((self.state_dir / "provisioning.json").read_text())["run_id"]
        self._verified_bots = {}
        self.journal_path = self.state_dir / "focused-demo.json"
        self.journal = json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {
            "schema": 1, "kind": "focused-alex", "claim_id": CLAIM_ID, "item_id": ITEM_ID,
            "community_channel_id": self.community, "started_at": now(), "steps": {}, "facts": {}, "stages": {},
            "disclosure": "Reuses actual found/claim posts. Any continuation is scripted fictional input through distinct authenticated accounts, with real Switch/model/domain results. Stops before handover or receipt."}
        if (self.journal.get("kind"), self.journal.get("claim_id"), self.journal.get("item_id"),
                self.journal.get("community_channel_id")) != ("focused-alex", CLAIM_ID, ITEM_ID, self.community):
            raise StoryError("The focused journal belongs to a different case or campus; nothing was posted.")

    def link(self, post_id: str) -> str:
        return self.showcase["browser_origin"] + "/switch/pl/" + post_id

    def _public_source(self, label: str, expected_person: str):
        reference = self.references[label]
        post = self.api("alex", "GET", "/api/v4/posts/" + reference["post_id"])
        if (post.get("id") != reference["post_id"] or post.get("channel_id") != self.community or
                post.get("user_id") != self.people[expected_person]["id"] or
                post.get("message") != reference["text"] or post.get("delete_at", 0)):
            raise StoryError("The existing found/claim source post no longer matches its authenticated provenance.")
        return {"id": post["id"], "url": self.link(post["id"]), "author": expected_person,
                "text": post["message"], "root_id": post.get("root_id") or None,
                "created_at_ms": post["create_at"]}

    def _bot(self, role: str, person: str):
        if role not in {"service", "matcher", "intake-alex"}:
            raise StoryError("The focused demo requested an unenrolled bot role.")
        if role not in self._verified_bots:
            username = "findback-" + self.run_id + "-" + role
            account = self.api(person, "GET", "/api/v4/users/username/" + username)
            if account.get("username") != username or account.get("is_bot") is not True or not account.get("id"):
                raise StoryError("The expected current-campus bot identity could not be verified.")
            self._verified_bots[role] = account["id"]
        return self._verified_bots[role]

    def _message(self, key: str, channel: str, person: str, *, expected_role: str):
        """Read an actual delivered bot post; never save a guessed card."""
        marker = "[findback:" + key + "]"
        expected_user_id = self._bot(expected_role, person)
        for page in range(8):
            batch = self.api(person, "GET", f"/api/v4/channels/{channel}/posts?page={page}&per_page=100")
            for post in batch.get("posts", {}).values():
                if marker in post.get("message", "") and post.get("channel_id") == channel:
                    if post.get("user_id") != expected_user_id or post.get("delete_at", 0):
                        raise StoryError("The delivered marker did not belong to the exact expected current-campus bot.")
                    return {"id": post["id"], "url": self.link(post["id"]),
                            "text": post["message"], "created_at_ms": post["create_at"],
                            "root_id": post.get("root_id") or None, "message_key": key}
            if len(batch.get("order", [])) < 100:
                return None
        raise StoryError("The focused notice exceeded the bounded message-history window.")

    def card(self, record_id: str, channel: str, person: str):
        card = super().card(record_id, channel, person)
        post = self.api(person, "GET", "/api/v4/posts/" + card["mattermost_post_id"])
        if (post.get("user_id") != self._bot("matcher", person) or post.get("channel_id") != channel or
                post.get("delete_at", 0) or "[findback:" + card["message_key"] + "]" not in post.get("message", "")):
            raise StoryError("The focus comparison is not the exact Matcher bot's actual delivered card.")
        return card

    def _claim_state(self):
        claim = self.row("SELECT id,status,revision,evidence_revision,selected_item_id FROM claims WHERE id=?", (CLAIM_ID,))
        item = self.row("SELECT id,status,revision FROM items WHERE id=?", (ITEM_ID,))
        reservation = self.row("SELECT id,status,item_id,approval_id,collection_instructions FROM reservations WHERE claim_id=? AND status IN ('ACTIVE','HANDOVER_RECORDED','RETURNED') ORDER BY rowid DESC LIMIT 1", (CLAIM_ID,))
        handover = self.row("SELECT rr.reservation_id,rr.receipt_id,rr.handed_over_at FROM returns rr JOIN reservations rs ON rs.id=rr.reservation_id WHERE rs.claim_id=? LIMIT 1", (CLAIM_ID,))
        return {"claim": claim, "item": item, "reservation": reservation, "handover_or_receipt": handover}

    def inspect(self) -> dict:
        found = self._public_source("ava-found-case", "ava")
        claim = self._public_source("alex-lost", "alex")
        if claim["root_id"] != found["id"]:
            raise StoryError("Alex's actual public claim is not a direct reply beneath Ava's actual found post.")
        welcome = self._message(self.references["alex-lost"]["switch_event_id"] + ":welcome", self.community, "alex", expected_role="service")
        if not welcome or self.config["cases"][CLAIM_ID]["url"] not in welcome["text"]:
            raise StoryError("The actual concierge reply does not link to Alex's enrolled private chat.")
        prompt = self.row("SELECT id FROM outbox WHERE source_event_id=? AND topic='more-information' AND delivery_state='SENT' ORDER BY rowid DESC LIMIT 1",
                          (self.references["alex-staff-needs-info"]["switch_event_id"],))
        private_prompt = self._message(prompt["id"], self.private_channel(CLAIM_ID), "alex", expected_role="intake-alex") if prompt else None
        return {"checked_at": now(), "read_only": True, "found_post": found, "claim_reply": claim,
                "claim_is_direct_reply_to_found_post": True, "concierge_reply": welcome,
                "private_information_request": private_prompt,
                "private_case_url": self.config["cases"][CLAIM_ID]["url"],
                "staff_room_url": self.config["staff_room"]["url"],
                "state": self._claim_state(), "completed_focus_stages": list(self.journal["stages"]),
                "completed_showcase_journal_sha256": hashlib.sha256(self.original_journal_path.read_bytes()).hexdigest(),
                "intended_stop": "APPROVED claim, RESERVED item, private collection instructions delivered; no handover and no receipt.",
                "new_posts_sent_by_inspection": 0, "domain_mutations_by_inspection": 0}

    def _begin(self):
        if not self.journal.get("source_inspection"):
            self.journal["source_inspection"] = self.inspect()
            _save(self.journal_path, self.journal)
        original_hash = hashlib.sha256(self.original_journal_path.read_bytes()).hexdigest()
        if original_hash != self.journal["source_inspection"]["completed_showcase_journal_sha256"]:
            raise StoryError("The preserved completed showcase journal changed; inspect it before continuing.")
        if self._claim_state()["handover_or_receipt"]:
            raise StoryError("This focused capture stops before handover; an existing handover or receipt prevents continuation.")

    def _complete(self, stage: str, result: dict):
        self.journal["stages"][stage] = {"completed_at": now(), **result}
        self.journal["current_state"] = self._claim_state()
        self.journal["completed_showcase_journal_unchanged"] = (
            hashlib.sha256(self.original_journal_path.read_bytes()).hexdigest() ==
            self.journal["source_inspection"]["completed_showcase_journal_sha256"])
        _save(self.journal_path, self.journal)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        _save(self.evidence_dir / "focused-run.json", self.journal)
        return self.journal["stages"][stage]

    def _prior_stage(self, name: str):
        result = self.journal["stages"].get(name)
        if not result:
            raise StoryError("Complete the explicit " + name + " stage first; no earlier stage is posted automatically.")
        return result

    def _mutation_result(self, post: dict, expected_code: str):
        recorded = self.row("SELECT result_json FROM processed_events WHERE source_event_id=?",
                            (post["switch_event_id"],))
        result = json.loads(recorded["result_json"]) if recorded else None
        if not result or not result.get("ok") or result.get("code") != expected_code:
            code = result.get("code", "NO_RECORDED_ACTION") if result else "NO_RECORDED_ACTION"
            raise StoryError("The actual focused action did not succeed (" + code + "); capture stopped at that result.")
        return result

    def evidence(self):
        self._begin()
        if LABELS["evidence"] not in self.journal["steps"]:
            # The server can have accepted the assertion immediately before a
            # crash that prevented the local post journal from being saved.
            # Reconcile that authenticated, marked post before applying the
            # fresh-case guard, so its real domain transition is not mistaken
            # for unrelated new evidence.
            existing = self._own_post("alex", self.private_channel(CLAIM_ID),
                "campus-v1:" + self.community + ":" + LABELS["evidence"])
            if existing is None:
                state = self._claim_state()
                if (state["claim"]["status"] != "NEEDS_INFO" or state["claim"]["evidence_revision"] != 0 or
                        state["item"]["status"] != "AVAILABLE" or state["reservation"]):
                    raise StoryError("Alex's inspected initial case or the available case B changed before capture; nothing was posted.")
        assertion = self.post(LABELS["evidence"], "alex", self.private_channel(CLAIM_ID), EVIDENCE)
        self.inbox(assertion)
        self._mutation_result(assertion, "EVIDENCE_RECORDED")
        card = self.review_for(CLAIM_ID, assertion, "focused-alex-evidence-review")
        return self._complete("evidence", {"assertion_post_id": assertion["post_id"],
            "assertion_url": self.link(assertion["post_id"]), "source_event_id": assertion["switch_event_id"],
            "review_id": card["record_id"], "review_post_id": card["mattermost_post_id"],
            "review_url": self.link(card["mattermost_post_id"])})

    def _focus_review(self):
        evidence = self._prior_stage("evidence")
        review = self.row("SELECT id,claim_id,source_event_id FROM reviews WHERE id=?", (evidence["review_id"],))
        if (not review or review["claim_id"] != CLAIM_ID or
                review["source_event_id"] != evidence["source_event_id"] + ":matcher"):
            raise StoryError("The focus review is not bound to this exact private assertion source.")
        card = self.card(review["id"], self.staff, "mira")
        if card["mattermost_post_id"] != evidence["review_post_id"]:
            raise StoryError("The focused comparison card changed; no approval was posted.")
        return evidence, card

    def approval(self):
        self._begin()
        evidence, card = self._focus_review()
        action = self.post(LABELS["approval"], "mira", self.staff, APPROVAL,
                           root_id=card["mattermost_post_id"])
        self.inbox(action)
        self._mutation_result(action, "RESERVED")
        reservation = self.wait("Alex's exact authorized reservation", lambda: self.row("""
            SELECT rs.id,rs.status,rs.item_id FROM reservations rs JOIN approvals a ON a.id=rs.approval_id
             WHERE rs.claim_id=? AND rs.item_id=? AND a.review_id=? AND a.source_event_id=?
             ORDER BY rs.rowid DESC LIMIT 1""", (CLAIM_ID, ITEM_ID, evidence["review_id"], action["switch_event_id"])))
        if reservation["status"] != "ACTIVE":
            raise StoryError("The focus reservation has advanced beyond the intended collection-only state.")
        notice = self.wait("delivered private reservation notice", lambda: self.row(
            "SELECT id FROM outbox WHERE source_event_id=? AND topic='reserved' AND delivery_state='SENT' LIMIT 1",
            (action["switch_event_id"],)))
        delivered = self.wait("actual private reservation message", lambda: self._message(notice["id"], self.private_channel(CLAIM_ID), "alex", expected_role="service"))
        return self._complete("approval", {"approval_post_id": action["post_id"], "approval_url": self.link(action["post_id"]),
            "reservation_id": reservation["id"], "review_id": evidence["review_id"],
            "private_reservation_notice": delivered})

    def collection(self):
        self._begin()
        approved = self._prior_stage("approval")
        evidence, card = self._focus_review()
        state = self._claim_state()
        if (not state["reservation"] or state["reservation"]["id"] != approved["reservation_id"] or
                state["reservation"]["status"] != "ACTIVE"):
            raise StoryError("The approved focus reservation changed; no collection message was posted.")
        action = self.post(LABELS["collection"], "mira", self.staff, COLLECTION,
                           root_id=card["mattermost_post_id"])
        self.inbox(action)
        self._mutation_result(action, "COLLECTION_ARRANGED")
        notice = self.wait("delivered private collection notice", lambda: self.row(
            "SELECT id,payload_json FROM outbox WHERE source_event_id=? AND topic='collection-arranged' AND delivery_state='SENT' LIMIT 1",
            (action["switch_event_id"],)))
        if json.loads(notice["payload_json"]).get("reservation_id") != approved["reservation_id"]:
            raise StoryError("The collection notification belongs to a different reservation.")
        delivered = self.wait("actual private collection message", lambda: self._message(notice["id"], self.private_channel(CLAIM_ID), "alex", expected_role="service"))
        state = self._claim_state()
        if (state["claim"]["status"] != "APPROVED" or state["item"]["status"] != "RESERVED" or
                state["handover_or_receipt"] is not None):
            raise StoryError("The actual case state differs from the intended reserved-for-collection ending.")
        return self._complete("collection", {"collection_post_id": action["post_id"], "collection_url": self.link(action["post_id"]),
            "reservation_id": approved["reservation_id"], "review_id": evidence["review_id"],
            "private_collection_notice": delivered, "public_boundary": self.verify_public_boundary(),
            "claim_status": "APPROVED", "item_status": "RESERVED", "handover_recorded": False, "receipt_recorded": False})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--inspect", action="store_true", help="Read actual posts/case state without posting or creating a journal")
    action.add_argument("--stage", choices=("evidence", "approval", "collection"), help="Run only this explicit stage; never implies another stage")
    parser.add_argument("--state-dir", type=Path, default=Path(".findback/live-campus"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence/focused"))
    parser.add_argument("--output", type=Path, help="Optional credential-free inspection/stage result artifact")
    parser.add_argument("--pace", type=float, default=.7)
    parser.add_argument("--max-seconds", type=float, default=240)
    parser.add_argument("--scripted-roleplay", action="store_true", help="Required before any participant posts; inputs and objects are fictional")
    args = parser.parse_args()
    if args.stage and not args.scripted_roleplay:
        parser.error("An explicit stage requires --scripted-roleplay. Inspection never posts.")
    if args.pace < 0 or args.max_seconds <= 0:
        parser.error("Pace must be nonnegative and max-seconds must be positive.")
    demo = FocusedDemo(args.state_dir, args.evidence_dir, pace=args.pace, max_seconds=args.max_seconds)
    try:
        result = demo.inspect() if args.inspect else getattr(demo, args.stage)()
        if args.output:
            _save(args.output.resolve(), result)
        print(json.dumps(result, indent=2))
    except StoryError as error:
        parser.exit(1, str(error) + "\n")
    finally:
        demo.close()


if __name__ == "__main__":
    main()
