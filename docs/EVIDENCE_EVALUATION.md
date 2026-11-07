# Evidence quality and action authority are separate

FindBack's model extracts and explains evidence. Application code decides who
may act, whether the reviewed revisions are current, and whether an item is
available. A fluent comparison is not an approval, and a safe refusal is not
proof that the comparison was factually correct.

The v1 evaluation keeps these questions separate:

| Question | Verification |
| --- | --- |
| Did Intake preserve stated facts and leave unknown facts unknown? | Predeclared field values, nulls and factual-component probes. |
| Did Matcher distinguish a coarse resemblance, a distinctive agreement, a contradiction and a missing observation? | Predeclared probes in the appropriate output fields, recommendation labels and entry-to-fact mappings. |
| Did urgency or embedded instructions produce an invented decision/confidence claim? | Explicit case-specific tripwires and recommendation constraints. These are bounded checks, not a universal injection detector. |
| Can a claimant or model approve an item, can stale evidence be approved, and can collection become a return before handover? | A separate six-check `DomainService`/SQLite fixture. Its results never increase model-quality counts. |

## Development, independent curation and freeze

The [public contract](../evals/CONTRACT.md) defines the data and scoring rules.
The scorer author used only [eight development cases](../evals/v1/development.jsonl)
with 37 evidence probes. [Example outputs](../evals/v1/development-outputs.json)
are handwritten test fixtures, not model results.

An independent agent authored and froze a separate held-out corpus before seeing
model outputs. It has 18 cases (eight Intake, ten Matcher) and 98 predeclared
probes. The scorer author did not inspect its contents before freezing the
scorer, production adapter/prompt, contract and development files in
[`scorer-freeze.json`](../evals/v1/scorer-freeze.json). The curator then seals
that freeze hash in the independent corpus manifest without changing corpus
bytes. This is recorded procedural separation within one development team,
not an externally administered benchmark.

A held-out run verifies both manifests. Only each case's `input` reaches the
ordinary production `CodexModel.draft` or `CodexModel.compare` call. IDs, tags,
expected values, aliases and tripwires stay in the local scorer. The entire
corpus runs; there is no favourable-case limit or filter.

Freeze v1 source before held-out execution:

```sh
uv run python -m findback.evaluation freeze \
  --output evals/v1/scorer-freeze.json
```

The checked-in freeze already exists; this command deliberately refuses to
overwrite it. A new experiment needs a new declared version and artifact path.
Do not rewrite an original freeze to conceal a scorer or prompt change.

## Run and reproduce

Development examples can exercise scoring without model credentials:

```sh
uv run python -m findback.evaluation run \
  --dataset evals/v1/development.jsonl \
  --freeze evals/v1/scorer-freeze.json \
  --mode fixture_replay \
  --replay evals/v1/development-outputs.json \
  --output /tmp/findback-eval-development-fixtures.json
```

This is labelled `fixture_replay`, with zero completed provider calls. It proves
that the scorer accepts those examples and catches deliberately wrong test
outputs; it does not prove that a model generated them.

Use the already-authenticated Codex CLI for actual proposals:

```sh
uv run python -m findback.evaluation run \
  --dataset evals/v1/development.jsonl \
  --freeze evals/v1/scorer-freeze.json \
  --mode live_codex \
  --output evidence/eval-v1-development-first.json

uv run python -m findback.evaluation run \
  --dataset evals/v1/held-out.jsonl \
  --freeze evals/v1/scorer-freeze.json \
  --corpus-manifest evals/v1/held-out-manifest.json \
  --mode live_codex \
  --output evidence/eval-v1-held-out-first.json
```

These calls use the constrained production adapter. They do not start Switch,
post chat messages, authorize a release or perform a physical return. Do not add
API keys or private claimant records to these fictional datasets.

Reports include the actual CLI version when available, Python/platform, raw
structured outputs, provider measurements/usage, elapsed time, every probe,
dataset and source hashes. The adapter requests the CLI default model and does
not expose the resolved provider model identity. The report states that limit;
model identity is not inferred or claimed pinned.

Final results and fsynced `.partial.jsonl` case journals use exclusive creation.
An interrupted run remains visible, and its path cannot be reused to erase it.
Partial journals do not establish a completed evaluation. Retain the first
completed held-out report and every miss/provider error. Later changes require
a new evaluation version, and their results must remain separate.

## Read the results without inflating them

`model_quality` reports cases, outputs scored, completed provider calls,
availability errors, all-probe-pass cases, schema validity and probe counts.
Tag counts overlap and must not be added as independent sample sizes. Automatic
schema validity is separate from the factual probes: a schema-valid invented
brand, invented date or misplaced contradiction can fail the evidence audit.

`deterministic_authority` runs actual domain methods and temporary SQLite with
fixed fictional people and proposals. It tests claimant/model refusal, stale
review refusal, a valid staff reservation, premature receipt refusal and the
reserved-versus-returned boundary. These six results are reported separately,
not blended with model quality or claimed as live transport checks.

Regex aliases audit specified factual components and field placement. They can
miss a valid paraphrase or fail to detect an unsupported extra clause, negation
or a new type of invention. Entry mapping means the annotated components were
mentioned; it is not proof that every claim in the entry was supported. Inspect
raw misses and outputs rather than retuning aliases after seeing held-out
answers. Human semantic review remains useful, and any post-hoc review must be
labelled separately from the predeclared score.

This small fictional sample establishes neither ownership accuracy, user demand,
real-world recovery success, broad prompt-injection resistance nor a winning
hackathon rank. Strong deterministic authority does not erase model-quality
misses; strong model-quality probes do not give a model action authority.
