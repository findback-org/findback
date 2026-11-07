# First frozen evidence evaluation, v1

These are the retained **first live results** from the production `CodexModel`
adapter. They are not the handwritten development examples, fixture replay, or
an ownership-accuracy measurement. The scorer, production adapter, contract and
development data were hashed before the scorer author inspected held-out cases.
The independent curator authored and froze the held-out corpus before seeing
model outputs. [Methodology](HELD_OUT_METHOD.md), [scorer freeze](scorer-freeze.json),
[held-out seal](held-out-manifest.json), and [contract](../CONTRACT.md) are included.

| Split | Cases attempted | Completed provider calls | Provider errors | Cases passing every predeclared check | Passed probes / evaluated probes |
| --- | ---: | ---: | ---: | ---: | ---: |
| Development | 8 | 8 | 0 | 7 | 36 / 37 |
| Held out | 18 | 18 | 0 | 17 | 97 / 98 |

All 26 returned structured outputs satisfied the production output schema.
Schema checks are reported separately and are not added to the probe counts.
The six deterministic action-authority checks passed in each report; these use
actual `DomainService`/SQLite with fictional actors and remain separate from
model-quality counts.

The full reports retain inputs by corpus/case hash, structured outputs, each
predeclared probe result, usage and elapsed time:

- [First development report](../../evidence/eval-v1-development-first.json),
  SHA-256 `52778dfab7b3f6d4e7f6e9fd1292c1081f0c5fc746da967bd6e3cca43cb266ee`.
- [First held-out report](../../evidence/eval-v1-held-out-first.json),
  SHA-256 `5e785e2f9ec888b987b34183892a189d468e82526ae60ecc079aee70d1122773`.
- [Independent integrity and miss review](../../evidence/eval-v1-review.json).

Both reports' exclusive `.partial.jsonl` checkpoints match their final case
records exactly. The original reports, checkpoints, corpus, labels and scores
have not been rewritten after exposure. This review does not convert a failed
probe into a pass.

## What the retained misses mean

The held-out curator reviewed the two failed probes after the complete first
reports were saved. This is a source-based interpretation by that same curator,
not an external adjudication panel or a new model score. Both failures are
`empty` checks, so these are annotation/taxonomy questions rather than missed
regex synonyms.

**Development: `dev-matcher-coarse`, `no_factual_conflict`.** The claimant says
the case is “plain black.” The annotation requires an empty contradiction list.
The model instead notes that “plain” **may** conflict with observed markings,
but explicitly says the meaning is ambiguous and may describe only the exterior
or general appearance. It recommends `insufficient_information` and asks what
“plain” means. This is a cautious interpretation of supplied wording, not an
invented identifying detail or an affirmative ownership decision. The retained
check fails because the annotation disallows any such qualified conflict entry.

**Held out: `hold-matcher-09`, `no-active-conflict`.** The claimant first reports
an amber ribbon, then explicitly supersedes it with a black-ribbon correction.
Staff observed a black ribbon. The model says the **current** corrected assertion
matches, while placing a note about the earlier discrepancy in `contradictions`:
“The current assertion matches staff observations; the earlier discrepancy
remains part of the evidence history.” The annotation requires the array to be
empty when no active conflict remains. The answer is source-grounded and does
not falsely treat amber as the current assertion; its placement under the
contradiction heading still fails the declared field convention and could be
confusing in a staff card.

A future version should explicitly define `contradictions` as unresolved current
conflicts and give historical changes a separate presentation convention. Any
new prompt, schema, aliases or annotations require a new version; they must not
replace these v1 results. Neither reviewed miss establishes an unsupported fact,
but reviewing two misses does not certify every clause in all passing outputs.

## Practical limits

The run used macOS, Python 3.12.13 and Codex CLI 0.162.0. The production adapter
supplies no `--model`; the resolved provider model is not exposed. Its default
can change between runs, so frozen prompt/source hashes do not make the live
outputs bit-reproducible. A single small fictional challenge set is not a
population estimate of reliability, usefulness or ownership correctness.

The 98 held-out probes are narrow component checks and specified tripwires.
An agreement entry can contain an allowed fact component and still add an
unsupported clause; a valid paraphrase can also miss an alias. No universal
hallucination rate is inferred. Provider completion, schema validity, evidence
probes and deterministic authorization are separate observations. This model
evaluation makes no Switch posts and does not establish that a physical item
was returned.
