# FindBack implementation evidence

Verified locally on November 7, 2026. Source and fixtures are included; no public deployment or hackathon submission is implied.

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
