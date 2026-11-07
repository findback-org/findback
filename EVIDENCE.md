# FindBack implementation evidence

Source and fixtures are included; no hosted agent deployment or hackathon submission is implied.

## Fresh-stack and held-out verification extension

The exact staged extension passed **318 tests in each clean environment**: macOS/Python 3.12 and local Linux arm64/Python 3.11 and 3.12. Each also passed frozen development-fixture evaluation, the offline workflow and isolated installed-wheel smoke. [Verification manifest](evidence/phase2-verification.json) and [Linux results](evidence/phase2-linux-checks.json) record hashes and limits. Its fresh-stack runner starts pinned official Core/Mattermost/PostgreSQL services with zero existing project resources, provisions new credentials/accounts/rooms, and uses actual chat HTTP/WebSocket traffic. Both runs passed **22 scenario checks and 28 live access checks**, recorded one fictional return, kept their recorded source unchanged and verified project-only teardown:

| Run | Proposals | Result |
| --- | --- | --- |
| [Fresh controlled-model stack](evidence/fresh-stack-fixture-retry.json) | Six canned proposals, **zero model inference** | Full lifecycle, expected refusals, injected failure/recovery, actual server replay and deduplication passed. |
| [Fresh actual-Codex stack](evidence/fresh-stack-codex.json) | Six real calls: three Intake, three Matcher | Same live scenario passed; recorded provider latency was 4.662–8.374 seconds. |

The [independent review](evidence/fresh-stack-independent-review.json) verified the raw private SQLite records, source/report hashes, model-mode labels, cleanup and public-artifact credential exclusion. [Commands, pins and limits](docs/FRESH_STACK_E2E.md) are included. The [original failed fixture attempt](evidence/fresh-stack-fixture.json) is retained: its workflow checks passed but the replay harness used the initial zero cursor instead of an issued multi-room ticket. The corrected test then ran on another fresh stack. These are scripted fictional users and handovers; they do not establish physical custody or adoption. The existing native demonstration was untouched.

The **first frozen evidence evaluation** used eight development cases and a separately authored, sealed 18-case held-out corpus. Its implementation author did not inspect the held-out cases before the first recorded run. The production adapter completed all 26 calls with no provider errors. Development passed **36/37 probes (7/8 cases)**; held out passed **97/98 probes (17/18 cases)**. Six deterministic authority checks are reported separately and do not inflate those model-quality counts. [First raw reports and review](evals/v1/RESULTS.md) retain both misses and explain their contradiction-category conventions without rewriting scores. These bounded component checks are not semantic or ownership accuracy; the resolved provider model is not exposed by the CLI, and future runs are replications of a now-public set.

The [fresh-stack CI workflow](.github/workflows/fresh-stack-e2e.yml) uses real disposable services and controlled proposals. It uploads only a redacted JSON result. The unit/wheel workflow separately exercises the frozen development-fixture evaluator. Local evidence does not imply that GitHub-hosted runners executed; their actual status must be checked on the published commit.

## November 8 technical hardening

The final local automated suite passed **247/247** checks. [Machine-readable verification](evidence/hardening-verification.json) records source hashes, test counts and verification boundaries. The exact staged source was exported and installed in a clean temporary checkout, then its wheel was installed and exercised outside that checkout. GitHub did not start the initial hosted runners, so these results are **local verification**, not a hosted CI pass.

