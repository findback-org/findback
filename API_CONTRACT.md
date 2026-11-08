# FindBack domain contract

Implemented domain contract for `src/findback/domain.py`, `storage.py`, `matching.py`, and `fixtures.py`. Domain tests run without Switch/model credentials; live transport and identity verification are separate runtime checks owned by the integration layer.

## Construction and trusted actor context

- `SQLiteStore(db_path)` creates/migrates schema; each mutation uses a fresh connection with foreign keys, a busy timeout and `BEGIN IMMEDIATE`.
- `DomainService(store, policy)` is synchronous and transport-independent.
- `ActorContext` is a frozen Python dataclass: `actor_id`, `actor_kind` (`human` or `agent`), `room_id`, `source_event_id`, `source_text`, `occurred_at`, `origin` (`switch` or `fixture`), optional `thread_id`.
- Actor IDs and kinds come from authenticated Switch envelopes. They never come from an LLM's function arguments, quoted message, display name, report text or `approved_by` field. Do not expose construction of ActorContext as an agent tool. The transport creates it outside model control and parses human commands deterministically from the actual message text.
- `policy`/enrollment maps stable staff IDs to the fixed staff room and each case to its claimant actor, private case room and optional Intake agent IDs. Matcher agents are enrolled for the staff room. Roles/scopes are looked up from enrollment; there is no caller-supplied `role` parameter.
- A human staff command must originate from the configured staff room. Claimant confirmation/evidence/receipt must originate from that claimant's exact case room. Agent identity cannot acquire staff authority even if its name matches staff.
- Fixture mode uses explicitly enrolled fictional actors and `origin=fixture`; results/audit must retain that origin and never claim authenticated live traffic.

## IDs and result shape

Fixture cases are `C001` (Priya) and `C002` (Alex). Fixture item IDs are `I-001`, `I-002`, `I-003`. Runtime drafts/reviews/reservations/audit/receipt IDs are generated opaque identifiers with readable prefixes (`D-`, `R-`, `RS-`, `A-`, `RET-`); clients must not derive meaning from them.

Every public method returns a JSON-safe dictionary, never raw sqlite rows:

```
{
  "ok": true,
  "code": "RESERVED",
  "message": "Item reserved; collection is pending.",
  "data": { ... },
  "audit_id": "A-...",   // null for a successful read
  "replayed": false
}
```

A refusal has `ok:false`, a stable code, safe plain-language message and `data:{}` unless a harmless current-state hint is explicitly authorized. Denied mutations receive an audit ID. Expected user errors are results; unexpected I/O/programming failures raise exceptions and roll back.

Stable refusal codes include `FORBIDDEN`, `NOT_FOUND`, `INVALID_INPUT`, `STALE_REVISION`, `REPLAY_MISMATCH`, `REPORT_UNCONFIRMED`, `REVIEW_REQUIRED`, `ITEM_UNAVAILABLE`, `RESERVATION_ACTIVE`, `INVALID_STATE`, `HANDOVER_REQUIRED`, `HANDOVER_ALREADY_RECORDED`, `TERMINAL_RECORD`. An unauthorized caller must not learn whether a hidden case/item exists.

## Deduplication and transaction semantics

Each mutation fingerprints its operation name and canonical JSON arguments, excluding delivery metadata. `source_event_id` is globally unique in `processed_events`.

- Same event ID + same operation/arguments/actor/room/origin/source text returns the saved original result with `replayed:true`; no new audit/state/outbox/return. Cached successful results are returned only if the actor remains authorized under current enrollment. If that authorization has been revoked, return an audited `FORBIDDEN` with empty data without altering the original dedup record. Cached refusals remain deduplicated.
- Same event ID + different operation/arguments returns `REPLAY_MISMATCH`, never reinterprets the original message. The original outcome remains unchanged. This mismatch can be logged separately without replacing the dedup record.
- A rejected command is also deduplicated and audited.
- Every authoritative state change, its audit, dedup result and notification outbox entries commit in one transaction.
- One source message drives one canonical domain mutation. The command handler must not split a message into unrelated mutation calls with the same event ID. Compound operations such as approval+reservation are one method/transaction.

