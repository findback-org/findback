"""Real chat/transport verification for a disposable, freshly provisioned stack.

The controlled model mode is deterministic and explicitly not AI inference.
The Codex mode invokes the same constrained production adapter on the host.
Human messages, staff decisions and handover are scripted fictional inputs.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

import httpx

from .cli import enroll
from .model import CodexModel, ModelError
from .runtime import FindBackRuntime
from .verify_access import verify_access
from .verify_restart import snapshot


class E2EVerificationError(RuntimeError):
    """Only a fixed step name is safe to expose, never arbitrary HTTP prose."""


def require(condition, label: str) -> None:
    if not condition:
        raise E2EVerificationError(label)


def source_event_id(config: dict, human: dict, channel: str, post_id: str) -> str:
    material = "\x1f".join([human["client_id"], config["bridge_id"], channel, post_id, "0"])
    return "sw_" + hashlib.sha256(material.encode()).hexdigest()[:32]


def replay_checkpoint(store, service_agent_id: str) -> int:
    """Use a real delivered cursor, never Core's fresh-connection zero sentinel."""
    with store.connection() as db:
        values = [json.loads(row[0]).get("id") for row in db.execute(
            "SELECT frame_json FROM wire_frames WHERE client_id=?", (service_agent_id,))]
    positive = [value for value in values if type(value) is int and value > 0]
    require(bool(positive), "positive_replay_checkpoint_required")
    return min(positive)


class E2EFixtureModel:
    """Canned proposals for the declared headphone-case integration scenario.

    This is intentionally not a general extractor, classifier or quality eval.
    All authoritative decisions still pass through the real domain runtime.
    """
    def __init__(self):
        self.measurements: list[dict] = []

    async def draft(self, text: str) -> dict:
        self.measurements.append({"role": "intake", "seconds": 0,
                                  "behavior": "deterministic E2E fixture; no inference"})
        return {"category": "headphone case", "color": "black", "brand_model": None,
                "lost_place": "library", "lost_at": None, "description": text}

    async def compare(self, data: dict) -> dict:
        self.measurements.append({"role": "matcher", "seconds": 0,
                                  "behavior": "deterministic E2E fixture; no inference"})
        has_assertion = bool(data.get("evidence"))
        return {"summary": "Controlled fixture comparison. The enrolled librarian must decide.",
                "agreements": ["Both coarse candidates are black headphone cases."],
                "contradictions": [],
                "missing_information": [] if has_assertion else ["Independent identifying details"],
                "recommendation": "staff_review" if has_assertion else "insufficient_information"}


class FailFirstDraft:
    """One labelled provider-failure injection before delegating normally."""
    def __init__(self, delegate):
        self.delegate = delegate
        self.failed = False

    @property
    def measurements(self):
        return self.delegate.measurements

    async def draft(self, text):
        if not self.failed:
            self.failed = True
            raise ModelError("Deliberate isolated E2E model-failure injection.")
        return await self.delegate.draft(text)

    async def compare(self, data):
        return await self.delegate.compare(data)


