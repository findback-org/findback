# FindBack

### Lost something? Start a conversation.

A black headphone case arrives at the campus library. Two students think it might be theirs. The outside looks the same. The detail that could settle it is inside—and sharing that detail with the whole group would give away the answer.

FindBack helps carry that conversation through: private identifying questions, a clear comparison for the librarian, and a return recorded only after handover and receipt. Built with **Switch, Codex, and Mattermost** for the **Flip the Switch Hackathon · Best Personal or Creative Workflow**.

**[Watch the 2:50 demo](demo/FindBack-submission-demo.mp4)** · **[Landing page source](docs/index.html)** · **[Implementation evidence](EVIDENCE.md)** · **[Developer guide](DEVELOPER_GUIDE.md)**

![The actual Campus Lost and Found conversation in Mattermost](docs/assets/campus-chat.jpg)

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

**[FindBack submission demo · 2 minutes 50 seconds](demo/FindBack-submission-demo.mp4)**

The narrated walkthrough follows Alex’s claim through private evidence, librarian review, and approved collection. Its endpoint is **approved and reserved**, before handover or receipt. The [earlier campus demonstration](evidence/live-campus/verification.json) separately records three completed simulated returns and one unresolved claim.

The people and objects are fictional. Switch transport, Mattermost messages, and model calls are real. The finished video uses edited captures of the actual browser, including cuts and frame holds, with synthetic English narration. It is not continuous footage or evidence that a physical object changed hands. [Subtitles](demo/FindBack-submission-demo.srt) and [capture details](demo/START-HERE.txt) are included.

## How Switch is used

Switch is the messaging layer for the workflow. It connects a Python runtime to the local Mattermost collaboration and gives the agent roles their own identities and room memberships.

| Participant | Conversation | Responsibility |
| --- | --- | --- |
| Campus community | Lost-and-found channel | Broad found notices and community discussion. |
| Claimant + Intake | Private case conversation | Loss description, non-leading questions, identifying evidence, and receipt. |
| Librarian + Matcher | Private review room | Evidence comparison, release decision, and handover record. |
| FindBack service | Explicitly enrolled workflow rooms | Validate actions, save case state, and deliver the appropriate messages. |

Intake receives its claimant’s report. Matcher receives the staff-side comparison snapshot. Codex proposes and explains; application code checks identity and case revisions before applying any action. Neither model can approve a claim or operate Switch directly.

## Run locally

### Requirements

- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- The native macOS Switch Console installation, with its local server and bundled Mattermost running.
- An authenticated `codex` CLI available on `PATH` for live model calls.

The bootstrap currently targets this native macOS setup. It is not a generic remote-server installer. The landing page itself is static and needs none of these services.

```sh
git clone https://github.com/Findback/findback.git
cd findback
uv sync --locked --extra dev --python 3.12
```

Try the domain workflow without connecting to chat:

```sh
uv run findback offline-demo
```

Connect the developer fixture to the running local Switch server:

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

**154 automated tests passed** in the publication check. Preserved campus evidence includes **94 live access checks**, **12 live Codex calls**, and four fictional claims. Those are local demonstration results, not campus adoption or accuracy measurements.

Tests cover authorization, stale reviews, double reservation, handover-before-receipt, duplicate events, restart behavior, and private output routing. [EVIDENCE.md](EVIDENCE.md) distinguishes automated tests, live integration checks, and scripted inputs.

## Repository map

| Path | Contents |
| --- | --- |
| [`src/findback/`](src/findback/) | Domain workflow, storage, matching, Codex adapter, Switch transport, bootstrap, and demo runners. |
| [`tests/`](tests/) | Automated behavioral and integration-adapter tests. |
| [`docs/`](docs/) | Static landing page and embedded demo, ready for GitHub Pages. |
| [`demo/`](demo/) | Submission video, subtitles, narration scripts, and edit manifests. |
| [`evidence/`](evidence/) | Redacted run records, access checks, captures, and verification. |
| [`scripts/`](scripts/) | Reproducible capture, narration, and video assembly utilities. |

## GitHub Pages

The landing page is served from this same repository: **Settings → Pages → Deploy from a branch → `main` → `/docs`**. There is no build step. GitHub Pages hosts the explanation and recorded demo; the live agent workflow runs locally with Switch.

## Private state

Local credentials and account tokens live under `.findback/` and are excluded from Git, alongside `.env`, databases, virtual environments, and caches. Public evidence uses fictional data. Switch/Mattermost administrators and the local operator remain trusted; room membership is not operating-system isolation.

## Read more

- [Developer guide and command reference](DEVELOPER_GUIDE.md)
- [API and authorization contract](API_CONTRACT.md)
- [Flip the Switch Hackathon](https://www.wemakedevs.org/hackathons/switch)
- [Switch getting started](https://docs.switchagents.ai/switch-rooms/getting-started)
