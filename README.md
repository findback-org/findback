# FindBack

[Watch the demo](https://youtu.be/lj0zuQWdPd8) · [Run locally](#run-locally) · [Fresh-stack verification](docs/FRESH_STACK_E2E.md) · [Evaluation results](evals/v1/RESULTS.md)

### Lost something? Start a conversation.

A black headphone case arrives at the campus library. Two students think it might be theirs. The outside looks the same. The detail that could settle it is inside—and sharing that detail with the whole group would give away the answer.

FindBack helps carry that conversation through: private identifying questions, a clear comparison for the librarian, and a return recorded only after handover and receipt. Built with **Switch, Codex, and Mattermost** for the **Flip the Switch Hackathon · Best Personal or Creative Workflow**.

**[Watch the demo · 2:58](https://youtu.be/lj0zuQWdPd8)** · **[Implementation evidence](EVIDENCE.md)** · **[Developer guide](DEVELOPER_GUIDE.md)**

## Verified implementation

| Check | Recorded result | Inspect the proof |
| --- | --- | --- |
| Clean installation and automated suite | **318 tests passed in each environment:** macOS/Python 3.12 and local Linux arm64/Python 3.11 and 3.12. Offline, installed-wheel and development-fixture checks also passed. | [Verification manifest](evidence/phase2-verification.json) · [Linux runs](evidence/phase2-linux-checks.json) |
| Fresh Switch + Mattermost + PostgreSQL | **22 scenario checks + 28 access checks** passed in each of the two passing fresh-stack runs. The real-Codex run made **6 successful model calls**; the controlled run made zero AI calls. Both verified replay and teardown. | [Real-Codex report](evidence/fresh-stack-codex.json) · [Controlled-model report](evidence/fresh-stack-fixture-retry.json) |
| First held-out evidence evaluation | **18/18 provider calls completed; 17/18 cases passed every declared check, totaling 97/98 probes.** The scorer and prompts were frozen before the scorer author inspected held-out cases; the miss is retained. | [Results and limits](evals/v1/RESULTS.md) · [Raw first report](evidence/eval-v1-held-out-first.json) |

These are local verification results with fictional people, items and handovers. Chat transport, access checks and the labelled Codex calls ran for real. Evidence probes do not establish ownership accuracy. Hosted CI is [reported separately](#verification); no hosted pass is claimed.

[![Latest FindBack demo: Ava reports a found case, Alex and Noah reply, and FindBack opens a private recovery chat](docs/assets/findback-chat-landscape.jpg)](https://youtu.be/lj0zuQWdPd8)

## The story

The community channel is where people hear about a found item. The identifying questions belong somewhere private.

An **Intake** agent helps a claimant describe their loss without revealing the answers staff are checking. A **Matcher** agent prepares the librarian’s comparison: what agrees, what contradicts the claim, and what still needs an answer. The librarian decides whether to release the specific item. Collection details go to the approved claimant privately.

An appointment is not a return. The case closes only when staff record the handover and that claimant confirms receipt. If the evidence is too broad, the case stays open.

## What runs today

- Private claimant conversations and a separate librarian review room, connected to an actual campus Mattermost channel through Switch.
- Live Codex calls for structured report intake and staff-only evidence comparison.
- Replies bound to the relevant report, review, and handover messages.
- Librarian-only release of the exact reviewed claim and item; one active reservation per item.
- Persistent cases, evidence revisions, approvals, audit records, and return receipts in SQLite.
- Scoped agent identities, verified message senders, restart handling, and replay checks.

**Next:** finding older photo and text reports buried in a busy campus group. Historical photo search and general public enrollment are still planned. The current build demonstrates verification and return; it does not claim to search hundreds of historical photos.

## Watch the demo

**[FindBack on YouTube · 2 minutes 58 seconds](https://youtu.be/lj0zuQWdPd8)** · [Download MP4](demo/FindBack-submission-under-3-minutes.mp4)

The narrated walkthrough follows Alex’s claim through private evidence, librarian review, and approved collection. Its endpoint is **approved and reserved**, before handover or receipt. The [earlier campus demonstration](evidence/live-campus/verification.json) separately records three completed simulated returns and one unresolved claim.

The people and objects are fictional. Switch transport, Mattermost messages, and model calls are real. The finished video uses edited captures of the actual browser, including reading holds and smooth transitions, with synthetic narration by Miles. It is not continuous footage or evidence that a physical object changed hands. [Subtitles](demo/FindBack-submission-under-3-minutes.srt) and [capture details](demo/START-HERE.txt) are included.

The submission cut is 2:58.167 at the original playback speed. It ends after the collection instructions and the explicit handover/receipt caveat, omitting only the final public thanks and recap. The [original full recording · 3:09](demo/FindBack-submission-demo.mp4) and its production manifests remain preserved.

## How Switch is used

Switch is the messaging layer for the workflow. It connects a Python runtime to the local Mattermost collaboration and gives the agent roles their own identities and room memberships.

| Participant | Conversation | Responsibility |
| --- | --- | --- |
| Campus community | Lost-and-found channel | Broad found notices and community discussion. |
| Claimant + Intake | Private case conversation | Loss description, non-leading questions, identifying evidence, and receipt. |
| Librarian + Matcher | Private review room | Evidence comparison, release decision, and handover record. |
| FindBack service | Explicitly enrolled workflow rooms | Validate actions, save case state, and deliver the appropriate messages. |

Intake receives its claimant’s report. Matcher receives the staff-side comparison snapshot. Codex proposes and explains; application code checks identity and case revisions before applying any action. Neither model can approve a claim or operate Switch directly.

```mermaid
flowchart LR
    Campus[Public campus channel] --> Switch[Switch rooms and scoped identities]
    Claimant[Claimant private chat] <--> Switch
    Staff[Librarian private review] <--> Switch
    Switch -->|durable inbox before cursor ACK| Runtime[Python workflow runtime]
    Runtime -->|claimant statement only| Intake[Codex Intake proposal]
    Runtime -->|staff-only snapshot| Matcher[Codex Matcher proposal]
    Intake --> Runtime
    Matcher --> Runtime
    Runtime -->|verified actor and current revisions| DB[(SQLite cases, reservations, audit)]
    DB -->|durable outbox| Switch
```

Switch supplies the rooms, agent identity, memberships, message context and event transport. Python owns the domain rules. Removing Switch removes the actual human–agent conversations and their identity-bound delivery; the isolated domain demo below deliberately uses fictional transport instead.

## Technical decisions you can verify

| Property | Implementation and failure check |
| --- | --- |
| A model cannot release an item | [Typed, tool-disabled proposal adapter](src/findback/model.py); [tool attempts and invalid output are refused](tests/test_model.py). Approval still requires an enrolled human librarian. |
| An old comparison cannot approve new evidence | [Revision-bound domain transactions](src/findback/domain.py); [evidence changing during inference invalidates the result](tests/test_revision_guard.py). |
| Two approvals cannot reserve the same object | [SQLite partial unique indexes and immediate transactions](src/findback/storage.py); [competing approvals, rollback, replay and restart tests](tests/test_domain.py). |
| Display names and quoted text are not authority | [Mattermost post/account/body verification](src/findback/runtime.py); [forged, relayed, webhook and agent impersonation checks](tests/test_runtime.py). |
| A backlog cannot silently discard an older human action | [Bounded, timestamp-aware source-history verification](src/findback/provenance.py); [older approvals, public reports, boundary ties and unavailable-proof checks](tests/test_provenance.py). Missing proof remains failed for operator attention. |
| A slow claimant does not freeze every conversation | At most four rooms process concurrently, with one slot reserved for the librarian and input order preserved within each room; [blocked-model, backlog, saturation and shutdown/replay checks](tests/test_scheduling.py). |
| Failed notifications cannot fill every delivery batch | [Persisted due times, bounded retries and blocked states](src/findback/delivery.py) apply to both queues and the real showcase; [restart, uncertain-POST reconciliation, migration and privacy checks](tests/test_delivery.py). |
| Reconnect does not silently skip received events | [Persistent Switch generation and cursor handling](src/findback/switch_client.py); [unacknowledged frames, gaps, eviction and unknown HTTP outcomes](tests/test_switch_client.py). |
| The workflow can run without the existing demo installation | [Fresh-stack runner](src/findback/e2e_stack.py) provisions pinned Core/Mattermost/PostgreSQL with new accounts and volumes; [fixture and actual Codex reports](docs/FRESH_STACK_E2E.md#recorded-runs) verify the complete lifecycle and server replay. |
| Model quality is measured separately from authority | [Versioned evidence evaluation](docs/EVIDENCE_EVALUATION.md), an independently sealed held-out corpus and [unaltered first results](evals/v1/RESULTS.md), including misses and grading limits. |

These automated checks use fictional data and controlled providers. [Live integration records](EVIDENCE.md) separately establish that the Switch identities, room access boundaries and Codex calls worked on the local stack. They do not establish real-world ownership accuracy.

Operators can inspect payload-free queue health with `findback health` and explicitly recover failed notifications with `findback retry-delivery`. [Operations and recovery](docs/OPERATIONS.md) explain the retry budget, unknown outcomes, queue-ordering limits and safe restart. Queue health is not a network or process-liveness check.

## Run locally

### Requirements

- Python 3.11+ (3.12 recommended) and [uv](https://docs.astral.sh/uv/).
- Docker Engine and Compose 2.24.4+ for the independent fresh-stack workflow.
- An authenticated `codex` CLI on `PATH` only for real-model runs and evaluation.

The fresh-stack runner does not require Switch Console. The optional native bootstrap below targets an existing macOS Switch Console installation and its bundled server/Mattermost. The landing page itself is static.

```sh
git clone https://github.com/findback-org/findback.git
cd findback
uv sync --locked --extra dev --python 3.12
```

Try the domain workflow without connecting to chat:

```sh
uv run findback offline-demo
```

Run the entire workflow on new disposable services, using real chat transport and controlled model proposals:

```sh
uv run python -m findback.e2e_stack --model fixture --output .findback/local-fresh-stack.json
```

For actual model inference, authenticate the host Codex CLI and run a second independent stack:

```sh
uv run python -m findback.e2e_stack --model codex --output .findback/local-fresh-stack-codex.json
```

Each invocation creates fresh accounts, rooms, databases and volumes, checks access refusals, drives the recovery lifecycle, verifies replay and tears down only its owned Docker project. No existing Switch/Mattermost setup or its credentials are reused; Codex authentication stays on the host. The people, provider-failure injection and handover are scripted. [Fresh-stack guide and recorded results](docs/FRESH_STACK_E2E.md).

To use the existing native macOS Switch Console server instead:

```sh
uv run findback doctor
uv run findback bootstrap
uv run findback run
```

Bootstrap provisions the demo’s private rooms, scoped identities, and fictional accounts. It reuses existing records without resetting progressed cases. Keep the runtime listening, open Mattermost, and use the enrolled accounts. The [developer guide](DEVELOPER_GUIDE.md) documents chat commands, account setup, and private session reuse.

To create a separate scripted campus showcase with its own history:

```sh
uv run python -m findback.showcase --viewer-username YOUR_MATTERMOST_USERNAME --pace 0.7
```

Use your existing local Mattermost username. The command prints the actual room URL, runs fictional participant messages through real accounts, and leaves the agents listening. 

## Verification

```sh
uv run pytest -q
```

Two workflows are configured:

- [Unit, evaluation-fixture and installed-wheel checks](.github/workflows/ci.yml) on Linux/Python 3.11 and 3.12 and macOS/Python 3.12. No model credentials or running Switch instance are needed.
- [Fresh-stack E2E](.github/workflows/fresh-stack-e2e.yml) on actual disposable Core/Mattermost/PostgreSQL services, with controlled proposals and only a redacted JSON artifact.

GitHub did not start the hosted runners at the November 8 publication check. **No hosted CI pass is claimed.** The table above links the completed clean local runs. Earlier run counts remain dated in [EVIDENCE.md](EVIDENCE.md).

The [fresh-stack reports](docs/FRESH_STACK_E2E.md#recorded-runs) retain the first replay-harness failure as well as both passing runs. The [evaluation results](evals/v1/RESULTS.md) retain the original misses and their reviewed contradiction-category boundary. To check the frozen scorer using handwritten development outputs, without calling a provider:

```sh
uv run python -m findback.evaluation run \
  --dataset evals/v1/development.jsonl \
  --freeze evals/v1/scorer-freeze.json --mode fixture_replay \
  --replay evals/v1/development-outputs.json \
  --output .findback/local-development-fixture.json
```

To replicate the original held-out experiment with actual model calls and a new result file:

```sh
uv run python -m findback.evaluation run \
  --dataset evals/v1/held-out.jsonl --freeze evals/v1/scorer-freeze.json \
  --corpus-manifest evals/v1/held-out-manifest.json --mode live_codex \
  --output .findback/local-held-out-evaluation.json
```

The evaluator refuses to overwrite an existing result. The set is now public; later runs are replications, not newly unseen trials. Prompt/scorer changes require a new evaluation version. The [evaluation guide](docs/EVIDENCE_EVALUATION.md) describes the split, seals, raw outputs and limits.

Preserved campus evidence includes **94 live access checks**, **12 live Codex calls**, and four fictional claims. Those are local demonstration results, not campus adoption or accuracy measurements. Check the dated [evidence log](EVIDENCE.md) and Actions results for verification of a particular commit.

Tests cover authorization, stale reviews, double reservation, handover-before-receipt, duplicate events, restart behavior, and private output routing. [EVIDENCE.md](EVIDENCE.md) distinguishes automated tests, live integration checks, and scripted inputs.

## Repository map

| Path | Contents |
| --- | --- |
| [`src/findback/`](src/findback/) | Domain workflow, storage, matching, Codex adapter, Switch transport, bootstrap, and demo runners. |
| [`tests/`](tests/) | Automated behavioral and integration-adapter tests. |
| [`docs/`](docs/) | Static landing page and embedded demo, ready for GitHub Pages. |
| [`demo/`](demo/) | Submission video, subtitles, narration scripts, and edit manifests. |
| [`evidence/`](evidence/) | Redacted run records, access checks, captures, and verification. |
| [`evals/`](evals/) | Versioned development/held-out cases, frozen scoring contract, seals and first results. |
| [`infra/e2e/`](infra/e2e/) | Pinned official deployment, isolated overrides, image digests and attribution. |
| [`scripts/`](scripts/) | Reproducible capture, narration, and video assembly utilities. |

## GitHub Pages

The static project overview and recorded demonstration are included in `docs/`. The live agent workflow runs locally with Switch.

## Private state

Local credentials and account tokens live under `.findback/` and are excluded from Git, alongside `.env`, databases, virtual environments, and caches. Public evidence uses fictional data. Switch/Mattermost administrators and the local operator remain trusted; room membership is not operating-system isolation.

## Read more

- [Developer guide and command reference](DEVELOPER_GUIDE.md)
- [API and authorization contract](API_CONTRACT.md)
- [Queue health, delivery recovery and restart](docs/OPERATIONS.md)
- [Fresh live-stack verification](docs/FRESH_STACK_E2E.md)
- [Evidence evaluation and first held-out results](docs/EVIDENCE_EVALUATION.md)
- [Flip the Switch Hackathon](https://www.wemakedevs.org/hackathons/switch)
- [Switch getting started](https://docs.switchagents.ai/switch-rooms/getting-started)