The [current 3:09 film](https://youtu.be/CwU4WdZB-Sk) is the Miles narration of the recorded interactive run. It shows real Switch/Mattermost messages and live agent responses with fictional participants and objects; its endpoint is approved and reserved, before handover and receipt. The film was recorded **before** the reliability changes below. The new failure/recovery evidence is automated and uses controlled providers, rather than claiming a newly recorded live recovery.

- [Per-room scheduling checks](tests/test_scheduling.py) block inference in one room, verify an authenticated second claimant still receives a status reply through the real handler, preserve FIFO within a room, enforce a four-room concurrency bound, exercise a 205-event backlog, and replay cancelled work from SQLite.
- [Model cancellation checks](tests/test_model.py) verify the child process is stopped and `CancelledError` survives even if that child already exited. Interrupted work remains eligible for replay rather than being marked successfully completed.
- [Delivery recovery checks](tests/test_delivery.py) exercise both real SQLite queues with controlled providers: persistent backoff across restart, older failures not starving new deliveries, bounded retries and permanent refusal, reconciliation after an unacknowledged successful POST, migration rollback, read-only health privacy, and explicit operator requeue. The conversational showcase uses the same delivery pass. See [the exact operational contract](docs/OPERATIONS.md); strict output order and network-wide exactly-once delivery are not claimed.
- [Source-history recovery checks](tests/test_provenance.py) process an older authenticated approval and community report through the real handlers, beyond the first 200 posts. Cursor overlap handles Mattermost's timestamp-boundary behavior. Missing, unavailable, incomplete, nonprogressing or over-bound history leaves a fixed safe failed-inbox diagnostic rather than silently completing the event. Search is bounded; raw Mattermost-to-Switch thread conversion remains trusted transport context, as documented in the operations guide.
- [CI](.github/workflows/ci.yml) installs locked runtime/test dependencies, exercises the offline domain workflow and builds and installs a wheel outside the source tree on Linux and macOS. The same clean-wheel smoke check passed locally: one fictional return, one reservation, and `passed: true`. The build backend itself is not locked, and bit-identical wheel builds are not claimed.
- [Fresh live access checks](evidence/hardening-access-checks.json) passed **28/28** against the existing local Switch/Mattermost stack. This was read-only: it did not claim a WebSocket connection, post messages, run models or change case progress. It verifies token/account/room boundaries, not the new retry behavior or a fresh full live scenario.
- [Local Linux verification](evidence/hardening-linux-checks.json) mounted the same verified source read-only in two ephemeral official Python containers. Python **3.11.18** and **3.12.16** each passed all **247 tests**, the locked offline domain workflow and an isolated installed-wheel smoke check. Exact image digests, source hashes and commands are included. These were Debian 13/Linux arm64 containers on the local Docker engine, not GitHub-hosted Ubuntu jobs. No existing containers or live workflow were changed.

The dated campus runs below remain historical evidence with their original counts. The repository's current automated checks are available in [GitHub Actions](https://github.com/Krishna3451/findback/actions/workflows/ci.yml).

## Earlier campus run: actual Mattermost campus

The primary demonstration is [Campus • Lost & Found](http://localhost:57898/switch/channels/findback-campus-4f73ef6b), with linked private conversations and staff review. It is an actual messaging-app history, not a slide reconstruction. [Showcase run](evidence/live-campus/showcase-run.json), [verification](evidence/live-campus/verification.json), and [94 actual access checks](evidence/live-campus/access-checks.json) record 12 distinct fictional human accounts, 46 human posts, four actual claims, 12 live model proposals and three saved returns. Alex remains NEEDS_INFO. All 49 public messages at story completion were checked for the identifying answers; none leaked them. All three receipts were checked against the exact handover threads.

The earlier publication suite had **154 passing tests**, including public-source intake isolation, exact draft/review/handover-card replies and bounded book-category aliases. Leo's word “paperback” initially yielded no coarse candidate; the alias was corrected and Mira sent a real new review request. The preserved run resumed without resetting cases or fabricating a review. Its recorded elapsed time includes the debugging pause; it is not presented as a clean timed benchmark. The earlier rehearsal completed in 143.5 seconds and has its own evidence files.

The actual browser received a new message without reloading after aligning the browser with Mattermost's existing canonical localhost address. No server/CORS/Origin checks were weakened. The current local team's capacity was increased from 50 to 100 to accommodate the explicitly fictional cohorts; that was the only server configuration field changed. See [capacity proof](evidence/live-campus/setup-capacity.json) and [browser check](evidence/showcase-browser-check.json).

Cosmetic formatting hid transport footers without editing human posts or domain records. The earlier checks and text playback below are historical evidence, not the current product demonstration.

[Actual Mattermost walkthrough](evidence/live-campus/mattermost-demo.mp4): 70.916667 seconds, H.264/24 fps, captured from the real browser at 649×769 and padded by one pixel to 650×770 for encoding. There is no generated UI, narration or text reconstruction. Navigation gaps/loading frames were trimmed as documented in the [capture manifest](evidence/live-campus/capture-manifest.json) and [capture notes](evidence/live-campus/CAPTURE.md). This is an edited walkthrough captured during the scripted run, not a continuous recording of every event.

| Behavior | Evidence | What it establishes |
| --- | --- | --- |
| Domain, transport, runtime and model checks | `python -m pytest -q`: **122 passed** | Atomic races, transaction rollback, current-revision approval, exact account/room authorization, replay/restart, source provenance, model input separation, safe output routing, startup cleanup, bounded posts and delivery reconciliation. Transport/model unit tests use fake providers. |
| Offline scenario | `findback offline-demo` | Actual domain code, simulated actors and comparison: one reservation/return, premature receipt and claimant approval refused. Separate database. |
| Actual room/token boundaries | [privacy-checks.json](evidence/privacy-checks.json) | Real nonadmin claimant tokens and scoped agent keys received HTTP 403 across forbidden rooms. Service's explicit memberships worked. Addressing rules exercised. |
| Reproducible access suite | [access-checks.json](evidence/access-checks.json) | All 31 HTTP checks passed, including credential/path substitution and nonadmin owner scope. |
| Full real transport/model scenario | [live-run.json](evidence/live-run.json) | Five real Codex calls; two real private claimant channels and a real staff channel; authoritative transitions driven by exact authenticated account messages. Inputs were scripted fictional roleplay. |
| Agent refusal and threading | [roundtrip-checks.json](evidence/roundtrip-checks.json) | Actual Matcher identity's approval refused; real threaded reply read back. No extra return record. |
| Live restart | [restart-checks.json](evidence/restart-checks.json) | Authoritative record digest unchanged; one authenticated status reply after restart. |
| Final constrained inference | [model-constraints-check.json](evidence/model-constraints-check.json) | Real harmless CLI call succeeds with 32 explicit feature disables and strict response validation. |
| Narrated playback | [findback-demo.mp4](evidence/findback-demo.mp4) | 152.021 seconds, 1080p H.264/AAC, captured live content reconstructed as text. Synthetic narration, fictional scripted input, no screen-recording claim. |

The captured live run completed at **13:25:39 UTC / 18:55:39 IST**. It produced two cases, three reviews, one reservation, one return record, seventeen audited domain attempts and five persisted model proposals. Priya ended `RETURNED`, Alex `NEEDS_INFO`. A second receipt message reused the first receipt.

The live run refused a claimant's self-approval (`FORBIDDEN`), staff's old comparison after new evidence (`STALE_REVISION`), and claimant receipt before staff handover (`HANDOVER_REQUIRED`). Collection instructions left the case `APPROVED`, rather than closing it. The similarity lookup kept both black cases as candidates.

Measured model latency was 7.129–7.446 seconds for Intake and 11.374–11.793 seconds for Matcher in this run. Token usage reported by the provider is in the JSON record. No dollar cost, time saved, accuracy or user demand is inferred from these measurements.

## Actual local stack

- Switch Console **0.40.1**, bundled native Core **0.31.0**.
- Mattermost Team Edition **11.9.0**, Docker container supplied by Switch Console.
- Codex CLI **0.162.0**, existing ChatGPT sign-in; no newly supplied API key.
- Python **3.12.13**, SQLite, httpx **0.28.1**, websockets **17.4**.
- Local native Core port **57897**, Mattermost port **57898**; setup discovers configured ports.

## Observed integration details

Core 0.31 emits an immutable enrolled human transport sender but can leave `sender_kind` null. The adapter verifies it against the underlying Mattermost user/post ID and exact message body; it does not infer humans from a display name. Webhook/system/delegated sources are refused. Bootstrap resolves the external identity mapping with operator authority; the runtime has no Core database credentials.

Switch's `!` prefix invokes built-in commands before application delivery. The initial attempted `!findback` message was intercepted and caused no domain transition. The verified application syntax is **`findback report …`**, without `!`.

Room visibility and addressing serve different purposes. Both platform channel privacy and Switch read/write visibility are private. A denied @mention is demoted to ordinary room chatter; a `filter=all` connection may still receive it. FindBack therefore applies its own immutable identity/room authorization to every command. Addressing alone is not presented as an authorization control.

## Reproduce safely

```sh
.venv/bin/python -m pytest -q
.venv/bin/findback doctor
.venv/bin/python -m findback.verify_access
```

Run `findback bootstrap` and `findback run` first. `verify_access` checks existing resources without changing case progress. To run the entire scripted roleplay, use `python -m findback.live_demo --scripted-roleplay` against a **fresh** setup. It refuses to reset an advanced case. A separate session can be provisioned with `findback --state-dir .findback/session-2 bootstrap` and run with the same flag.

Additional opt-in checks are `python -m findback.verify_roundtrip --post-diagnostics` (posts labeled agent diagnostics), and `python -m findback.verify_restart --capture` followed by a manual runtime restart and `python -m findback.verify_restart --verify-after` (posts a claimant status query). Later diagnostics have their own timestamps; the full-run counts above remain the original run's snapshot.

## Limits of the evidence

The fictional account inputs, ownership judgment, handover note and receipt were sent by a script. No real claimant or librarian user study, independently typed human participation, or physical recovery has been established. Both AI roles did run live, and both posted through their own scoped Switch identities. A real two-person demonstration can use the same commands and accounts without the roleplay script.

The service enforces who may decide and which revisions are acted on. It cannot establish that staff made a correct ownership judgment. Authorized staff can approve an insufficiently evidenced claim; there is no claimed universal ownership classifier.

SQLite reservations/return receipts are deduplicated transactionally. Chat notifications reconcile trusted sender markers against available history; network-wide absolute exactly-once delivery is not claimed. Missing/non-progressing history stops a retry. Room controls do not provide OS process isolation; local administrators and the operator remain trusted.
