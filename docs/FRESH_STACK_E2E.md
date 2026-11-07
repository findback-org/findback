# Verify FindBack on a fresh live stack

This opt-in test starts its **own Switch Core, Mattermost and PostgreSQL**, then
runs FindBack against real agent WebSockets and real authenticated human-account
HTTP posts. It does not use Switch Console's existing native server, accounts,
rooms, tokens, database, ports or volumes. The fictional people and physical
handover remain scripted. A deterministic model is the reproducible default;
actual Codex inference is an explicit separate mode.

Run from this repository checkout after `uv sync --locked --extra dev`. Docker
Engine must be running, Docker Compose must support
[`!override`](https://docs.docker.com/reference/compose-file/merge/#replace-value)
(v2.24.4 or newer), and the host needs network access to the public image
registries. The deployment is designed for Linux Docker and Docker Desktop. Mattermost's
official image is `linux/amd64`; Apple Silicon runs it under Docker's emulation.
No Switch Console installation is required.

```sh
# Live Switch/Mattermost; controlled model proposals, no model account required.
uv run --no-sync python -m findback.e2e_stack \
  --model fixture --output .findback/e2e-results/fixture.json

# Fresh independent stack again; actual host-side Codex inference.
# First authenticate the host Codex CLI normally. No keys enter the containers.
uv run --no-sync python -m findback.e2e_stack \
  --model codex --output .findback/e2e-results/codex.json
```

Every invocation chooses a fresh private `.findback/e2e-<random>` directory and
`findback-e2e-<random>` Compose project. An optional `--state-dir PATH` must name a
directory that **does not exist**. Reusing an old case database or an earlier E2E
configuration is refused. A passing command exits 0; a failed scenario, startup,
or teardown exits nonzero. Inspect `passed`, `scenario.failed_step`/`error_code`
and `teardown` in the JSON rather than inferring success from a created artifact.

## What actually runs

The CLI copies the unmodified official Switch standalone Compose contract,
adds an isolation override, generates new secrets, validates the effective
deployment, and starts only PostgreSQL, its initialization job, Core, Mattermost
and the official bridge setup job. Both `/health`/Mattermost ping and successful
completion of the setup job are required before enrollment.

Provisioning uses the fresh gateway/Mattermost APIs to create four separate
nonadmin agent owners, four scoped agents, three nonadmin human accounts and
three private rooms. A setup-only, read-only query inside this project's own
PostgreSQL container resolves Core 0.31's internal client/actor IDs, which the
gateway directory does not expose. This query cannot target the native database:
its command always names this generated Compose project and its own `postgres`
service. The FindBack runtime receives scoped agent keys and human tokens, not
Core/database administrator credentials. No native bootstrap helper is invoked.

The host-side scenario then exercises:

- Actual scoped agent/human reads and expected 403 refusals across the rooms.
- A deliberately labelled model failure, persisted inbox failure and explicit
  recovery of that original input, followed by claim/draft confirmation.
- Ambiguous candidates, independent claimant evidence, agent/claimant approval
  refusal, stale-review rejection and an exact librarian approval/reservation.
- Collection without falsely recording return, premature receipt refusal,
  simulated staff handover and the claimant's own receipt.
- Receipt deduplication, private-inventory readback checks, and an unresolved
  second claim.
- A runtime restart with its owned service cursor rewound to an earlier
  acknowledged point, actual Core replay, and unchanged authoritative action/
  notification/model counts. This verifies redelivery rather than manually
  calling the domain twice.

`fixture` substitutes deterministic in-process draft/comparison proposals; all
chat transport, provisioning, account identity, room permissions and SQLite
state transitions are real. `codex` uses the existing host `CodexModel` and
authenticated CLI, including the same injected failure before real inference.
The test does not mount the host's home directory, Codex auth, API keys, source
tree or Docker socket into any container. It does not test physical custody,
user demand, production deployment security or every possible outage.

## Isolation and recovery

Only Core and Mattermost have published ports: Docker assigns unused ports bound
to `127.0.0.1`. PostgreSQL has no host port. All database/chat data uses new named
volumes in this generated project. Ambient `COMPOSE_*`, `DB_*` and model secrets
are excluded from the Compose process environment. The effective deployment is
refused if it introduces a bind mount, external volume/network, public/fixed port,
host network, privileged service, unexpected image or missing ownership label.

Normal success, failure and interruption all attempt `compose down --volumes`
for **that project only**, after validating the saved configuration hashes and
each container/volume/network's ownership labels. Removal is checked afterward.
There is no Docker prune, global stop, image deletion or shared-volume cleanup.

```sh
# Explicit diagnostic option: leave only this generated stack running.
uv run --no-sync python -m findback.e2e_stack --model fixture \
  --state-dir .findback/e2e-debug-new --keep-stack \
  --output evidence/fresh-stack-debug.json

# Later, remove that exact saved owned stack; no scenario/posts are run.
uv run --no-sync python -m findback.e2e_stack \
  --cleanup .findback/e2e-debug-new
```

If a host/process kill prevents `finally` cleanup, use that cleanup command with
the saved directory. If ownership or snapshot validation fails, investigate
instead of bypassing it. Keep private state/configuration and
`compose-last-error.log` out of GitHub and CI uploads: these can contain generated
credentials. The designated report contains safe checks and image/source pins;
the private directory also keeps its own `verification-report.json`.

## Version and source pins

The checked-in [image lock](../infra/e2e/images.lock.json) records immutable OCI
index digests for official Core 0.31.0, setup 0.31.0, Mattermost 11.9.0 and PostgreSQL 16
images. No `latest` image is used. On November 8, 2026 the PostgreSQL index resolved
to 16.15-alpine3.24. The unmodified official Compose SHA256 is checked before use.

Official Switch source is pinned to
[`7e57cd433a922bc91a00150c106e6ef1f15b1057`](https://github.com/sandbox-quantum/switch/blob/7e57cd433a922bc91a00150c106e6ef1f15b1057/deploy/local/standalone-docker-compose.yml)
(`switch-v0.31.0`), matching FindBack's protocol 8 transport. Its license/modified
Commons Clause are preserved beside the vendored file. Updating a pin is a
reviewed infrastructure change, followed by new fixture and real-model reports.

The reference was Overruled's
[clean-install E2E workflow](https://github.com/Kevrc/overruled/blob/200c869afb01b3194b8504900b59e63fbbc8157a/.github/workflows/e2e.yml)
at its inspected commit. FindBack uses its own lifecycle and scenario code,
dynamic loopback ports, explicit ownership validation and project-only teardown.

`tests/test_e2e_stack.py` tests the no-reuse guard, secret isolation, immutable
snapshots, unsafe deployment refusal, failed setup readiness, private diagnostics
and cleanup ownership without Docker. Those unit tests are separate from the
explicit live runs above; ordinary `pytest` does not launch containers or models.

## Recorded runs

The [fresh fixture rerun](../evidence/fresh-stack-fixture-retry.json) and
[fresh Codex run](../evidence/fresh-stack-codex.json) each passed 22 scenario checks
and 28 live access checks on November 8, 2026. Both started with zero project
resources, created 5 containers/2 volumes/1 network, kept their recorded source
unchanged, and verified owned-resource removal. The Codex run records 6 successful
model calls and zero controlled proposal calls; the fixture run records the
inverse. Each report includes every host source file's SHA256, a combined manifest
and base Git commit, in addition to the separate official Switch source pin.

The [first fixture attempt](../evidence/fresh-stack-fixture.json) remains available:
its lifecycle/access checks passed, but the replay harness initially rewound to
fresh handshake cursor 0, which does not identify a historical room position in
Core 0.31. The harness now reuses an actual earlier positive issued ticket. The
failure was retained, and the passing rerun used another completely fresh stack.