## Public methods

All arguments after `ctx` are keyword-only. IDs/revisions displayed to a person come from authorized views. Root command grammar can use kebab-case verbs while invoking these snake_case methods.

### Bootstrap/enrollment (operator only, not an LLM tool)

`store.configure_enrollment(staff_room_id, staff_actor_ids, matcher_agent_ids, cases)`

`cases` contains `{claim_id, claimant_actor_id, case_room_id, intake_agent_ids}`. Explicit setup only; repeated identical setup is harmless. Changes to enrollment are operator configuration, not authority inferred from chat. Case records are created OPEN with no confirmed report. Fixture loading supplies the exact three fictional inventory records without pretending a human registered them.

### Report and evidence

- `save_report_draft(ctx, claim_id, report, source_event_ids)` — enrolled Intake for that case, or its claimant; the report is a proposed coarse structured description. It does not become canonical merely because an agent wrote it. Returns `draft_id`, `draft_revision`, proposed fields and a confirmation prompt.
- `confirm_report(ctx, claim_id, draft_id)` — enrolled human claimant in their case room only; confirms that exact current draft. Increments the claim's basis revision, invalidates earlier reviews, and leaves the case NEEDS_INFO until compared/reviewed. No source text is relabeled as established truth.
- `record_evidence(ctx, claim_id, text, attachment_ref=None, supersedes_evidence_id=None)` — enrolled human claimant in their case room only. The transport passes their actual text/approved attachment reference; an LLM cannot call this as if it were the claimant. Stores immutable assertion/source provenance, increments claim/evidence revisions and invalidates earlier reviews. A correction appends evidence and points to the superseded record; it does not erase history.
- `request_information(ctx, claim_id, reason)` — staff human or enrolled Matcher in staff room; moves an unreserved nonterminal case to NEEDS_INFO and emits a non-leading generic request. The claimant-facing outbox must not copy a private staff comparison or expected identifying values. Staff-only `reason` remains in staff view.

Confirmed report fields: `category`, `color`, `brand_model` (nullable), `lost_place`, `lost_at` (nullable ISO timestamp/range), `description`. Unknown fields stay null. Additional fields are rejected, not silently persisted as potential secret output. Raw source text remains separate from extracted fields.

### Inventory and comparison

- `register_item(ctx, item_id, coarse, private_facts)` — enrolled staff human in staff room. `coarse` includes category/color/brand_model/found_place/found_at; private_facts is separate staff-only text/list. New item AVAILABLE, revision1.
- `correct_item(ctx, item_id, expected_revision, coarse_patch=None, private_facts=None, reason)` — staff human; requires current revision and no active reservation/recorded handover. Increments item basis revision and invalidates reviews referencing it.
- `candidates(ctx, claim_id)` — authorized claimant/Intake receives only coarse fields and simple agreement/missing facts; authorized staff/Matcher may also inspect staff inventory through `staff_comparison`. No percentage ownership probability; no private-value matching on the claimant path.
- `prepare_review(ctx, claim_id, item_ids, analysis=None, expected_basis=None)` — enrolled Matcher or staff human in staff room. Requires a confirmed report. Stores an immutable snapshot of current claim/evidence/item basis revisions, deterministic coarse candidates, source evidence and optional model-authored analysis. `expected_basis` defaults to `None` for a synchronous staff proposal; a model-backed caller must provide the revisions it actually sent to inference. The service checks them after acquiring the write transaction and refuses `STALE_REVISION` if any selected basis changed. Known unavailable items may appear in a staff comparison, but approval still requires current availability. Marks UNDER_REVIEW. Returns `review_id` and exact revisions. Model analysis is explicitly labeled a proposal, not staff verification.
- `staff_comparison(ctx, claim_id)` — staff/Matcher in staff room only. Returns full inventory facts, all claimant assertions with source identities/event IDs, current candidates, exact revisions, existing review snapshots and missing information. This is the only broad comparison read.