class ChatScenario:
    def __init__(self, config: dict, runtime: FindBackRuntime, report: dict, *, timeout: float = 210):
        self.config, self.runtime, self.report = config, runtime, report
        self.timeout = timeout

    def row(self, sql, values=()):
        with self.runtime.store.connection() as db:
            row = db.execute(sql, values).fetchone()
            return dict(row) if row else None

    def counts(self):
        with self.runtime.store.connection() as db:
            return {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("claims", "reviews", "approvals", "reservations", "returns", "model_proposals")}

    def check(self, name, condition, **details):
        self.report["checks"].append({"name": name, "passed": bool(condition), **details})
        require(condition, name)

    async def wait(self, label, predicate):
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            for task in self.runtime.tasks:
                if task.done():
                    task.result()
                    raise E2EVerificationError("runtime_task_ended")
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.05)
        raise E2EVerificationError("timeout_" + label)

    async def event_result(self, event: str, label: str, *, failure=False):
        inbox = await self.wait(label, lambda: self.row(
            "SELECT state,error FROM inbox WHERE event_id=? AND state IN ('DONE','FAILED')", (event,)))
        require(inbox["state"] == ("FAILED" if failure else "DONE"), label + "_inbox_state")
        result = self.row("SELECT result_json FROM processed_events WHERE source_event_id=?", (event,))
        return json.loads(result["result_json"]) if result else None

    async def post(self, role, text, label, *, failure=False):
        case = self.config["cases"].get(role)
        human = case["human"] if case else self.config["staff"]["human"]
        channel = case["channel_id"] if case else self.config["staff_room"]["channel_id"]
        response = await self.runtime.http.post(self.config["mattermost_url"] + "/api/v4/posts",
            headers={"Authorization": "Bearer " + human["token"]},
            json={"channel_id": channel, "message": text})
        require(response.status_code == 201, label + "_post_status")
        post_id = response.json().get("id")
        require(isinstance(post_id, str) and bool(post_id), label + "_post_id")
        event = source_event_id(self.config, human, channel, post_id)
        result = await self.event_result(event, label, failure=failure)
        self.report["events"].append({"label": label, "role": role,
            "input": "scripted fictional authenticated Mattermost post",
            "mattermost_post_id": post_id, "switch_event_id": event,
            "result_code": result["code"] if result else "MODEL_FAILURE_INJECTED" if failure else "PROPOSAL_OR_READ"})
        return event, result

    async def expect(self, role, text, label, code):
        event, result = await self.post(role, text, label)
        self.check(label, result is not None and result["code"] == code,
                   expected=code, observed=result["code"] if result else None)
        return event, result

    async def drain(self):
        await self.wait("domain_deliveries", lambda: not self.row("SELECT id FROM outbox WHERE delivery_state!='SENT'"))
        await self.wait("runtime_deliveries", lambda: not self.row("SELECT id FROM runtime_messages WHERE state!='SENT'"))

    async def messages(self, role):
        case = self.config["cases"][role]
        response = await self.runtime.http.get(
            self.config["mattermost_url"] + f"/api/v4/channels/{case['channel_id']}/posts",
            params={"per_page": 200}, headers={"Authorization": "Bearer " + case["human"]["token"]})
        require(response.status_code == 200, "read_back_claimant_posts")
        return list(response.json()["posts"].values())


def validate_fresh_config(config: dict, state_dir: Path) -> None:
    """Refuse accidental use against the long-lived native demo enrollment."""
    marker = config.get("e2e_stack", {})
    require(marker.get("fresh") is True and
            str(marker.get("project_name", "")).startswith("findback-e2e-"), "fresh_stack_marker_required")
    require(not (state_dir / "findback.db").exists(), "fresh_findback_database_required")
    require(set(config["cases"]) == {"C001", "C002"}, "expected_isolated_cases")


