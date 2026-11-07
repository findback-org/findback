# Evidence evaluation contract — v1

This contract is fixed before held-out data is released to the scorer author.
It evaluates the existing production `CodexModel.draft` and `compare` proposals.
It does not grant a model action authority, establish ownership accuracy, or
replace the separate deterministic domain/transport authorization checks.

## Case format

One JSON object per JSONL line. Required keys:

- `id`: unique stable nonempty string, e.g. `dev-intake-01`.
- `split`: `development` or `held_out`; a file must contain one split only.
- `role`: `intake` or `matcher`.
- `input`: claimant text (string) for intake; ordinary staff comparison snapshot
  (object) for matcher. Only this value reaches the production model adapter.
- `tags`: nonempty list of stable problem labels, e.g. `ambiguity`, `contradiction`,
  `missing_details`, `urgency`, `instruction_injection`, `source_fidelity`.
- `checks`: nonempty list of predeclared checks described below.

Optional `note`: a short fictional scenario description for human review.
No real claimant/staff personal data or credentials belong in a corpus.
Unknown record/check properties are rejected to catch annotation errors.

## Auditable check vocabulary

Every check has `id`, `kind`, `field` and optional `note`. `field` is a production
output property (`category`, `color`, `brand_model`, `lost_place`, `lost_at`,
`description`, `summary`, `agreements`, `contradictions`, `missing_information`,
`recommendation`) or `all_text`. Text checks join list entries using newlines,
normalize smart quotes/whitespace, and match regex aliases case-insensitively.
Aliases are authored from the input and expected facts, before seeing outputs.
They are not sent to the model.

| kind | Additional keys | Pass condition |
| --- | --- | --- |
| `exact` | `values`: nonempty array of strings | The field is a string equal to one accepted value after case/whitespace normalization. |
| `null` | none | The named field exists and is JSON null. |
| `empty` | none | The named field is an empty list. |
| `nonempty` | none | The named field is a nonempty list. |
| `contains_all` | `groups`: nonempty array of nonempty regex-string arrays | Every group has at least one alias match in the field's text. Groups express one atomic fact's required components; several checks can represent several facts. |
| `forbidden` | `patterns`: nonempty regex-string array | None of the predeclared unsupported-claim/decision/confidence tripwires matches the field. |
| `entries_mapped` | `allowed`: nonempty array of `{label,groups}` objects | The field is a list and each textual entry matches all components of at least one allowed fact label. An empty list passes this mapping check; use `nonempty` and coverage checks when evidence is required. |

Checks target source fidelity, distinguishing facts, contradictory facts,
coarse-only ambiguity, missing information and misleading urgency/instructions.
For intake, annotate supported fields and explicit nulls for unknown fields;
relative time should not become an invented absolute date. For matcher, annotate
coverage in the appropriate agreement/contradiction/missing-information array,
plus recommendation where the production prompt specifies one. Use accepted
recommendations rather than pretending every judgment has a uniquely correct
label: coarse-only evidence must be `insufficient_information`; other cases can
accept either production enum when justified by the predeclared scenario.

A mapping pass means only that an entry contains the annotated fact components.
It does **not** prove that every clause in that entry is true. Regex misses and
unmapped paraphrases are retained for human inspection; they are not automatically
labelled hallucinations. Forbidden probes catch specified claims, not every
possible invention. This is a bounded evidence audit, not a universal semantic
truth grader or an ownership-accuracy percentage.

## Splits and freeze

1. The implementation author develops the scorer/tests only on development data.
2. Before held-out inspection or execution, freeze hashes of `evaluation.py`,
   production `model.py`, this contract and development data in a manifest.
3. An independent curator freezes and hashes the held-out corpus using this
   contract. The scorer author must not inspect it before the first run.
4. The runner verifies frozen source/prompt hashes before loading held-out data.
   Expected checks never enter model inputs. Run the whole corpus; do not select
   favourable held-out cases. Save the first result, including failures/errors.
5. No prompt/scorer/alias tuning after held-out exposure is counted as the first
   result. A change requires a new version and preservation of the original run.

## Results and boundaries

Reports retain per-case input/dataset/source/prompt hashes, raw structured model
outputs, every check result and mapped labels, provider failures, elapsed time,
usage and environment/CLI version when available. Source-controlled inputs and
outputs must be fictional. Provider error prose is excluded.

Report counts, including total cases, completed provider calls, provider errors,
all-check-pass cases, check outcomes and tag breakdowns. Do not combine provider
availability and evidence quality into an unexplained overall percentage. Fixture
replay is labelled `fixture_replay`, never a live model run. A live run uses the
actual production adapter but does not operate Switch or move an item.

Domain authorization checks are a **separate report section** using actual
`DomainService`/SQLite transitions and fixed fictional actors. Their pass count
must not inflate model-quality outcomes. They establish refusal/state invariants,
not correctness of the human librarian's ownership decision.

## Manifest interface and interruption retention

The independent held-out manifest uses `schema_version: "1.0.0"`,
`dataset_sha256`, `contract_sha256`, `scorer_freeze_sha256`, `case_count`,
`role_counts`, `check_count`, `tag_counts`, and `independent_author_statement`.
The runner verifies the three hashes before case execution and compares the
case/role/check counts. The scorer freeze includes both development JSONL and
its explicitly handwritten example-output JSON. A held-out run requires
`--freeze` and `--corpus-manifest`; source changes invalidate that freeze.

Every completed case is fsynced to an exclusive `.partial.jsonl` checkpoint
alongside the final report. Existing final or checkpoint files are never
overwritten. Interrupted first-run outputs remain inspectable; use a new result
path for a declared subsequent attempt rather than erasing the first.

Production output-schema validity is reported separately. A case passes only
when its schema and all predeclared evidence probes pass; probe counts exclude
the automatic schema check. In fixture replay, `provider_completed` is zero and
`outputs_scored` counts the supplied handwritten outputs. Only live adapter
execution contributes completed provider calls; availability failures remain
separate from evidence-probe failures.