### Decisions, reservation and completion

- `approve(ctx, claim_id, item_id, review_id, reason)` — staff human in staff room only. Review must reference the selected item and match current claim/evidence/item revisions. Requires confirmed report and a current UNDER_REVIEW/NEEDS_INFO eligible case. Staff's reason records their ownership judgment; the model score never approves. In one transaction: verify authority and revisions, item AVAILABLE, no active claim reservation; create approval and unique active reservation; item RESERVED; claim APPROVED; private collection-pending receipt. Return `reservation_id`, approval/review IDs and revised authorized view.
- `reject(ctx, claim_id, item_id, review_id, reason)` — staff human in staff room only; same exact-review validation, no item reservation; claim REJECTED. Insufficient detail should normally use request_information rather than rejection or fraud language.
- `arrange_collection(ctx, claim_id, reservation_id, instructions)` — staff human in staff room; current active reservation required; stores private collection instructions. Claim remains APPROVED and item RESERVED.
- `cancel_reservation(ctx, claim_id, reservation_id, reason)` — staff human in staff room; only before recorded handover. Cancels that exact reservation, invalidates its approval, item AVAILABLE, claim NEEDS_INFO, prior reviews invalidated. It does not close the claim or reinterpret later receipt messages. This explicit step is required before correcting a reserved basis.
- `record_handover(ctx, claim_id, reservation_id, note)` — staff human in staff room; exact current active reservation/valid approval required. Stores one handover event, claim HANDOVER_RECORDED; item remains RESERVED until claimant receipt. Reservation becomes HANDOVER_RECORDED and still excludes competing reservations.
- `confirm_receipt(ctx, claim_id, reservation_id)` — enrolled human claimant in their own room only. Requires the same reservation's staff handover. Creates one immutable return receipt, reservation RETURNED, item RETURNED and claim RETURNED. A new message after completed receipt returns the existing receipt without creating a second return/outbox. An old reservation ID cannot close a new reservation.

### Reads and outbox

- `case_view(ctx, claim_id)` — own claimant/Intake in case room gets redacted view; staff/Matcher in staff room gets staff view. No arbitrary room routing argument.
- `audit_trail(ctx, claim_id)` — staff/Matcher gets full case audit; claimant gets only safe lifecycle events concerning their own case, never other claims or private rejection/comparison details.
- `store.pending_outbox(limit=100)` — internal transport call, not agent tool; returns pending destination IDs and already-renderable safe payloads.
- `store.mark_outbox_sent(outbox_id, remote_event_id)` / `store.mark_outbox_failed(outbox_id, error)` — internal delivery bookkeeping; no case-state mutation.

## Basis revisions and state versions

`claim.revision` changes when human-confirmed report/evidence changes. `claim.evidence_revision` increments for new/corrected evidence. `item.revision` changes when item facts change. Approval snapshots these basis revisions. Status transitions do not invalidate their own approval; separate `state_version` fields count lifecycle transitions for audit/concurrency.

Reviews are immutable snapshots; `invalidated_at/reason` records staleness. A current item fact correction invalidates any referencing review. New evidence/draft confirmation invalidates existing claim reviews. An unconfirmed new draft does not silently change confirmed facts.

### Reservation freeze rule

When a case/item has an ACTIVE or HANDOVER_RECORDED reservation, report confirmation, evidence-basis additions/corrections and item-fact corrections return `RESERVATION_ACTIVE`. The incoming raw transport message can remain in the inbox/audit, but cannot change approved facts. Staff must explicitly cancel before changes, then obtain a fresh review/approval. Cancellation after handover is refused: the item must never become AVAILABLE while it may physically be with the claimant. Final RETURNED records are immutable in MVP; exceptional reconciliation is manual and documented.

## SQLite records

