# FindBack

Recover a lost possession through private Switch rooms. Intake helps a claimant describe it, Matcher compares evidence for staff, and an enrolled custodian decides. The service closes a return only after **staff handover and that claimant's receipt**.

**Start with the actual messaging-app demo:** [Campus • Lost & Found](http://localhost:57898/switch/channels/findback-campus-4f73ef6b). It contains 12 distinct fictional people, four claims, six real Switch agent identities, and the complete conversations. The local `@krishna` account is already enrolled as a demo viewer across the community, private cases and review desk.

The current campus conversation contains **46 authenticated human-account posts and 12 live Codex calls**. Priya, Maya and Leo have recorded returns; Alex is still gathering evidence. Each fictional participant posts from a distinct account. [Run and access evidence](evidence/live-campus/verification.json) distinguish the simulation from the live transport and AI. The genuine paperback/book exception and Mira's fresh review are retained in the history.

[Watch the actual Mattermost screens](evidence/live-campus/mattermost-demo.mp4) — a 71-second edited screen-captured walkthrough. It uses unchanged browser captures, with navigation/loading gaps trimmed. [Capture details](evidence/live-campus/CAPTURE.md) record the edits and source hashes.

To watch a **fresh** campus conversation unfold, use one command and open the printed Mattermost link:

```sh
.venv/bin/python -m findback.showcase --viewer-username krishna --pace 0.7
```

This creates a separate session and leaves its real agents listening after the story. It preserves previous conversations and receipts. Natural confirmation, staff decisions and receipt replies bind to their exact message cards; participants do not copy database IDs into chat.

The demo uses fictional people and inventory. A `RETURNED` database record in this demo does not establish that a physical object moved.

Verified: **154 automated tests**, **94 campus access checks**, the live multi-person campus story, actual threaded messages and agent-approval refusal, and service restart. See [the evidence](EVIDENCE.md). The older text-only playback is retained as a historical artifact; the primary demo is now the actual Mattermost conversation above.

## Run locally

From the repository root:

```sh
uv sync --locked --extra dev --python 3.12
.venv/bin/findback doctor
.venv/bin/findback bootstrap
.venv/bin/findback run
```

Start the native local server in Switch Console first, with its bundled Mattermost. The bootstrap targets this Mac installation; it is not a general remote-server installer. It creates three private rooms in the existing local Switch tenant, four nonadmin agent owners, four application agent identities, and fictional Mattermost accounts. Existing resources and workflow history are reused. It never resets an existing case.

The model provider is the already signed-in `codex` CLI. No additional API key is required. Calls use fresh sessions, structured output, disabled shell/exec/search/skill discovery, and no Switch tools. Intake receives only its claimant's report text; Matcher receives a staff-only snapshot. Models cannot invoke domain operations. This is a constrained application adapter, not an unrestricted autonomous coding agent or an OS security sandbox.

The installed CLI's apps, plugins, browser, computer, image, multi-agent and hook surfaces are explicitly disabled too. Only structured response/reasoning items are accepted; startup diagnostics are counted without relaying their text. [The harmless live constraint check](evidence/model-constraints-check.json) verifies CLI compatibility.

Private setup credentials and runtime state live in `.findback/` (directory 0700, configuration 0600), which is ignored by Git. The setup process reads local Switch administrative configuration solely to provision accounts and resolve immutable identities. The running service does not receive Core admin/DB credentials. The local operator and Switch/Mattermost administrators remain trusted.

## Use the chat rooms

Open the local [Mattermost server](http://localhost:57898). The private config has each fictional user's username/password and room URL. Use separate browser sessions for Priya, Alex and the custodian, or sign out between roles. These are distinct authenticated accounts, not display-name roles.

The campus showcase uses the natural phrases and reply threads shown in its conversations. The command reference below belongs to the original three-room developer fixture.

Every command starts at the beginning of a message with `findback`. Quoted commands, prose describing a command, and attachments are not commands.

| Claimant command | Result |
| --- | --- |
| `findback report I lost my black headphone case at the library this afternoon.` | Live model proposes a structured draft. |
| `findback confirm <draft-id>` | Claimant confirms that exact draft. Staff comparison is prepared. |
| `findback evidence Mine has a green triangle under the lid and a short blue cable inside.` | Preserves the claimant's own assertion and authenticated source; requests a fresh comparison. |
| `findback correct-evidence <evidence-id> <corrected text>` | Appends a correction and invalidates an old review. |
| `findback status` / `findback audit` | Reads only this claimant's case. |
| `findback receipt <reservation-id>` | Closes only that claimant's current reservation, after staff handover. |

| Staff command | Result |
| --- | --- |
| `findback review C001` | Generates a staff-only comparison for exact revisions. |
| `findback approve C001 I-001 <review-id> <reason>` | Atomic human decision and reservation. |
| `findback reject C001 I-001 <review-id> <reason>` | Rejects the proposed match without a reservation. |
| `findback ask C002 The broad description fits two items.` | Saves staff's reason privately; sends a generic, non-leading question. |
| `findback collect C001 <reservation-id> Library Desk A at 17:00.` | Private collection instructions; remains unreturned. |
| `findback handover C001 <reservation-id> Fictional demo handover.` | Records staff handover; claimant receipt remains pending. |
| `findback cancel C001 <reservation-id> More evidence is needed.` | Cancels before handover; requires fresh review. |
| `findback status C001` / `findback audit C001` | Staff-only records. |
| `findback help` | Exact command syntax, including inventory registration/correction. |

The fixture inventory has two distinguishable black cases (`I-001`, `I-002`) and one blue bottle (`I-003`). Their identifying facts live in staff views. A broad black-case report yields two candidates; software never selects the owner from similarity.

## Verify

```sh
.venv/bin/python -m pytest -q
.venv/bin/findback offline-demo
.venv/bin/findback status C001
```

`offline-demo` uses a separate newly named database and explicitly simulated actors/model analysis. It never overwrites live state. It exercises the actual domain state machine, including a refused claimant approval, premature receipt, and duplicate receipt.

Live integration evidence is separate from unit tests. See `EVIDENCE.md` for measured results, versions and the distinction between real model/transport behavior and scripted fictional participants. The demo script, when run, sends messages from fictional account tokens: that is authenticated scripted roleplay, not evidence that real users typed them or that real property was returned.

For a fresh private session without deleting the completed example:

```sh
.venv/bin/findback --state-dir .findback/session-2 bootstrap
.venv/bin/findback --state-dir .findback/session-2 run
```

Keep that runtime listening, then use the corresponding fictional accounts in Mattermost. The optional scripted run is `python -m findback.live_demo --state-dir .findback/session-2 --scripted-roleplay` from the same virtual environment.

If a model call or message fails, its raw event is retained. `findback retry-failed` requeues interrupted inbox work without erasing deduplication or case history. Run it after inspecting the local inbox error classes. A restart resumes durable transport cursors, inbox work, reservations and pending notices.

## How the boundaries work

- Separate private claimant rooms and staff room, with both private Mattermost channel type and private Switch read/write visibility.
- Distinct Intake identity and regular owner per case. Matcher belongs only to staff. The deterministic service belongs to all three rooms.
- The adapter checks the full enrolled Switch sender, then binds a command to the underlying immutable Mattermost user/post ID. It does not trust display names, a model's role string, or delegated/quoted approval.
- Literal privileged commands go directly to deterministic authorization. LLM outputs can draft a report or explain a comparison; they cannot construct an authenticated human context.
- SQLite transactions bind approval to claim/evidence/item revisions and enforce one active reservation per item and per case. Audit, deduplication and domain notices commit together.
- Approved records are frozen while reserved. Staff must cancel and obtain a fresh review before changing the basis. Cancellation after handover is refused.
- Transport acknowledgment follows durable inbox storage. Model inference does not block the heartbeat reader.
- Outbound posts carry stable markers. Recovery searches trusted posting identities in room history before retrying; incomplete/non-progressing history fails closed. Domain reservation/receipt deduplication is stronger than network delivery: absolute exactly-once posting is not claimed.

## MVP boundaries

Text-first intake, one enrolled claimant per precreated case, one custodian, fictional inventory, and a local Mattermost bridge. Photos, multilingual intake, public discovery, production enrollment, reminders, inventory withdrawal, whole-case cancellation, and exceptional reconciliation after physical handover are deferred. The matcher is an explanation aid; staff still owns the ownership decision. No accuracy, fraud-detection or time-saving claim has been measured.

The process uses a single runtime lock per state directory. A very old command outside the verified Mattermost post window fails closed; send a fresh command after inspection. Never publish `.findback/`, native Switch config, database files, or real claimant data.
