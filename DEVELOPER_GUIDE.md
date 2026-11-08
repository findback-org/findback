# FindBack developer guide

FindBack coordinates private lost-property recovery through Switch and Mattermost. Intake helps an enrolled claimant confirm a report; Matcher prepares a staff comparison. The enrolled custodian approves the exact reviewed item. A return closes only after staff handover and claimant receipt.

For the portable, isolated verification stack, start with the [README](README.md#run-locally) and [fresh-stack guide](docs/FRESH_STACK_E2E.md). The interactive setup below targets an existing **native macOS Switch Console installation with its bundled Mattermost**; it is not a general remote-server installer.

## Run locally

Requirements: Python 3.11+ (3.12 recommended), [uv](https://docs.astral.sh/uv/), a running native Switch Console server, and an authenticated `codex` CLI on `PATH`.

From the repository root:

```sh
uv sync --locked --extra dev --python 3.12
uv run findback doctor
uv run findback bootstrap
uv run findback run
```

Bootstrap creates three private rooms in the existing local Switch tenant, four nonadmin agent owners, four application agent identities, and fictional Mattermost accounts. It reuses existing enrollment and workflow history without resetting a progressed case.

Private credentials and runtime state live in ignored `.findback/` files: the directory uses mode `0700`, and configuration uses `0600`. Setup reads local Switch administrative configuration to provision accounts and resolve immutable identities. The running service does not receive Core/database administrator credentials. The local operator and Switch/Mattermost administrators remain trusted.

The model provider is the authenticated host `codex` CLI; no additional API key is required. Calls use fresh sessions, structured output and disabled tools. Intake receives the claimant's report text; Matcher receives a staff-side snapshot. Models cannot invoke domain operations. This is a constrained application adapter, not an operating-system security sandbox. [The recorded constraint check](evidence/model-constraints-check.json) documents CLI compatibility.

## Use the chat rooms

Open the Mattermost address from your local setup. Private configuration contains each fictional participant's credentials and room URL. Use separate browser sessions for the claimant and custodian roles. These are distinct authenticated accounts, not display-name roles.

Every command below begins a message with `findback`. Quoted commands, prose describing a command, and attachments do not authorize actions. These commands belong to the three-room developer fixture; the campus showcase also supports the natural phrases and reply threads shown in its recorded conversations.

| Claimant command | Result |
| --- | --- |
| `findback report I lost my black headphone case at the library this afternoon.` | Intake proposes a structured draft. |
| `findback confirm <draft-id>` | Confirms that exact draft and prepares the staff comparison. |
| `findback evidence Mine has a green triangle under the lid and a short blue cable inside.` | Preserves the claimant's assertion and authenticated source; requests a fresh comparison. |
| `findback correct-evidence <evidence-id> <corrected text>` | Appends a correction and invalidates an old review. |
| `findback status` / `findback audit` | Reads only this claimant's case. |
| `findback receipt <reservation-id>` | Closes that claimant's current reservation only after staff records handover. |

| Staff command | Result |
| --- | --- |
| `findback review C001` | Generates a staff-only comparison for exact revisions. |
| `findback approve C001 I-001 <review-id> <reason>` | Records the human decision and reserves the item atomically. |
| `findback reject C001 I-001 <review-id> <reason>` | Rejects the proposed match without a reservation. |
| `findback ask C002 The broad description fits two items.` | Saves the staff reason privately and sends a generic, non-leading question. |
| `findback collect C001 <reservation-id> Library Desk A at 17:00.` | Delivers private collection instructions; the item remains unreturned. |
| `findback handover C001 <reservation-id> Fictional demo handover.` | Records staff handover; claimant receipt remains pending. |
| `findback cancel C001 <reservation-id> More evidence is needed.` | Cancels before handover and requires a fresh review. |
| `findback status C001` / `findback audit C001` | Reads staff-only records. |
| `findback help` | Shows command syntax, including inventory registration and correction. |

The fixture inventory contains two distinguishable black cases (`I-001`, `I-002`) and one blue bottle (`I-003`). Their identifying facts belong in staff views. A broad black-case report yields two candidates; similarity alone does not establish ownership.

## Separate sessions and campus showcase

To create a separate developer fixture without replacing an existing case history:

```sh
uv run findback --state-dir .findback/session-2 bootstrap
uv run findback --state-dir .findback/session-2 run
```

Keep the runtime listening, then use the accounts enrolled in that session. An optional scripted scenario is available through:

```sh
uv run python -m findback.live_demo \
  --state-dir .findback/session-2 --scripted-roleplay
```

To create a separate campus conversation, replace the viewer placeholder with your local Mattermost username:

```sh
uv run python -m findback.showcase \
  --viewer-username YOUR_MATTERMOST_USERNAME --pace 0.7
```

This prints the actual room URL, sends fictional participant messages through authenticated accounts, and leaves the agents listening. It preserves earlier conversations and receipts. The designated demo observer can view the showcase rooms; that viewing exception is disclosed in their notices. Neither scripted messages nor a `RETURNED` record establish that real people participated independently or that a physical object changed hands.

## Verify

```sh
uv run pytest -q
uv run findback offline-demo
uv run findback status C001
```

The offline demo uses a separate newly named database, simulated actors and controlled model analysis. It exercises the domain state machine, including refused claimant approval, premature receipt and duplicate receipt. It does not establish live transport or inference.

[EVIDENCE.md](EVIDENCE.md) distinguishes recorded automated tests, live Switch/model runs, and scripted fictional participants. [Fresh-stack verification](docs/FRESH_STACK_E2E.md) provides an independent setup; [evidence evaluation](docs/EVIDENCE_EVALUATION.md) measures model outputs separately from action authority.

If a model call or message fails, its original event remains retained. Inspect the error before using `findback retry-failed` to requeue interrupted inbox work. A restart resumes durable cursors, pending work, reservations and notices. Use the [operations guide](docs/OPERATIONS.md) for queue health, delivery retries and recovery boundaries.

## Authorization and persistence

- Private Mattermost channels and Switch room visibility separate claimant and staff contexts. Each case has its own Intake identity; Matcher belongs to the staff room. The deterministic service is enrolled across the workflow rooms.
- The runtime verifies the enrolled Switch sender and underlying immutable Mattermost account/post. Display names, quoted text, model-provided roles and delegated approvals do not grant authority.
- Privileged human commands pass deterministic authorization. Model outputs can draft reports and explain comparisons; they cannot construct trusted human identities.
- SQLite transactions bind approval to claim/evidence/item revisions and enforce one active reservation per case and item. Domain changes, audit, deduplication and notices commit together.
- Approval evidence stays frozen while reserved. Staff must cancel and request a fresh review before changing it; cancellation after handover is refused.
- Events are persisted before transport acknowledgement. Model inference does not block the heartbeat reader. Uncertain outbound sends are reconciled against trusted posting identities before retry; incomplete source history fails closed. Network-wide exactly-once posting is not claimed.

See the [domain contract](API_CONTRACT.md) for method signatures, revision rules and response shapes.

## Current limits

The build uses text-based intake, enrolled claimants, fictional inventory and a local Mattermost bridge. Historical photo retrieval, multilingual intake, general public enrollment, reminders, inventory withdrawal, whole-case cancellation and exceptional reconciliation after physical handover remain deferred. Staff owns the ownership decision; no real-world accuracy, fraud-detection or time-saving claim has been measured.

One runtime owns each state directory. Commands whose source cannot be verified fail closed. Keep private configuration, database files, native Switch settings and real claimant data out of public repositories and evidence artifacts.