async def run_scenario(config: dict, state_dir: Path, model_mode: str = "fixture") -> dict:
    """Run actual Switch/MM lifecycle checks; return a safe report even on failure.

    This function never provisions, deletes or repairs external stack resources.
    The stack owner controls those resources and must guarantee final teardown.
    """
    require(model_mode in {"fixture", "codex"}, "unsupported_model_mode")
    state_dir = Path(state_dir)
    validate_fresh_config(config, state_dir)
    report = {"passed": False, "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "behavior": {"transport": "live isolated Switch Core and Mattermost",
            "model": "real host Codex CLI" if model_mode == "codex" else "deterministic in-process proposals; no AI inference",
            "human_input": "scripted fictional accounts", "physical_handover": "simulated; no property moved"},
        "model_mode": model_mode, "checks": [], "events": [],
        "fault_injection": "one intentional ModelError, explicit inbox retry, and a rewound owned resume cursor to represent a lost ACK"}
    delegate = CodexModel() if model_mode == "codex" else E2EFixtureModel()
    model = FailFirstDraft(delegate)
    enroll(config, state_dir)
    try:
        async with FindBackRuntime(config, state_dir, model=model) as runtime:
            run = ChatScenario(config, runtime, report)
            run.check("fresh_domain", run.counts()["returns"] == 0 and run.counts()["model_proposals"] == 0)
            resume_path = state_dir / "connection-service.json"
            initial_cursor = json.loads(resume_path.read_text())["cursor"]
            access = await asyncio.to_thread(verify_access, state_dir)
            report["access_checks"] = access
            run.check("actual_room_access_boundaries", access["passed"], total=len(access["checks"]))

            failed_event, _ = await run.post("C002", "findback report I lost a black headphone case in the library.",
                                             "injected_model_failure", failure=True)
            run.check("failed_proposal_does_not_release", run.counts()["approvals"] == 0 and run.counts()["reservations"] == 0)
            with runtime.store.transaction() as db:
                retried = db.execute("UPDATE inbox SET state='PENDING',error=NULL WHERE event_id=? AND state='FAILED' AND error='MODEL_UNAVAILABLE'", (failed_event,)).rowcount
            run.check("explicit_inbox_recovery", retried == 1)
            await run.event_result(failed_event, "retried_original_input")
            run.check("retried_input_produces_draft", bool(run.row("SELECT current_draft_id FROM claims WHERE id='C002'")["current_draft_id"]))

            await run.post("C001", "findback report I lost my black headphone case at the library this afternoon.", "claimant_report")
            draft = run.row("SELECT current_draft_id FROM claims WHERE id='C001'")["current_draft_id"]
            await run.post("C001", "findback confirm " + draft, "claimant_confirms_draft")
            first = run.row("SELECT * FROM reviews WHERE claim_id='C001' ORDER BY rowid DESC LIMIT 1")
            run.check("broad_description_retains_two_candidates", len(json.loads(first["deterministic_comparison_json"])["candidates"]) == 2)

            await run.post("C002", "findback report I lost a black headphone case in the library. I cannot remember its identifying marks.", "second_claimant_report")
            draft = run.row("SELECT current_draft_id FROM claims WHERE id='C002'")["current_draft_id"]
            await run.post("C002", "findback confirm " + draft, "second_claimant_confirmation")
            await run.post("staff", "findback ask C002 Both cases share a broad description; independent identifying evidence is still needed.", "staff_requests_missing_evidence")
            await run.expect("C001", f"findback approve C001 I-001 {first['id']} I approve myself.", "claimant_cannot_approve", "FORBIDDEN")
            agent_post = await runtime.clients["matcher"].post_message(config["staff_room"]["id"],
                f"findback approve C001 I-001 {first['id']} A scripted agent tries to approve.")
            agent_event = agent_post.get("event_id", agent_post.get("message_id", agent_post.get("id")))
            require(isinstance(agent_event, str), "agent_post_event_id")
            agent_result = await run.event_result(agent_event, "agent_cannot_approve")
            run.check("agent_cannot_approve", agent_result is not None and agent_result["code"] == "FORBIDDEN")

            await run.post("C001", "findback evidence Mine has a green triangle sticker under the lid and a short blue charging cable inside.", "claimant_identifying_assertion")
            review = run.row("SELECT * FROM reviews WHERE claim_id='C001' ORDER BY rowid DESC LIMIT 1")
            await run.expect("staff", f"findback approve C001 I-001 {first['id']} Attempting the old comparison.", "stale_review_refused", "STALE_REVISION")
            _, reserved = await run.expect("staff", f"findback approve C001 I-001 {review['id']} Both independently stated details agree with the desk record.", "staff_approves_exact_review", "RESERVED")
            reservation = reserved["data"]["reservation_id"]
            await run.post("staff", f"findback collect C001 {reservation} Fictional pickup at Library Desk A; ask Mira and show your private claim.", "private_collection_instructions")
            run.check("collection_is_not_return", run.row("SELECT status FROM claims WHERE id='C001'")["status"] == "APPROVED" and run.counts()["returns"] == 0)
            await run.expect("C001", f"findback receipt {reservation}", "premature_receipt_refused", "HANDOVER_REQUIRED")
            await run.post("staff", f"findback handover C001 {reservation} Scripted fictional handover; no physical object moved.", "scripted_staff_handover")
            _, returned = await run.expect("C001", f"findback receipt {reservation}", "scripted_claimant_receipt", "RETURNED")
            _, repeated = await run.expect("C001", f"findback receipt {reservation}", "second_receipt_deduplicates", "RETURNED")
            run.check("one_return_receipt", returned["data"]["receipt_id"] == repeated["data"]["receipt_id"] and run.counts()["returns"] == 1 and run.counts()["reservations"] == 1)
            await run.drain()
            second_text = "\n".join(post["message"] for post in await run.messages("C002")).lower()
            run.check("other_claimant_does_not_receive_private_inventory", not any(
                secret in second_text for secret in ("green triangle", "blue charging cable", "red thread", "initials rk")))
            before = snapshot(runtime.store)
            run.check("other_claim_remains_unresolved", run.row("SELECT status FROM claims WHERE id='C002'")["status"] == "NEEDS_INFO")
            final_cursor = json.loads(resume_path.read_text())["cursor"]
            replay_cursor = replay_checkpoint(runtime.store, config["agents"]["service"]["id"])
            with runtime.store.connection() as db:
                prior_inbox_ids = {row[0] for row in db.execute("SELECT event_id FROM inbox")}
                work_before_replay = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                      for table in ("processed_events", "model_proposals", "outbox", "runtime_messages")}
            report["totals"] = run.counts()

        # Deliberately represent a crash after local capture but before the last
        # resume-state ACK. Only this disposable stack's service cursor is changed.
        saved = json.loads(resume_path.read_text())
        require(type(final_cursor) is int and final_cursor > replay_cursor,
                "replay_cursor_progress")
        saved["cursor"] = replay_cursor
        resume_path.write_text(json.dumps(saved))
        resume_path.chmod(0o600)
        async with FindBackRuntime(config, state_dir, model=model) as restarted:
            run = ChatScenario(config, restarted, report)
            await run.wait("actual_switch_replay_ack", lambda: restarted.clients["service"].cursor is not None and restarted.clients["service"].cursor >= final_cursor)
            with restarted.store.connection() as db:
                inbox_ids = [row[0] for row in db.execute("SELECT event_id FROM inbox")]
                work_after_replay = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                                     for table in work_before_replay}
            # New notification frames can legitimately finish arriving after
            # the old runner closes. They must not invalidate deduplication of
            # the prior inputs, nor rerun a model or create another notice.
            run.check("replay_preserves_unique_prior_inputs", prior_inbox_ids <= set(inbox_ids) and len(inbox_ids) == len(set(inbox_ids)))
            run.check("replay_does_not_repeat_models_or_notices", work_after_replay == work_before_replay)
            run.check("restart_and_transport_replay_preserve_domain", snapshot(restarted.store) == before)
            status_event, _ = await run.post("C001", "findback status", "status_after_restart")
            await run.drain()
            marker = f"[findback:{status_event}:result]"
            delivered = [post for post in await run.messages("C001") if marker in post["message"]]
            run.check("one_actual_status_reply_after_restart", len(delivered) == 1 and "RETURNED" in delivered[0]["message"])
            run.check("restart_status_does_not_change_domain", snapshot(restarted.store) == before)
            report["replay"] = {"simulated_lost_ack": True, "actual_server_replay": True,
                                "initial_connection_cursor": initial_cursor,
                                "requested_cursor": replay_cursor, "previous_ack_cursor": final_cursor,
                                "domain_digest_unchanged": True}
        report["model_measurements"] = model.measurements
        report["successful_model_calls"] = len(model.measurements) if model_mode == "codex" else 0
        report["controlled_proposal_calls"] = len(model.measurements) if model_mode == "fixture" else 0
        report["passed"] = all(check["passed"] for check in report["checks"])
    except Exception as error:
        report["error_code"] = type(error).__name__
        if isinstance(error, E2EVerificationError):
            report["failed_step"] = str(error)
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    return report