- `principals` / `case_bindings` / `settings`: enrolled IDs, kinds/scopes, fixed room bindings.
- `items`: id, coarse_json, private_facts_json, status, revision, state_version, timestamps.
- `claims`: id, claimant_actor_id, case_room_id, confirmed_report_json, current_draft_id, status, revision, evidence_revision, state_version, selected_item_id.
- `drafts`: id, claim_id, fields_json, source_event_ids_json, created_by, source_kind, draft_revision, confirmed_at.
- `evidence`: id, claim_id, text, attachment_ref, actor_id/kind, source_event_id, origin, occurred_at, supersedes_id; immutable.
- `reviews`: id, claim_id/revision/evidence_revision, item_revisions_json, deterministic_comparison_json, proposed_analysis_json, authored_by, source_event_id, created_at, invalidated_at/reason.
- `approvals`: id, claim_id/revision/evidence_revision, item_id/revision, review_id, staff_actor_id, source_event_id, reason, status, created_at.
- `reservations`: id, approval_id, claim_id, item_id, status, collection_instructions, created_at/cancelled_at/reason.
- `returns`: reservation_id UNIQUE, staff_handover_event_id UNIQUE, staff_actor_id, handed_over_at/note, claimant_receipt_event_id UNIQUE nullable, claimant_actor_id nullable, receipt_id UNIQUE nullable, returned_at nullable.
- `audit_events`: id, source_event_id, origin, actor_id/kind, room_id, action, affected IDs/revisions, previous/next state, result_code, safe/privileged details, occurred_at.
- `processed_events`: source_event_id PRIMARY KEY, request_fingerprint, result_json, processed_at.
- `outbox`: id, source_event_id, topic, destination_room_id, visibility, payload_json, delivery_state, attempts, remote_event_id, last_error, created_at/sent_at.

Partial UNIQUE indexes exclude double reservation per item AND per claim while reservation status is ACTIVE or HANDOVER_RECORDED. Foreign keys bind receipt→reservation→approval→review. All reservation state checks and inserts happen under the same transaction, including separate-process/connection races.

## Redaction and output contracts

Claimant/Intake views include their own confirmed report and source assertions, lifecycle status, coarse candidate count/list, generic question, and their own collection instructions/receipt when authorized. They exclude all private item facts, other claimant IDs/reports/evidence, full staff comparison/model analysis, staff-only reasons and detailed authorization denial logs.

Staff views can contain private facts and claimant evidence, but outbound staff→claimant notices are constructed from an explicit allowlist. Never redact by merely asking an LLM to summarize confidential data. Outbox destinations derive from enrollment and operation type, never model-provided arbitrary IDs. Generic evidence question: “What marks, contents, or accessories would help staff identify yours?”

Outbox state changes and dedup are durable, but exactly-once network posting is not promised by SQLite. Root must use verified platform idempotency if available, or stable FindBack markers plus reconciliation before retry. A crash after posting and before saving remote_event_id otherwise creates an at-least-once delivery window; this is distinct from exactly-once reservation/return state.

Implementation clarification: model-backed review callers pass `expected_basis={claim_revision,evidence_revision,item_revisions}` from the data sent to inference. Domain checks that basis atomically before storing the analysis, refusing `STALE_REVISION` if records changed during the model call. Cached mutation replay rechecks current enrollment before returning a saved privileged result.


## Model comparison revision guard

`expected_basis` is an optional JSON-safe dictionary with exactly these three keys:

```json
{
  "claim_revision": 2,
  "evidence_revision": 1,
  "item_revisions": {"I-001": 1, "I-002": 1}
}
```

Capture these values from the staff comparison before inference. The item map may contain additional known items, but must contain a matching current revision for every item selected for the new review. The claim and evidence revisions must match exactly. A refused stale comparison stores no review or notification and does not change the case state; its refusal/audit/dedup result is durable. The caller must obtain a fresh snapshot and new comparison under a new source/job event, rather than relabeling the old answer as current.

`tests/test_revision_guard.py` covers evidence and item changes after the model-input snapshot, a review waiting behind a separate SQLite write transaction, and replay of a previously successful restricted response after staff revocation. These tests use fictional actor contexts and local SQLite; they do not establish live Switch authentication or actual ownership.
