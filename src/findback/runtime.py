"""Deterministic command routing between authenticated chat and domain rules.

Only this adapter constructs ActorContext. Model output never chooses an actor,
room, privileged operation, or notification destination.
"""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import hashlib
import json
import fcntl
from pathlib import Path
import re
import sqlite3
from typing import Any

import httpx

from .domain import ActorContext, DomainService
from .model import CodexModel, ModelError
from .storage import SQLiteStore, dumps, loads
from .switch_client import SwitchClient


PREFIX = re.compile(r"^(?:@findback-[a-z0-9-]+\s+)?findback\s+([a-z-]+)(?:\s+(.*))?$", re.DOTALL)
GENERIC_QUESTION = "What marks, contents, or accessories would help staff identify yours? Please describe them in your own words."


def parse_command(text: str) -> tuple[str, str] | None:
    """No searching inside quotes, attachments, model paraphrases, or prose."""
    match = PREFIX.fullmatch(text.strip())
    return (match[1], (match[2] or "").strip()) if match else None


class RuntimeJournal:
    def __init__(self, store: SQLiteStore):
        self.store = store
        with store.connection() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS wire_frames (
              client_id TEXT NOT NULL, frame_key TEXT NOT NULL, frame_json TEXT NOT NULL,
              PRIMARY KEY(client_id,frame_key));
            CREATE TABLE IF NOT EXISTS inbox (
              event_id TEXT PRIMARY KEY, frame_json TEXT NOT NULL,
              state TEXT NOT NULL DEFAULT 'PENDING', error TEXT, updated_at TEXT);
            CREATE TABLE IF NOT EXISTS model_proposals (
              job_id TEXT PRIMARY KEY, role TEXT NOT NULL, input_hash TEXT NOT NULL,
              result_json TEXT NOT NULL, metrics_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_messages (
              id TEXT PRIMARY KEY, room_id TEXT NOT NULL, speaker TEXT NOT NULL,
              body TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING', remote_id TEXT);
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(runtime_messages)")}
            if "thread_id" not in columns:
                db.execute("ALTER TABLE runtime_messages ADD COLUMN thread_id TEXT")

    def capture(self, client_id: str, frame: dict, primary: bool) -> None:
        encoded = dumps(frame)
        key = hashlib.sha256(encoded.encode()).hexdigest()
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO wire_frames VALUES (?,?,?)", (client_id, key, encoded))
            if primary and frame.get("event") == "message":
                event_id = frame["data"].get("payload", {}).get("message_id")
                if event_id:
                    db.execute("INSERT OR IGNORE INTO inbox(event_id,frame_json) VALUES (?,?)", (event_id, encoded))

    def pending(self) -> list[dict]:
        with self.store.connection() as db:
            return [loads(r[0]) for r in db.execute("SELECT frame_json FROM inbox WHERE state='PENDING' ORDER BY rowid LIMIT 100")]

    def finish(self, event_id: str, error: str | None = None) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE inbox SET state=?,error=?,updated_at=? WHERE event_id=?",
                       ("FAILED" if error else "DONE", error, datetime.now(timezone.utc).isoformat(), event_id))

    def queue(self, key: str, room: str, body: str, speaker: str = "service", thread_id: str | None = None) -> None:
        with self.store.transaction() as db:
            db.execute("INSERT OR IGNORE INTO runtime_messages(id,room_id,speaker,body,thread_id) VALUES (?,?,?,?,?)",
                       (key, room, speaker, body, thread_id))


class FindBackRuntime:
    def __init__(self, config: dict, state_dir: Path, *, model: CodexModel | None = None):
        self.config, self.state_dir = config, Path(state_dir)
        self.store = SQLiteStore(self.state_dir / "findback.db")
        self.domain = DomainService(self.store)
        self.journal = RuntimeJournal(self.store)
        self.model = model or CodexModel()
        self.clients: dict[str, SwitchClient] = {}
        self.staff_room = config["staff_room"]["id"]
        self.cases = config["cases"]
        self.rooms_to_cases = {v["room_id"]: k for k, v in self.cases.items()}
        self.http = httpx.AsyncClient(timeout=20)
        self._stop = asyncio.Event()

    async def __aenter__(self):
        try:
            return await self._start()
        except BaseException:
            stack = getattr(self, "stack", None)
            if stack is not None:
                try:
                    await stack.aclose()
                except BaseException as cleanup_error:
                    print(f"Startup cleanup requires attention ({type(cleanup_error).__name__}).", flush=True)
            lock = getattr(self, "_process_lock", None)
            if lock is not None:
                lock.close()
            try:
                await self.http.aclose()
            except BaseException as cleanup_error:
                print(f"HTTP cleanup requires attention ({type(cleanup_error).__name__}).", flush=True)
            finally:
                self.store.close()
            raise

    async def _start(self):
        self._process_lock = (self.state_dir / "runner.lock").open("a+")
        try:
            fcntl.flock(self._process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._process_lock.close()
            raise RuntimeError("Another FindBack runtime already owns this state directory.") from None
        self.stack = AsyncExitStack()
        await self.stack.__aenter__()
        self.stack.callback(self._process_lock.close)
        rooms = [self.staff_room, *self.rooms_to_cases, *self.config.get("additional_service_rooms", [])]
        specs = {"service": (self.config["agents"]["service"], rooms),
                 "matcher": (self.config["agents"]["matcher"], [self.staff_room])}
        specs.update({f"intake-{cid}": (case["intake_agent"], [case["room_id"]]) for cid, case in self.cases.items()})
        for key, (agent, agent_rooms) in specs.items():
            client = SwitchClient(self.config["switch_url"], agent["id"], agent["api_key"], agent_rooms,
                                  self.state_dir / f"connection-{key}.json")
            self.clients[key] = await self.stack.enter_async_context(client)
        self.tasks = [asyncio.create_task(self._capture(key, client)) for key, client in self.clients.items()]
        self.tasks.extend([asyncio.create_task(self._work()), asyncio.create_task(self._deliver())])
        return self

    async def __aexit__(self, *args):
        self._stop.set()
        try:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.stack.__aexit__(*args)
        finally:
            self._process_lock.close()
            try:
                await self.http.aclose()
            finally:
                self.store.close()

    async def _capture(self, key: str, client: SwitchClient):
        async for frame in client.events():
            # Commit the original envelope before acknowledging the resume cursor.
            self.journal.capture(client.agent_id, frame, key == "service")
            await client.ack(frame)

    async def run_forever(self):
        print("FindBack is listening in the three private demo rooms. Ctrl-C stops safely.", flush=True)
        done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()

    async def _work(self):
        while not self._stop.is_set():
            for frame in self.journal.pending():
                payload = frame["data"].get("payload", {})
                eid = payload["message_id"]
                try:
                    await self.handle(frame)
                except ModelError:
                    self.journal.queue(eid + ":model-error", frame["data"]["room_id"],
                                       "The model proposal could not be prepared. Your message is saved; no item was released. Use status, then send the command again.")
                    self.journal.finish(eid, "MODEL_UNAVAILABLE")
                    continue
                except Exception as exc:
                    # Safe diagnostic class only; payload/provider prose may contain private data.
                    self.journal.finish(eid, type(exc).__name__)
                    print(f"Message requires operator attention: {eid} ({type(exc).__name__})", flush=True)
                    continue
                self.journal.finish(eid)
            await asyncio.sleep(0.15)

    async def _verified_context(self, frame: dict) -> ActorContext | None:
        data, payload = frame["data"], frame["data"].get("payload", {})
        room, sender = data.get("room_id"), payload.get("sender")
        humans = [(self.config["staff"]["actor_id"], self.config["staff"]["human"], self.staff_room)]
        humans += [(c["claimant_actor_id"], c["human"], c["room_id"]) for c in self.cases.values()]
        for actor_id, human, authorized_room in humans:
            if sender != actor_id:
                continue
            if payload.get("on_behalf_of") or payload.get("sender_kind") in {"agent", "platform"}:
                return None
            # Independently bind the transport event to an immutable Mattermost
            # account/post ID. Enrollment resolves client_id once during setup.
            channel = self.config["staff_room"]["channel_id"] if room == self.staff_room else next(
                (case["channel_id"] for case in self.cases.values() if case["room_id"] == room), None)
            if channel is None:
                return None
            response = await self.http.get(self.config["mattermost_url"] + f"/api/v4/channels/{channel}/posts",
                                           params={"per_page": 200}, headers={"Authorization": "Bearer " + human["token"]})
            if response.status_code != 200:
                return None
            for post in response.json().get("posts", {}).values():
                source = "\x1f".join([self.config["bridge_id"], channel, post["id"]])
                material = "\x1f".join([human["client_id"], source, "0"])
                expected = "sw_" + hashlib.sha256(material.encode()).hexdigest()[:32]
                if expected != payload["message_id"]:
                    continue
                if post["user_id"] != human["id"] or post["channel_id"] != channel or post["message"] != payload.get("body"):
                    return None
                if post.get("type") or post.get("props", {}).get("from_webhook") or post.get("props", {}).get("from_bot"):
                    return None
                return ActorContext(actor_id=actor_id, actor_kind="human", room_id=room,
                                    source_event_id=payload["message_id"], source_text=payload.get("body", ""),
                                    occurred_at=datetime.fromtimestamp(post["create_at"] / 1000, timezone.utc).isoformat(),
                                    origin="switch", thread_id=payload.get("thread_id"))
            # Fail closed if the relevant post is no longer in the first page.
            return None
        # Known agents remain agents even when their body imitates staff.
        for agent in [*self.config["agents"].values(), *(c["intake_agent"] for c in self.cases.values())]:
            if sender == agent.get("actor_id"):
                return ActorContext(actor_id=sender, actor_kind="agent", room_id=room,
                                    source_event_id=payload["message_id"], source_text=payload.get("body", ""), origin="switch")
        return None

    def _agent_context(self, key: str, source: ActorContext, room: str) -> ActorContext:
        agent = self.config["agents"]["matcher"] if key == "matcher" else self.cases[key]["intake_agent"]
        return ActorContext(actor_id=agent["actor_id"], actor_kind="agent", room_id=room,
                            source_event_id=source.source_event_id + ":" + key,
                            source_text="Model proposal responding to " + source.source_event_id,
                            occurred_at=source.occurred_at, origin="switch")

    async def _proposal(self, job: str, role: str, data: dict) -> dict:
        fingerprint = hashlib.sha256(dumps(data).encode()).hexdigest()
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM model_proposals WHERE job_id=?", (job,)).fetchone()
        if row:
            if row["input_hash"] != fingerprint:
                raise ValueError("A proposal job cannot be rebound to different input")
            return loads(row["result_json"])
        result = await (self.model.draft(data["text"]) if role == "intake" else self.model.compare(data))
        with self.store.transaction() as db:
            db.execute("INSERT INTO model_proposals VALUES (?,?,?,?,?)", (job, role, fingerprint, dumps(result), dumps(self.model.measurements[-1])))
        return result

    async def _review(self, source: ActorContext, claim_id: str):
        ctx = self._agent_context("matcher", source, self.staff_room)
        with self.store.connection() as db:
            previous = db.execute("SELECT result_json FROM processed_events WHERE source_event_id=?", (ctx.source_event_id,)).fetchone()
            proposal = db.execute("SELECT result_json FROM model_proposals WHERE job_id=?", (ctx.source_event_id,)).fetchone()
        if previous and proposal:
            # Recover a crash after domain commit and before notification enqueue.
            result, analysis = loads(previous[0]), loads(proposal[0])
        else:
            comparison = self.domain.staff_comparison(ctx, claim_id=claim_id)
            if not comparison["ok"]:
                return comparison
            # The staff-only snapshot is sent only to the Matcher model call.
            analysis = await self._proposal(ctx.source_event_id, "matcher", comparison["data"])
            view = comparison["data"]
            current = self.domain.staff_comparison(ctx, claim_id=claim_id)
            if not current["ok"] or current["data"] != view:
                raise ModelError("The comparison changed during inference; a fresh review is required.")
            candidates = view.get("candidates", [])
            item_ids = [c.get("item_id", c.get("id")) for c in candidates]
            item_ids = [i for i in item_ids if i]
            if not item_ids:
                return self.domain.request_information(ctx, claim_id=claim_id, reason="No currently available coarse candidate. Staff should investigate.")
            basis = {"claim_revision": view["claim"]["revision"],
                     "evidence_revision": view["claim"]["evidence_revision"],
                     "item_revisions": {i["id"]: i["revision"] for i in view["items"]}}
            result = self.domain.prepare_review(ctx, claim_id=claim_id, item_ids=item_ids,
                                                analysis=analysis, expected_basis=basis)
        if result["ok"]:
            rid = result["data"]["review_id"]
            snapshot = result["data"]["comparison"]
            observations = "\n".join(f"- **{i['id']} r{i['revision']}**: " + "; ".join(i["private_facts"])
                                      for i in snapshot["items"])
            assertions = "\n".join(f"- {e['text']} — claimant assertion, source `{e['source_event_id']}`"
                                    for e in snapshot["claimant_assertions"]) or "No independent identifying assertion yet."
            body = (f"### Staff review • {claim_id}\nModel proposal; staff decides. Review `{rid}`\n"
                    f"Claim r{result['data']['claim_revision']} • Evidence r{result['data']['evidence_revision']}\n\n"
                    "**Staff-only observations**\n" + observations + "\n\n**Claimant source**\n" + assertions + "\n\n" +
                    analysis["summary"] + "\n\n" +
                    "\n".join(f"**{label}:** " + ("; ".join(analysis[k]) or "None identified") for k, label in (
                        ("agreements", "Agrees"), ("contradictions", "Contradicts"), ("missing_information", "Still needed"))) +
                    f"\n\nTo approve exact records: `findback approve {claim_id} <item-id> {rid} <your reason>`\n" +
                    "Approval reserves an item. It does not record physical handover.")
            self.journal.queue(ctx.source_event_id + ":review", self.staff_room, body, "matcher")
        return result

    async def handle(self, frame: dict):
        data, payload = frame["data"], frame["data"].get("payload", {})
        command = parse_command(payload.get("body", ""))
        if command is None:
            return
        ctx = await self._verified_context(frame)
        if ctx is None:
            # Unknown/relayed identities cannot mutate or query records.
            return
        self.store.ingest_message(ctx)
        verb, args = command
        own_case = self.rooms_to_cases.get(ctx.room_id)
        parts = args.split()
        result = None
        try:
            if verb == "report" and own_case:
                if ctx.actor_id != self.cases[own_case]["claimant_actor_id"] or ctx.actor_kind != "human":
                    result = {"ok": False, "code": "FORBIDDEN", "message": "Only the enrolled claimant can provide this report.", "data": {}}
                else:
                    report = await self._proposal(ctx.source_event_id + ":draft", "intake", {"text": args})
                    agent = self._agent_context(own_case, ctx, ctx.room_id)
                    result = self.domain.save_report_draft(agent, claim_id=own_case, report=report, source_event_ids=[ctx.source_event_id])
                    if result["ok"]:
                        draft = result["data"]["draft_id"]
                        body = ("### Your report draft\n" + "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in report.items() if v) +
                                f"\n\nPlease check this. Confirm with `findback confirm {draft}` or send a new report.\n\n" + GENERIC_QUESTION +
                                "\nUse `findback evidence <your details>` to record your own assertion.")
                        self.journal.queue(ctx.source_event_id + ":draft-display", ctx.room_id, body, "intake-" + own_case)
            elif verb == "confirm" and own_case:
                result = self.domain.confirm_report(ctx, claim_id=own_case, draft_id=args)
                if result["ok"]:
                    await self._review(ctx, own_case)
            elif verb in {"evidence", "correct-evidence"} and own_case:
                old, text = (args.split(" ", 1) if verb == "correct-evidence" else (None, args))
                result = self.domain.record_evidence(ctx, claim_id=own_case, text=text,
                                                     supersedes_evidence_id=old)
                if result["ok"]:
                    await self._review(ctx, own_case)
            elif verb == "receipt" and own_case:
                result = self.domain.confirm_receipt(ctx, claim_id=own_case, reservation_id=args)
            elif verb in {"status", "audit"}:
                cid = own_case or args
                fn = self.domain.case_view if verb == "status" else self.domain.audit_trail
                result = fn(ctx, claim_id=cid)
            elif verb == "review":
                # Review is a proposal, but requesting one from outside staff is refused.
                permitted = self.domain.staff_comparison(ctx, claim_id=args)
                result = await self._review(ctx, args) if permitted["ok"] else permitted
            elif verb in {"approve", "reject"}:
                cid, iid, review, reason = args.split(" ", 3)
                result = getattr(self.domain, verb)(ctx, claim_id=cid, item_id=iid, review_id=review, reason=reason)
            elif verb in {"collect", "handover", "cancel"}:
                cid, reservation, text = args.split(" ", 2)
                fn, field = {"collect": (self.domain.arrange_collection, "instructions"),
                             "handover": (self.domain.record_handover, "note"),
                             "cancel": (self.domain.cancel_reservation, "reason")}[verb]
                result = fn(ctx, claim_id=cid, reservation_id=reservation, **{field: text})
            elif verb == "ask":
                cid, reason = args.split(" ", 1)
                result = self.domain.request_information(ctx, claim_id=cid, reason=reason)
            elif verb == "register-item":
                iid, raw = args.split(" ", 1)
                item = json.loads(raw)
                result = self.domain.register_item(ctx, item_id=iid, coarse=item["coarse"], private_facts=item["private_facts"])
            elif verb == "correct-item":
                iid, revision, raw = args.split(" ", 2)
                patch = json.loads(raw)
                result = self.domain.correct_item(ctx, item_id=iid, expected_revision=int(revision), **patch)
            elif verb == "help":
                self.journal.queue(ctx.source_event_id + ":help", ctx.room_id, help_text(own_case is None))
                return
            else:
                raise ValueError("Unknown command or wrong room")
        except (ValueError, KeyError, TypeError):
            self.journal.queue(ctx.source_event_id + ":usage", ctx.room_id,
                               "That command could not be read. Use `findback help` for the exact syntax. No action was authorized.")
            return
        if result is not None:
            body = f"**{result['code']}** — {result['message']}"
            if result["ok"] and verb in {"status", "audit"}:
                body += "\n" + render_read(verb, result["data"])
            if result["ok"] and verb in {"approve", "reject", "collect", "handover", "cancel"}:
                compact = {key: value for key, value in result["data"].items() if key in {"reservation_id", "approval_id", "review_id", "receipt_id", "status"}}
                body += "\n```json\n" + json.dumps(compact, indent=2, ensure_ascii=False) + "\n```"
            self.journal.queue(ctx.source_event_id + ":result", ctx.room_id, body)

    async def _send_once(self, key: str, room: str, body: str, speaker: str, thread_id: str | None = None) -> str:
        marker = f"[findback:{key}]"
        client = self.clients[speaker]
        expected_sender = (self.config["agents"][speaker]["actor_id"] if speaker in self.config["agents"]
                           else self.cases[speaker.removeprefix("intake-")]["intake_agent"]["actor_id"])
        # Reconcile a crash after post/before local commit. Read from the trusted
        # service (not the claimant-scoped bot), and fail closed on read errors.
        def find(value):
            if isinstance(value, dict):
                if value.get("sender") == expected_sender and marker in str(value.get("body", value.get("message", ""))):
                    return value.get("id", value.get("event_id", value.get("message_id", "reconciled")))
                for nested in value.values():
                    found = find(nested)
                    if found:
                        return found
            elif isinstance(value, list):
                for nested in value:
                    found = find(nested)
                    if found:
                        return found
            return None
        before = None
        previous_oldest = None
        for _ in range(40):
            context = await self.clients["service"].read_context(room, limit=500, before=before)
            existing = find(context)
            if existing:
                return str(existing)
            if not context.get("truncated"):
                break
            oldest = context.get("oldest_timestamp")
            if not isinstance(oldest, int) or (previous_oldest is not None and oldest >= previous_oldest):
                raise RuntimeError("Cannot safely reconcile message history; operator attention required.")
            previous_oldest = oldest
            # Include the boundary millisecond on the next page. A page full of
            # equal-time messages cannot be skipped safely and fails closed.
            before = datetime.fromtimestamp((oldest + 1) / 1000, timezone.utc).isoformat()
        else:
            raise RuntimeError("Message history exceeded the reconciliation bound.")
        # Mattermost has a post size bound. Keep a readable excerpt; the complete
        # authoritative record remains in SQLite and the operator's case view.
        encoded = body.encode("utf-8")
        if len(encoded) > 12000:
            body = encoded[:11500].decode("utf-8", errors="ignore") + "\n\n[Excerpt: full record retained in FindBack's local audit.]"
        # A zero-width Markdown link title keeps delivery metadata out of the
        # conversation while retaining the exact marker in the stored message.
        suffix = f'\n\n[\u2060](# "{marker}")' if self.config.get("compact_delivery_markers") else "\n\n" + marker
        if thread_id is None:
            result = await client.post_message(room, body + suffix)
        else:
            result = await client.post_message(room, body + suffix, thread_id=thread_id)
        return str(result.get("event_id", result.get("message_id", result.get("id", "posted")))) if isinstance(result, dict) else "posted"

    async def _deliver(self):
        while not self._stop.is_set():
            for row in self.store.pending_outbox():
                try:
                    payload = row["payload"]
                    body = payload.get("text") or payload.get("message") or payload.get("body") or json.dumps(payload, indent=2, ensure_ascii=False)
                    remote = await self._send_once(row["id"], row["destination_room_id"], body, "service")
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
                except Exception:
                    pass  # Unknown outcome remains pending; reconcile before retry.
            await asyncio.sleep(1)


def render_read(verb: str, data: dict) -> str:
    if verb == "audit":
        events = data.get("events", [])
        lines = [f"**Audit • {len(events)} events** (latest 15 shown; full record retained locally)"]
        lines += [f"- `{e['id']}` • {e['action']} → **{e['result_code']}** • {e['occurred_at']} • {e['origin']}"
                  for e in events[-15:]]
        return "\n".join(lines)
    claim = data.get("claim", {})
    lines = [f"### {claim.get('id', 'Case')} • {claim.get('status', 'UNKNOWN')}",
             f"Claim revision {claim.get('revision')} • Evidence revision {claim.get('evidence_revision')}"]
    if claim.get("report"):
        report = claim["report"]
        lines.append("**Confirmed report:** " + "; ".join(f"{k.replace('_', ' ')}: {str(v)[:250]}" for k, v in report.items() if v))
    if data.get("draft"):
        lines.append(f"**Report draft:** `{data['draft']['draft_id']}`")
    for evidence in data.get("evidence", [])[-5:]:
        lines.append(f"**Claimant assertion `{evidence['id']}`:** {evidence['text'][:500]}\nSource `{evidence['source_event_id']}`")
    if data.get("reservation"):
        reservation = data["reservation"]
        lines.append(f"**Reservation:** `{reservation['id']}` • {reservation['status']} • {reservation['item_id']}")
        if reservation.get("collection_instructions"):
            lines.append("**Collection:** " + reservation["collection_instructions"][:750])
    if data.get("return"):
        receipt = data["return"]
        lines.append(f"**Receipt:** `{receipt.get('receipt_id') or 'pending claimant confirmation'}`\nHandover: {receipt.get('handed_over_at')} • Returned: {receipt.get('returned_at') or 'pending'}")
    if "items" in data:
        lines.append("**Staff-only inventory** (up to 8 items)")
        for item in data["items"][:8]:
            lines.append(f"- **{item['id']} r{item['revision']} • {item['status']}**: " + "; ".join(str(fact)[:200] for fact in item["private_facts"][:4]))
        if len(data["items"]) > 8:
            lines.append(f"{len(data['items']) - 8} additional inventory records are retained locally.")
        for review in data.get("reviews", [])[-3:]:
            lines.append(f"Review `{review['review_id']}` • Claim r{review['claim_revision']}, evidence r{review['evidence_revision']} • " + ("stale" if review.get("invalidated_at") else "current basis"))
    return "\n\n".join(lines)


def help_text(staff: bool) -> str:
    if staff:
        return ("**FindBack staff commands**\n"
                "`findback review C001` • `status C001` • `audit C001`\n"
                "`findback approve C001 I-001 <review-id> <reason>`\n"
                "`findback reject C001 I-001 <review-id> <reason>`\n"
                "`findback ask C001 <private reason>`\n"
                "`findback collect C001 <reservation-id> <instructions>`\n"
                "`findback handover C001 <reservation-id> <note>`\n"
                "`findback cancel C001 <reservation-id> <reason>`\n"
                "`findback register-item I-004 {\"coarse\":{...},\"private_facts\":[...]}`\n"
                "`findback correct-item I-001 1 {\"reason\":\"...\",\"private_facts\":[...]}`\n"
                "All commands need the findback prefix. Only your enrolled staff identity can authorize actions.")
    return ("**FindBack claimant commands**\n"
            "`findback report <what you lost, where and when>`\n"
            "`findback confirm <draft-id>`\n"
            "`findback evidence <your identifying details>`\n"
            "`findback correct-evidence <evidence-id> <corrected details>`\n"
            "`findback status` • `findback audit`\n"
            "`findback receipt <reservation-id>` — only after staff records handover.\n"
            "This is a fictional library demonstration. No real physical return is implied.")
