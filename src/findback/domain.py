"""FindBack's durable authority. Models propose; enrolled humans decide and confirm."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import sqlite3
from typing import Any, Callable
from uuid import uuid4

from .storage import SQLiteStore, dumps, loads, utcnow
from .matching import VERIFICATION_QUESTION, coarse_candidates


@dataclass(frozen=True)
class ActorContext:
    """Internal trusted envelope. Never accept this object from model arguments."""
    actor_id: str
    actor_kind: str
    room_id: str
    source_event_id: str
    source_text: str = ""
    occurred_at: str = ""
    origin: str = "fixture"
    thread_id: str | None = None


class Refusal(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


def identifier(prefix: str) -> str:
    return prefix + uuid4().hex[:16]


def result(ok: bool, code: str, message: str, data: dict | None = None,
           audit_id: str | None = None, replayed: bool = False) -> dict:
    return {"ok": ok, "code": code, "message": message, "data": data or {},
            "audit_id": audit_id, "replayed": replayed}


class DomainService:
    def __init__(self, store: SQLiteStore, policy: Any = None):
        self.store = store
        # Policy is reserved for an additional trusted restriction. Enrollment is always enforced.
        self.policy = policy

    def _known(self, db, ctx):
        row = db.execute("SELECT actor_kind FROM principals WHERE actor_id=?", (ctx.actor_id,)).fetchone()
        if not row or row[0] != ctx.actor_kind or ctx.actor_kind not in ("human", "agent"):
            raise Refusal("FORBIDDEN", "This action is not authorized from this account and room.")

    def _authorize(self, db, ctx, permission: str, claim_id: str | None = None) -> str:
        self._known(db, ctx)
        staff_room = self.store.setting(db, "staff_room_id")
        staff = ctx.actor_kind == "human" and ctx.actor_id in self.store.setting(db, "staff_actor_ids", []) and ctx.room_id == staff_room
        matcher = ctx.actor_kind == "agent" and ctx.actor_id in self.store.setting(db, "matcher_agent_ids", []) and ctx.room_id == staff_room
        binding = db.execute("SELECT * FROM case_bindings WHERE claim_id=?", (claim_id,)).fetchone() if claim_id else None
        claimant = bool(binding and ctx.actor_kind == "human" and ctx.actor_id == binding["claimant_actor_id"] and ctx.room_id == binding["case_room_id"])
        intake = bool(binding and ctx.actor_kind == "agent" and ctx.actor_id in loads(binding["intake_agent_ids_json"], []) and ctx.room_id == binding["case_room_id"])
        allowed = {"staff": staff, "staff_or_matcher": staff or matcher,
                   "claimant": claimant, "intake": claimant or intake,
                   "case": staff or matcher or claimant or intake}.get(permission, False)
        if not allowed:
            raise Refusal("FORBIDDEN", "This action is not authorized from this account and room.")
        if self.policy is not None and hasattr(self.policy, "allows") and not self.policy.allows(ctx, permission, claim_id):
            raise Refusal("FORBIDDEN", "This action is not authorized from this account and room.")
        return "staff" if staff else "matcher" if matcher else "claimant" if claimant else "intake"

    @staticmethod
    def _claim(db, claim_id):
        row = db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise Refusal("NOT_FOUND", "Case not found.")
        return row

    @staticmethod
    def _item(db, item_id):
        row = db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise Refusal("NOT_FOUND", "Item not found.")
        return row

    @staticmethod
    def _active(db, claim_id=None, item_id=None):
        column, key = ("claim_id", claim_id) if claim_id else ("item_id", item_id)
        return db.execute(f"SELECT * FROM reservations WHERE {column}=? AND status IN ('ACTIVE','HANDOVER_RECORDED')", (key,)).fetchone()

    def _unfrozen_claim(self, db, claim):
        if self._active(db, claim_id=claim["id"]):
            raise Refusal("RESERVATION_ACTIVE", "This case has a reservation. Staff must cancel it before changing the reviewed evidence.")
        if claim["status"] in ("RETURNED", "CANCELLED"):
            raise Refusal("TERMINAL_RECORD", "This completed case cannot be changed.")

    @staticmethod
    def _invalidate(db, claim_id, reason):
        db.execute("UPDATE reviews SET invalidated_at=?,invalidated_reason=? WHERE claim_id=? AND invalidated_at IS NULL",
                   (utcnow(), reason, claim_id))

    def _notice(self, db, ctx, claim_id, topic, text, audience="claimant", **extra):
        room = self.store.setting(db, "staff_room_id") if audience == "staff" else self._claim(db, claim_id)["case_room_id"]
        if not room:
            raise RuntimeError("Missing enrolled notification destination")
        db.execute("INSERT INTO outbox(id,source_event_id,topic,destination_room_id,visibility,payload_json,created_at) VALUES (?,?,?,?,?,?,?)",
                   (identifier("O-"), ctx.source_event_id, topic, room, audience,
                    dumps({"text": text, "claim_id": claim_id, **extra}), utcnow()))

    @staticmethod
    def _snapshot(db, args):
        out = {}
        item_id = args.get("item_id")
        claim_id = args.get("claim_id")
        if claim_id:
            claim = db.execute("SELECT id,status,revision,state_version,selected_item_id FROM claims WHERE id=?", (claim_id,)).fetchone()
            if claim:
                out["claims"] = dict(claim)
                item_id = item_id or claim["selected_item_id"]
        if args.get("reservation_id"):
            reservation = db.execute("SELECT id,claim_id,item_id,status,approval_id FROM reservations WHERE id=?", (args["reservation_id"],)).fetchone()
        elif claim_id:
            reservation = db.execute("SELECT id,claim_id,item_id,status,approval_id FROM reservations WHERE claim_id=? AND status IN ('ACTIVE','HANDOVER_RECORDED')", (claim_id,)).fetchone()
        else:
            reservation = None
        if reservation:
            out["reservations"] = dict(reservation)
            item_id = item_id or reservation["item_id"]
        if item_id:
            item = db.execute("SELECT id,status,revision,state_version FROM items WHERE id=?", (item_id,)).fetchone()
            if item:
                out["items"] = dict(item)
        return out

    def _mutate(self, ctx: ActorContext, action: str, args: dict, operation: Callable) -> dict:
        if not ctx.source_event_id or not ctx.actor_id or not ctx.room_id or ctx.origin not in ("fixture", "switch"):
            return result(False, "FORBIDDEN", "A trusted source event, account and room are required.")
        request = {"action": action, "args": args, "actor_id": ctx.actor_id, "actor_kind": ctx.actor_kind,
                   "room_id": ctx.room_id, "origin": ctx.origin, "source_text": ctx.source_text}
        try:
            fingerprint = sha256(dumps(request).encode()).hexdigest()
        except (TypeError, ValueError):
            return result(False, "INVALID_INPUT", "Arguments must be JSON-safe values.")
        with self.store.transaction() as db:
            old = db.execute("SELECT * FROM processed_events WHERE source_event_id=?", (ctx.source_event_id,)).fetchone()
            if old:
                if old["request_fingerprint"] != fingerprint:
                    return self._audit_refusal(db, ctx, action, args, "REPLAY_MISMATCH", "This source event was already processed with different content.")
                saved = loads(old["result_json"])
                # Cached refusals have no privileged data and remain exactly
                # deduplicated even when the actor is still unauthorized.
                if not saved["ok"]:
                    saved["replayed"] = True
                    return saved
                # Revoked enrollment must not turn a cached privileged result into
                # a read bypass. Preserve the original dedup record on refusal.
                permission = {
                    "save_report_draft": "intake", "confirm_report": "claimant",
                    "record_evidence": "claimant", "register_item": "staff",
                    "correct_item": "staff", "prepare_review": "staff_or_matcher",
                    "request_information": "staff_or_matcher", "approve": "staff",
                    "reject": "staff", "arrange_collection": "staff",
                    "cancel_reservation": "staff", "record_handover": "staff",
                    "confirm_receipt": "claimant",
                }[action]
                try:
                    self._authorize(db, ctx, permission, args.get("claim_id"))
                except Refusal as exc:
                    return self._audit_refusal(db, ctx, action, args, exc.code, exc.message)
                saved["replayed"] = True
                return saved
            try:
                self.store.ingest_in(db, ctx)
            except ValueError:
                return self._audit_refusal(db, ctx, action, args, "REPLAY_MISMATCH", "This source event does not match its recorded identity or content.")
            previous = self._snapshot(db, args)
            db.execute("SAVEPOINT domain_operation")
            try:
                code, message, data = operation(db)
                db.execute("RELEASE SAVEPOINT domain_operation")
                outcome = result(True, code, message, data)
            except Refusal as exc:
                db.execute("ROLLBACK TO SAVEPOINT domain_operation")
                db.execute("RELEASE SAVEPOINT domain_operation")
                outcome = result(False, exc.code, exc.message)
            aid = identifier("A-")
            next_state = self._snapshot(db, args)
            affected_item = args.get("item_id") or next_state.get("items", {}).get("id") or previous.get("items", {}).get("id")
            revisions = {name: {key: record[key] for key in ("revision", "state_version") if key in record}
                         for name, record in next_state.items()}
            details = {"message": outcome["message"]}
            for key in ("reason", "review_id", "reservation_id", "draft_id", "supersedes_evidence_id"):
                if args.get(key) is not None:
                    details[key] = args[key]
            for key in ("evidence_id", "receipt_id", "reservation_id", "approval_id", "review_id"):
                if outcome["data"].get(key) is not None:
                    details[key] = outcome["data"][key]
            db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (aid, ctx.source_event_id, ctx.origin, ctx.actor_id, ctx.actor_kind, ctx.room_id, action,
                        args.get("claim_id"), affected_item, dumps(revisions), dumps(previous), dumps(next_state),
                        outcome["code"], dumps(details), ctx.occurred_at or utcnow()))
            outcome["audit_id"] = aid
            db.execute("INSERT INTO processed_events VALUES (?,?,?,?)", (ctx.source_event_id, fingerprint, dumps(outcome), utcnow()))
            return outcome

    def _audit_refusal(self, db, ctx, action, args, code, message):
        aid = identifier("A-")
        db.execute("INSERT INTO audit_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (aid, ctx.source_event_id, ctx.origin, ctx.actor_id, ctx.actor_kind, ctx.room_id,
                    action, args.get("claim_id"), args.get("item_id"), dumps({}), dumps({}), dumps({}),
                    code, dumps({"message": message}), ctx.occurred_at or utcnow()))
        return result(False, code, message, audit_id=aid)

    def _read(self, ctx, operation):
        with self.store.connection() as db:
            try:
                return result(True, "OK", "", operation(db))
            except Refusal as exc:
                return result(False, exc.code, exc.message)

    @staticmethod
    def _require_text(value, label, maximum=10000):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise Refusal("INVALID_INPUT", f"Provide {label} as nonempty text within {maximum} characters.")
        return value.strip()

    @staticmethod
    def _fields(value, allowed):
        if not isinstance(value, dict) or set(value) - set(allowed):
            raise Refusal("INVALID_INPUT", "The structured record contains unsupported fields.")
        if any(v is not None and not isinstance(v, str) for v in value.values()):
            raise Refusal("INVALID_INPUT", "Structured description fields must be text or unknown.")
        return {k: value.get(k) for k in allowed}

    @staticmethod
    def _item_view(row, private=False):
        view = {"id": row["id"], "coarse": loads(row["coarse_json"]), "status": row["status"],
                "revision": row["revision"], "state_version": row["state_version"]}
        if private:
            view["private_facts"] = loads(row["private_facts_json"])
        return view

    def _view(self, db, ctx, claim_id):
        role = self._authorize(db, ctx, "case", claim_id)
        claim = self._claim(db, claim_id)
        staff = role in ("staff", "matcher")
        view = {k: claim[k] for k in ("id", "status", "revision", "evidence_revision", "state_version", "selected_item_id")}
        view["report"] = loads(claim["confirmed_report_json"])
        evs = db.execute("SELECT * FROM evidence WHERE claim_id=? ORDER BY rowid", (claim_id,)).fetchall()
        evidence = [{k: e[k] for k in ("id", "text", "attachment_ref", "actor_id", "actor_kind", "source_event_id", "origin", "occurred_at", "supersedes_id")} for e in evs]
        data = {"claim": view, "evidence": evidence, "verification_question": VERIFICATION_QUESTION}
        if claim["current_draft_id"]:
            draft = db.execute("SELECT * FROM drafts WHERE id=?", (claim["current_draft_id"],)).fetchone()
            data["draft"] = {"draft_id": draft["id"], "fields": loads(draft["fields_json"]),
                             "draft_revision": draft["draft_revision"], "confirmed_at": draft["confirmed_at"],
                             "proposed_by": draft["created_by"], "source_event_ids": loads(draft["source_event_ids_json"])}
        reservation = db.execute("SELECT * FROM reservations WHERE claim_id=? ORDER BY rowid DESC LIMIT 1", (claim_id,)).fetchone()
        if reservation:
            data["reservation"] = {k: reservation[k] for k in ("id", "item_id", "status", "collection_instructions")}
            returned = db.execute("SELECT * FROM returns WHERE reservation_id=?", (reservation["id"],)).fetchone()
            if returned:
                data["return"] = {k: returned[k] for k in ("reservation_id", "handed_over_at", "receipt_id", "returned_at")}
        if staff:
            data["claim"]["claimant_actor_id"] = claim["claimant_actor_id"]
            data["claim"]["case_room_id"] = claim["case_room_id"]
            data["items"] = [self._item_view(row, True) for row in db.execute("SELECT * FROM items ORDER BY id")]
            data["reviews"] = [self._review_view(row) for row in db.execute("SELECT * FROM reviews WHERE claim_id=? ORDER BY rowid", (claim_id,))]
        return data

    @staticmethod
    def _review_view(row):
        return {"review_id": row["id"], "claim_id": row["claim_id"], "claim_revision": row["claim_revision"],
                "evidence_revision": row["evidence_revision"], "item_revisions": loads(row["item_revisions_json"]),
                "comparison": loads(row["deterministic_comparison_json"]), "proposed_analysis": loads(row["proposed_analysis_json"]),
                "authored_by": row["authored_by"], "invalidated_at": row["invalidated_at"],
                "invalidated_reason": row["invalidated_reason"]}

    def case_view(self, ctx, *, claim_id):
        return self._read(ctx, lambda db: self._view(db, ctx, claim_id))

    def save_report_draft(self, ctx, *, claim_id, report, source_event_ids=None):
        args = dict(claim_id=claim_id, report=report, source_event_ids=source_event_ids or [])
        def op(db):
            self._authorize(db, ctx, "intake", claim_id)
            claim = self._claim(db, claim_id)
            self._unfrozen_claim(db, claim)
            fields = self._fields(report, ("category", "color", "brand_model", "lost_place", "lost_at", "description"))
            if not any(fields.values()):
                raise Refusal("INVALID_INPUT", "Provide a description before drafting a report.")
            for sid in args["source_event_ids"]:
                source = db.execute("SELECT * FROM source_messages WHERE source_event_id=?", (sid,)).fetchone()
                if not source or source["actor_id"] != claim["claimant_actor_id"] or source["actor_kind"] != "human" or source["room_id"] != claim["case_room_id"]:
                    raise Refusal("INVALID_INPUT", "Report sources must be actual messages from this claimant's case.")
            did = identifier("D-")
            rev = db.execute("SELECT COUNT(*) FROM drafts WHERE claim_id=?", (claim_id,)).fetchone()[0] + 1
            db.execute("INSERT INTO drafts VALUES (?,?,?,?,?,?,?,?,?,NULL)",
                       (did, claim_id, dumps(fields), dumps(args["source_event_ids"]), ctx.actor_id, ctx.source_event_id, ctx.origin, rev, utcnow(),))
            db.execute("UPDATE claims SET current_draft_id=?,updated_at=? WHERE id=?", (did, utcnow(), claim_id))
            self._notice(db, ctx, claim_id, "report-draft", f"Draft report {did}: {fields.get('description') or 'Please review the structured report.'}\nPlease confirm this draft before staff review.", draft_id=did, fields=fields)
            return "DRAFT_SAVED", "Report drafted; claimant confirmation is required.", {"draft_id": did, "draft_revision": rev, "fields": fields}
        return self._mutate(ctx, "save_report_draft", args, op)

    def confirm_report(self, ctx, *, claim_id, draft_id):
        args = dict(claim_id=claim_id, draft_id=draft_id)
        def op(db):
            self._authorize(db, ctx, "claimant", claim_id)
            claim = self._claim(db, claim_id)
            self._unfrozen_claim(db, claim)
            if claim["current_draft_id"] != draft_id:
                raise Refusal("STALE_REVISION", "Confirm the current report draft.")
            draft = db.execute("SELECT * FROM drafts WHERE id=? AND claim_id=?", (draft_id, claim_id)).fetchone()
            if not draft:
                raise Refusal("NOT_FOUND", "Report draft not found.")
            if draft["confirmed_at"]:
                return "REPORT_CONFIRMED", "This report is already confirmed.", self._view(db, ctx, claim_id)
            now = utcnow()
            self._invalidate(db, claim_id, "Confirmed report changed")
            db.execute("UPDATE drafts SET confirmed_at=? WHERE id=?", (now, draft_id))
            db.execute("UPDATE claims SET confirmed_report_json=?,status='NEEDS_INFO',revision=revision+1,state_version=state_version+1,updated_at=? WHERE id=?", (draft["fields_json"], now, claim_id))
            self._notice(db, ctx, claim_id, "report-confirmed", f"Report confirmed for {claim_id}. {VERIFICATION_QUESTION}")
            self._notice(db, ctx, claim_id, "staff-report", f"{claim_id}: claimant confirmed a lost-item report. Staff comparison is available.", "staff")
            return "REPORT_CONFIRMED", "Report confirmed; identifying evidence and staff review are needed.", self._view(db, ctx, claim_id)
        return self._mutate(ctx, "confirm_report", args, op)

    def record_evidence(self, ctx, *, claim_id, text, attachment_ref=None, supersedes_evidence_id=None):
        args = dict(claim_id=claim_id, text=text, attachment_ref=attachment_ref, supersedes_evidence_id=supersedes_evidence_id)
        def op(db):
            self._authorize(db, ctx, "claimant", claim_id)
            claim = self._claim(db, claim_id)
            self._unfrozen_claim(db, claim)
            clean = self._require_text(text, "the claimant's identifying detail")
            if attachment_ref is not None and (not isinstance(attachment_ref, str) or len(attachment_ref) > 2000):
                raise Refusal("INVALID_INPUT", "Attachment reference is invalid.")
            if supersedes_evidence_id:
                old = db.execute("SELECT * FROM evidence WHERE id=? AND claim_id=?", (supersedes_evidence_id, claim_id)).fetchone()
                if not old or old["actor_id"] != ctx.actor_id:
                    raise Refusal("INVALID_INPUT", "Only your own evidence in this case can be corrected.")
            eid = identifier("E-")
            db.execute("INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?)", (eid, claim_id, clean, attachment_ref, ctx.actor_id, ctx.actor_kind,
                       ctx.source_event_id, ctx.origin, ctx.occurred_at or utcnow(), supersedes_evidence_id))
            self._invalidate(db, claim_id, "Claimant evidence changed")
            db.execute("UPDATE claims SET revision=revision+1,evidence_revision=evidence_revision+1,status='NEEDS_INFO',state_version=state_version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._notice(db, ctx, claim_id, "evidence-recorded", f"Your identifying evidence was recorded for {claim_id}. Staff review is needed.", evidence_id=eid)
            self._notice(db, ctx, claim_id, "staff-evidence", f"{claim_id}: new claimant assertion ({eid}): {clean}", "staff", evidence_id=eid)
            return "EVIDENCE_RECORDED", "Claimant assertion recorded; it has not been verified as ownership.", {"evidence_id": eid, **self._view(db, ctx, claim_id)}
        return self._mutate(ctx, "record_evidence", args, op)

    def register_item(self, ctx, *, item_id, coarse, private_facts):
        args = dict(item_id=item_id, coarse=coarse, private_facts=private_facts)
        def op(db):
            self._authorize(db, ctx, "staff")
            self._require_text(item_id, "an item ID", 80)
            fields = self._fields(coarse, ("category", "color", "brand_model", "found_place", "found_at"))
            if not isinstance(private_facts, (str, list)) or isinstance(private_facts, list) and any(not isinstance(x, str) for x in private_facts):
                raise Refusal("INVALID_INPUT", "Private facts must be text or a list of text observations.")
            if db.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone():
                raise Refusal("INVALID_INPUT", "This item ID already exists. Use an explicit correction.")
            now = utcnow()
            db.execute("INSERT INTO items(id,coarse_json,private_facts_json,status,created_at,updated_at) VALUES (?,?,?,'AVAILABLE',?,?)", (item_id, dumps(fields), dumps(private_facts), now, now))
            return "ITEM_REGISTERED", "Found item registered in staff inventory.", {"item": self._item_view(self._item(db, item_id), True)}
        return self._mutate(ctx, "register_item", args, op)

    def correct_item(self, ctx, *, item_id, expected_revision, coarse_patch=None, private_facts=None, reason):
        args = dict(item_id=item_id, expected_revision=expected_revision, coarse_patch=coarse_patch, private_facts=private_facts, reason=reason)
        def op(db):
            self._authorize(db, ctx, "staff")
            item = self._item(db, item_id)
            self._require_text(reason, "a correction reason")
            if self._active(db, item_id=item_id):
                raise Refusal("RESERVATION_ACTIVE", "Cancel the reservation before changing this item's reviewed facts.")
            if item["status"] == "RETURNED":
                raise Refusal("TERMINAL_RECORD", "Returned item facts are immutable in this workflow.")
            if item["revision"] != expected_revision:
                raise Refusal("STALE_REVISION", "The item changed; refresh its current revision.")
            coarse = loads(item["coarse_json"])
            if coarse_patch is not None:
                if not isinstance(coarse_patch, dict) or set(coarse_patch) - set(coarse):
                    raise Refusal("INVALID_INPUT", "The item patch contains unsupported fields.")
                coarse.update(coarse_patch)
                coarse = self._fields(coarse, tuple(coarse))
            facts = loads(item["private_facts_json"]) if private_facts is None else private_facts
            if not isinstance(facts, (str, list)) or isinstance(facts, list) and any(not isinstance(x, str) for x in facts):
                raise Refusal("INVALID_INPUT", "Private facts must be text or a list of text observations.")
            db.execute("UPDATE items SET coarse_json=?,private_facts_json=?,revision=revision+1,updated_at=? WHERE id=?", (dumps(coarse), dumps(facts), utcnow(), item_id))
            for review in db.execute("SELECT id,item_revisions_json FROM reviews WHERE invalidated_at IS NULL").fetchall():
                if item_id in loads(review["item_revisions_json"]):
                    db.execute("UPDATE reviews SET invalidated_at=?,invalidated_reason=? WHERE id=?", (utcnow(), "Item facts changed", review["id"]))
            return "ITEM_CORRECTED", "Item facts corrected; earlier comparisons require a fresh review.", {"item": self._item_view(self._item(db, item_id), True)}
        return self._mutate(ctx, "correct_item", args, op)

    def _candidates(self, db, claim):
        if not claim["confirmed_report_json"]:
            raise Refusal("REPORT_UNCONFIRMED", "Confirm the report before looking for possible matches.")
        items = [self._item_view(row) for row in db.execute("SELECT * FROM items ORDER BY id")]
        return coarse_candidates(loads(claim["confirmed_report_json"]), items)

    def candidates(self, ctx, *, claim_id):
        def read(db):
            self._authorize(db, ctx, "case", claim_id)
            claim = self._claim(db, claim_id)
            candidates = self._candidates(db, claim)
            return {"candidates": candidates, "count": len(candidates), "verification_question": VERIFICATION_QUESTION,
                    "assessment": "Possible matches only. Staff decides whether identifying evidence is sufficient."}
        return self._read(ctx, read)

    def staff_comparison(self, ctx, *, claim_id):
        def read(db):
            self._authorize(db, ctx, "staff_or_matcher", claim_id)
            claim = self._claim(db, claim_id)
            return {**self._view(db, ctx, claim_id), "candidates": self._candidates(db, claim)}
        return self._read(ctx, read)

    def prepare_review(self, ctx, *, claim_id, item_ids, analysis=None, expected_basis=None):
        args = dict(claim_id=claim_id, item_ids=item_ids, analysis=analysis, expected_basis=expected_basis)
        def op(db):
            self._authorize(db, ctx, "staff_or_matcher", claim_id)
            claim = self._claim(db, claim_id)
            self._unfrozen_claim(db, claim)
            candidates = self._candidates(db, claim)
            if not isinstance(item_ids, list) or not item_ids or any(not isinstance(x, str) for x in item_ids):
                raise Refusal("INVALID_INPUT", "Select at least one found item for comparison.")
            items = [self._item(db, iid) for iid in dict.fromkeys(item_ids)]
            if expected_basis is not None:
                if (not isinstance(expected_basis, dict) or
                        set(expected_basis) != {"claim_revision", "evidence_revision", "item_revisions"} or
                        not isinstance(expected_basis.get("item_revisions"), dict)):
                    raise Refusal("INVALID_INPUT", "Expected review basis must identify the claim, evidence and item revisions.")
                if (expected_basis["claim_revision"] != claim["revision"] or
                        expected_basis["evidence_revision"] != claim["evidence_revision"] or
                        any(expected_basis["item_revisions"].get(i["id"]) != i["revision"] for i in items)):
                    raise Refusal("STALE_REVISION", "Facts changed while the comparison was prepared. Request a fresh review.")
            evidence = [dict(e) for e in db.execute("SELECT * FROM evidence WHERE claim_id=? ORDER BY rowid", (claim_id,))]
            comparison = {"report": loads(claim["confirmed_report_json"]), "candidates": candidates,
                          "items": [self._item_view(i, True) for i in items], "claimant_assertions": evidence,
                          "ownership_decision": "Not determined by software; staff judgment required."}
            rid = identifier("R-")
            revs = {i["id"]: i["revision"] for i in items}
            db.execute("INSERT INTO reviews VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                       (rid, claim_id, claim["revision"], claim["evidence_revision"], dumps(revs), dumps(comparison),
                        dumps(analysis) if analysis is not None else None, ctx.actor_id, ctx.source_event_id, utcnow()))
            db.execute("UPDATE claims SET status='UNDER_REVIEW',state_version=state_version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._notice(db, ctx, claim_id, "review-ready", f"{claim_id}: review {rid} prepared for claim revision {claim['revision']}, evidence revision {claim['evidence_revision']}. Staff must inspect the comparison before deciding.", "staff", review_id=rid)
            return "REVIEW_PREPARED", "Exact comparison prepared for human staff review.", {"review_id": rid, "claim_revision": claim["revision"], "evidence_revision": claim["evidence_revision"], "item_revisions": revs, "comparison": comparison}
        return self._mutate(ctx, "prepare_review", args, op)

    def request_information(self, ctx, *, claim_id, reason):
        args = dict(claim_id=claim_id, reason=reason)
        def op(db):
            self._authorize(db, ctx, "staff_or_matcher", claim_id)
            claim = self._claim(db, claim_id)
            self._unfrozen_claim(db, claim)
            self._require_text(reason, "a staff review reason")
            db.execute("UPDATE claims SET status='NEEDS_INFO',state_version=state_version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._notice(db, ctx, claim_id, "more-information", f"Staff need more identifying evidence for {claim_id}. {VERIFICATION_QUESTION}")
            self._notice(db, ctx, claim_id, "staff-more-information", f"{claim_id}: more information requested. Staff reason: {reason}", "staff")
            return "INFORMATION_REQUESTED", "A non-leading request was sent to the claimant.", {"claim": {"id": claim_id, "status": "NEEDS_INFO"}}
        return self._mutate(ctx, "request_information", args, op)

    def _exact_review(self, db, claim, item, review_id):
        review = db.execute("SELECT * FROM reviews WHERE id=? AND claim_id=?", (review_id, claim["id"])).fetchone()
        if not review:
            raise Refusal("REVIEW_REQUIRED", "Prepare and inspect a current review of this case and item.")
        if (review["invalidated_at"] or review["claim_revision"] != claim["revision"] or
                review["evidence_revision"] != claim["evidence_revision"] or
                loads(review["item_revisions_json"]).get(item["id"]) != item["revision"]):
            raise Refusal("STALE_REVISION", "The reviewed facts changed. Prepare a fresh review before deciding.")
        return review

    def approve(self, ctx, *, claim_id, item_id, review_id, reason):
        args = dict(claim_id=claim_id, item_id=item_id, review_id=review_id, reason=reason)
        def op(db):
            self._authorize(db, ctx, "staff", claim_id)
            claim, item = self._claim(db, claim_id), self._item(db, item_id)
            self._require_text(reason, "the staff decision reason")
            if not claim["confirmed_report_json"]:
                raise Refusal("REPORT_UNCONFIRMED", "The claimant must confirm their report first.")
            self._exact_review(db, claim, item, review_id)
            if self._active(db, claim_id=claim_id):
                raise Refusal("RESERVATION_ACTIVE", "This case already has an active reservation.")
            if claim["status"] not in ("UNDER_REVIEW", "NEEDS_INFO"):
                raise Refusal("INVALID_STATE", "This case is not eligible for approval.")
            if item["status"] != "AVAILABLE" or self._active(db, item_id=item_id):
                raise Refusal("ITEM_UNAVAILABLE", "The selected item is no longer available.")
            aid, rsid, now = identifier("AP-"), identifier("RS-"), utcnow()
            db.execute("INSERT INTO approvals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (aid, claim_id, claim["revision"], claim["evidence_revision"], item_id,
                       item["revision"], review_id, ctx.actor_id, ctx.source_event_id, reason.strip(), "VALID", now))
            db.execute("INSERT INTO reservations(id,approval_id,claim_id,item_id,status,created_at) VALUES (?,?,?,?,'ACTIVE',?)", (rsid, aid, claim_id, item_id, now))
            db.execute("UPDATE items SET status='RESERVED',state_version=state_version+1,updated_at=? WHERE id=?", (now, item_id))
            db.execute("UPDATE claims SET status='APPROVED',selected_item_id=?,state_version=state_version+1,updated_at=? WHERE id=?", (item_id, now, claim_id))
            self._notice(db, ctx, claim_id, "reserved", f"Staff reserved an item for {claim_id}. Reservation {rsid}. Collection is pending; it has not been returned.", reservation_id=rsid, status="APPROVED")
            self._notice(db, ctx, claim_id, "staff-reserved", f"{claim_id}: {item_id} reserved as {rsid} by the enrolled staff reviewer. Handover and claimant receipt are still required.", "staff", reservation_id=rsid)
            return "RESERVED", "Item reserved; collection is pending.", {"reservation_id": rsid, "approval_id": aid, "review_id": review_id, **self._view(db, ctx, claim_id)}
        return self._mutate(ctx, "approve", args, op)

    def reject(self, ctx, *, claim_id, item_id, review_id, reason):
        args = dict(claim_id=claim_id, item_id=item_id, review_id=review_id, reason=reason)
        def op(db):
            self._authorize(db, ctx, "staff", claim_id)
            claim, item = self._claim(db, claim_id), self._item(db, item_id)
            self._unfrozen_claim(db, claim)
            self._require_text(reason, "the staff decision reason")
            self._exact_review(db, claim, item, review_id)
            db.execute("UPDATE claims SET status='REJECTED',state_version=state_version+1,updated_at=? WHERE id=?", (utcnow(), claim_id))
            self._notice(db, ctx, claim_id, "not-approved", f"Staff did not approve the proposed match for {claim_id}. No item has been reserved. Contact staff if you have more identifying information.")
            self._notice(db, ctx, claim_id, "staff-rejected", f"{claim_id}: proposed match to {item_id} rejected. Staff reason: {reason}", "staff")
            return "REJECTED", "Proposed match rejected; no item was reserved.", {"claim": {"id": claim_id, "status": "REJECTED"}}
        return self._mutate(ctx, "reject", args, op)

    def _reservation(self, db, claim_id, reservation_id):
        row = db.execute("SELECT * FROM reservations WHERE id=? AND claim_id=?", (reservation_id, claim_id)).fetchone()
        if not row:
            raise Refusal("INVALID_STATE", "This reservation does not belong to the case.")
        return row

    def arrange_collection(self, ctx, *, claim_id, reservation_id, instructions):
        args = dict(claim_id=claim_id, reservation_id=reservation_id, instructions=instructions)
        def op(db):
            self._authorize(db, ctx, "staff", claim_id)
            reservation = self._reservation(db, claim_id, reservation_id)
            if reservation["status"] != "ACTIVE":
                raise Refusal("INVALID_STATE", "Collection can be arranged only for an active reservation.")
            clean = self._require_text(instructions, "collection instructions")
            db.execute("UPDATE reservations SET collection_instructions=? WHERE id=?", (clean, reservation_id))
            self._notice(db, ctx, claim_id, "collection-arranged", f"Collection arranged for {claim_id}: {clean}\nThe case remains open until staff handover and your receipt confirmation.", reservation_id=reservation_id)
            return "COLLECTION_ARRANGED", "Collection arranged; the item is not yet returned.", {"reservation_id": reservation_id, "status": "APPROVED"}
        return self._mutate(ctx, "arrange_collection", args, op)

    def cancel_reservation(self, ctx, *, claim_id, reservation_id, reason):
        args = dict(claim_id=claim_id, reservation_id=reservation_id, reason=reason)
        def op(db):
            self._authorize(db, ctx, "staff", claim_id)
            reservation = self._reservation(db, claim_id, reservation_id)
            clean = self._require_text(reason, "a cancellation reason")
            if reservation["status"] == "HANDOVER_RECORDED":
                raise Refusal("HANDOVER_ALREADY_RECORDED", "Handover is recorded. This item cannot be released back into available inventory.")
            if reservation["status"] != "ACTIVE":
                raise Refusal("INVALID_STATE", "Only the current active reservation can be cancelled.")
            now = utcnow()
            db.execute("UPDATE reservations SET status='CANCELLED',cancelled_at=?,cancellation_reason=? WHERE id=?", (now, clean, reservation_id))
            db.execute("UPDATE approvals SET status='INVALIDATED' WHERE id=?", (reservation["approval_id"],))
            db.execute("UPDATE items SET status='AVAILABLE',state_version=state_version+1,updated_at=? WHERE id=?", (now, reservation["item_id"]))
            db.execute("UPDATE claims SET status='NEEDS_INFO',selected_item_id=NULL,state_version=state_version+1,updated_at=? WHERE id=?", (now, claim_id))
            self._invalidate(db, claim_id, "Reservation cancelled; fresh review required")
            self._notice(db, ctx, claim_id, "reservation-cancelled", f"The collection reservation for {claim_id} was cancelled by staff. Do not collect under the old reservation; a fresh review is needed.", reservation_id=reservation_id)
            return "RESERVATION_CANCELLED", "Reservation cancelled; a fresh review is required.", {"reservation_id": reservation_id, "status": "NEEDS_INFO"}
        return self._mutate(ctx, "cancel_reservation", args, op)

    def record_handover(self, ctx, *, claim_id, reservation_id, note):
        args = dict(claim_id=claim_id, reservation_id=reservation_id, note=note)
        def op(db):
            self._authorize(db, ctx, "staff", claim_id)
            reservation = self._reservation(db, claim_id, reservation_id)
            if reservation["status"] == "HANDOVER_RECORDED":
                return "HANDOVER_RECORDED", "Staff handover is already recorded; claimant receipt is pending.", {"reservation_id": reservation_id, "status": "HANDOVER_RECORDED"}
            if reservation["status"] != "ACTIVE":
                raise Refusal("INVALID_STATE", "Handover requires the current active reservation.")
            self._require_text(note, "a handover note")
            approval = db.execute("SELECT * FROM approvals WHERE id=?", (reservation["approval_id"],)).fetchone()
            if not approval or approval["status"] != "VALID":
                raise Refusal("STALE_REVISION", "The approval is not valid; staff review is required.")
            claim, item = self._claim(db, claim_id), self._item(db, reservation["item_id"])
            self._exact_review(db, claim, item, approval["review_id"])
            now = utcnow()
            db.execute("INSERT INTO returns(reservation_id,staff_handover_event_id,staff_actor_id,handed_over_at,handover_note) VALUES (?,?,?,?,?)", (reservation_id, ctx.source_event_id, ctx.actor_id, now, note.strip()))
            db.execute("UPDATE reservations SET status='HANDOVER_RECORDED' WHERE id=?", (reservation_id,))
            db.execute("UPDATE claims SET status='HANDOVER_RECORDED',state_version=state_version+1,updated_at=? WHERE id=?", (now, claim_id))
            self._notice(db, ctx, claim_id, "handover-recorded", f"Staff recorded handover for {claim_id}, reservation {reservation_id}. Please confirm receipt only if you received the item. The return is not yet closed.", reservation_id=reservation_id)
            return "HANDOVER_RECORDED", "Staff handover recorded; claimant receipt is pending.", {"reservation_id": reservation_id, "status": "HANDOVER_RECORDED"}
        return self._mutate(ctx, "record_handover", args, op)

    def confirm_receipt(self, ctx, *, claim_id, reservation_id):
        args = dict(claim_id=claim_id, reservation_id=reservation_id)
        def op(db):
            self._authorize(db, ctx, "claimant", claim_id)
            reservation = self._reservation(db, claim_id, reservation_id)
            prior = db.execute("SELECT * FROM returns WHERE reservation_id=?", (reservation_id,)).fetchone()
            if reservation["status"] == "RETURNED" and prior and prior["receipt_id"]:
                return "RETURNED", "This return is already confirmed.", {"receipt_id": prior["receipt_id"], "reservation_id": reservation_id, "status": "RETURNED"}
            if reservation["status"] != "HANDOVER_RECORDED" or not prior:
                raise Refusal("HANDOVER_REQUIRED", "Staff must record handover for this reservation before you can confirm receipt.")
            rid, now = identifier("RET-"), utcnow()
            db.execute("UPDATE returns SET claimant_receipt_event_id=?,claimant_actor_id=?,receipt_id=?,returned_at=? WHERE reservation_id=?", (ctx.source_event_id, ctx.actor_id, rid, now, reservation_id))
            db.execute("UPDATE reservations SET status='RETURNED' WHERE id=?", (reservation_id,))
            db.execute("UPDATE items SET status='RETURNED',state_version=state_version+1,updated_at=? WHERE id=?", (now, reservation["item_id"]))
            db.execute("UPDATE claims SET status='RETURNED',state_version=state_version+1,updated_at=? WHERE id=?", (now, claim_id))
            self._notice(db, ctx, claim_id, "returned", f"Return confirmed for {claim_id}. Staff handover and your receipt are both recorded. Receipt {rid}.", receipt_id=rid, status="RETURNED")
            self._notice(db, ctx, claim_id, "staff-returned", f"{claim_id}: return confirmed after staff handover and enrolled claimant receipt. Receipt {rid}.", "staff", receipt_id=rid, status="RETURNED")
            return "RETURNED", "Staff handover and claimant receipt recorded; return complete.", {"receipt_id": rid, "reservation_id": reservation_id, "status": "RETURNED"}
        return self._mutate(ctx, "confirm_receipt", args, op)

    def audit_trail(self, ctx, *, claim_id):
        def read(db):
            role = self._authorize(db, ctx, "case", claim_id)
            rows = db.execute("SELECT * FROM audit_events WHERE claim_id=? ORDER BY rowid", (claim_id,)).fetchall()
            if role in ("staff", "matcher"):
                return {"events": [{**dict(r), "previous": loads(r["previous_json"]), "next": loads(r["next_json"]), "details": loads(r["details_json"])} for r in rows]}
            safe = {"REPORT_CONFIRMED", "EVIDENCE_RECORDED", "INFORMATION_REQUESTED", "RESERVED", "COLLECTION_ARRANGED", "RESERVATION_CANCELLED", "HANDOVER_RECORDED", "RETURNED", "REJECTED"}
            return {"events": [{k: r[k] for k in ("id", "action", "result_code", "occurred_at", "origin")} for r in rows if r["result_code"] in safe]}
        return self._read(ctx, read)
