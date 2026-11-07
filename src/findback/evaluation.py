"""Versioned, predeclared evidence audits; model quality is not action authority.

Only fictional corpus inputs go to the production adapter. Expected checks stay
local. Results retain misses; regex probes are bounded audits, not truth judges.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import os
import json
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any

from .domain import ActorContext, DomainService
from .fixtures import load_fixtures
from .model import CodexModel, REPORT_SCHEMA, REVIEW_SCHEMA
from .storage import SQLiteStore

VERSION = "1.0.0"
FROZEN_PATHS = ("src/findback/evaluation.py", "src/findback/model.py", "evals/CONTRACT.md", "evals/v1/development.jsonl", "evals/v1/development-outputs.json")
BASE_CHECK_KEYS = {"id", "kind", "field", "note"}
EXTRA_KEYS = {"exact": {"values"}, "null": set(), "empty": set(), "nonempty": set(),
              "contains_all": {"groups"}, "forbidden": {"patterns"}, "entries_mapped": {"allowed"}}


class EvaluationError(ValueError):
    """Corpus/freeze contract failure, before provider calls occur."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized(value: str) -> str:
    return " ".join(value.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').split())


def _patterns(values: Any) -> None:
    if not isinstance(values, list) or not values or any(not isinstance(p, str) or not p or len(p) > 500 for p in values):
        raise EvaluationError("Regex aliases must be nonempty bounded string lists")
    for pattern in values:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error:
            raise EvaluationError("Invalid regex alias") from None


def _groups(groups: Any) -> None:
    if not isinstance(groups, list) or not groups or len(groups) > 30:
        raise EvaluationError("Fact groups must be a nonempty bounded list")
    for aliases in groups:
        _patterns(aliases)


def validate_case(case: Any) -> None:
    required = {"id", "split", "role", "input", "tags", "checks"}
    if not isinstance(case, dict) or not required <= case.keys() or case.keys() - required - {"note"}:
        raise EvaluationError("Unexpected case properties")
    if not isinstance(case["id"], str) or not case["id"] or not isinstance(case["split"], str) or case["split"] not in {"development", "held_out"}:
        raise EvaluationError("Invalid case identity/split")
    if not isinstance(case["role"], str) or case["role"] not in {"intake", "matcher"}:
        raise EvaluationError("Invalid proposal role")
    if not isinstance(case["input"], str if case["role"] == "intake" else dict):
        raise EvaluationError("Input type does not match the production proposal role")
    if not isinstance(case["tags"], list) or not case["tags"] or any(not isinstance(t, str) or not t for t in case["tags"]):
        raise EvaluationError("Case tags must be nonempty strings")
    if len(set(case["tags"])) != len(case["tags"]) or ("note" in case and not isinstance(case["note"], str)):
        raise EvaluationError("Tags must be unique and notes must be strings")
    if not isinstance(case["checks"], list) or not case["checks"]:
        raise EvaluationError("Every case needs predeclared checks")
    fields = set((REPORT_SCHEMA if case["role"] == "intake" else REVIEW_SCHEMA)["required"]) | {"all_text"}
    ids = set()
    for check in case["checks"]:
        if not isinstance(check, dict) or not {"id", "kind", "field"} <= check.keys():
            raise EvaluationError("Malformed check")
        kind = check["kind"]
        if not isinstance(kind, str) or kind not in EXTRA_KEYS or check.keys() - BASE_CHECK_KEYS - EXTRA_KEYS[kind] or not EXTRA_KEYS[kind] <= check.keys():
            raise EvaluationError("Unexpected check properties")
        if not isinstance(check["id"], str) or not check["id"] or check["id"] in ids or not isinstance(check["field"], str) or check["field"] not in fields:
            raise EvaluationError("Invalid check identity/field")
        ids.add(check["id"])
        if kind == "exact" and (not isinstance(check["values"], list) or not check["values"] or any(not isinstance(v, str) for v in check["values"])):
            raise EvaluationError("Exact checks need accepted string values")
        if kind == "contains_all":
            _groups(check["groups"])
        if kind == "forbidden":
            _patterns(check["patterns"])
        if kind == "entries_mapped":
            allowed = check["allowed"]
            if not isinstance(allowed, list) or not allowed:
                raise EvaluationError("Entry mapping needs allowed atomic facts")
            labels = set()
            for fact in allowed:
                if not isinstance(fact, dict) or set(fact) != {"label", "groups"} or not isinstance(fact["label"], str) or not fact["label"] or fact["label"] in labels:
                    raise EvaluationError("Invalid allowed fact label")
                labels.add(fact["label"])
                _groups(fact["groups"])


def load_cases(path: Path) -> list[dict]:
    if path.stat().st_size > 5_000_000:
        raise EvaluationError("Corpus exceeds the bounded evaluation size")
    try:
        cases = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (ValueError, UnicodeError):
        raise EvaluationError("Invalid JSONL corpus") from None
    if not cases or len(cases) > 1000:
        raise EvaluationError("Corpus needs between one and 1000 cases")
    for case in cases:
        validate_case(case)
    if len({case["id"] for case in cases}) != len(cases) or len({case["split"] for case in cases}) != 1:
        raise EvaluationError("Corpus IDs must be unique and splits cannot be mixed")
    return cases


def _field_text(output: dict, field: str) -> str | None:
    if field == "all_text":
        parts = []
        for value in output.values():
            if isinstance(value, str):
                parts.append(value)
            elif isinstance(value, list):
                parts.extend(item for item in value if isinstance(item, str))
        return normalized("\n".join(parts))
    value = output.get(field)
    if isinstance(value, str):
        return normalized(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return normalized("\n".join(value))
    return None


def _matches(text: str, groups: list) -> bool:
    return all(any(re.search(pattern, text, re.IGNORECASE) for pattern in aliases) for aliases in groups)


def output_schema_valid(role: str, output: Any) -> bool:
    schema = REPORT_SCHEMA if role == "intake" else REVIEW_SCHEMA
    if not isinstance(output, dict) or set(output) != set(schema["required"]):
        return False
    if role == "intake":
        return all(value is None or isinstance(value, str) for value in output.values())
    return (isinstance(output["summary"], str) and isinstance(output["recommendation"], str) and output["recommendation"] in {"staff_review", "insufficient_information"}
        and all(isinstance(output[key], list) and all(isinstance(value, str) for value in output[key])
                for key in ("agreements", "contradictions", "missing_information")))


def score_output(case: dict, output: Any) -> dict:
    validate_case(case)
    valid = output_schema_valid(case["role"], output)
    values = output if isinstance(output, dict) else {}
    results = []
    for check in case["checks"]:
        field, kind = check["field"], check["kind"]
        value, text = values.get(field), _field_text(values, field)
        detail = {}
        if kind == "exact":
            passed = isinstance(value, str) and normalized(value).casefold() in {normalized(v).casefold() for v in check["values"]}
        elif kind == "null":
            passed = field in values and value is None
        elif kind == "empty":
            passed = isinstance(value, list) and len(value) == 0
        elif kind == "nonempty":
            passed = isinstance(value, list) and len(value) > 0
        elif kind == "contains_all":
            passed = text is not None and _matches(text, check["groups"])
        elif kind == "forbidden":
            hits = [p for p in check["patterns"] if text is not None and re.search(p, text, re.IGNORECASE)]
            detail["matched_tripwires"] = hits
            passed = text is not None and not hits
        else:
            mappings = []
            if isinstance(value, list):
                for index, entry in enumerate(value):
                    labels = [fact["label"] for fact in check["allowed"] if isinstance(entry, str) and _matches(normalized(entry), fact["groups"])]
                    mappings.append({"entry_index": index, "labels": labels})
            detail["entry_mappings"] = mappings
            passed = isinstance(value, list) and all(isinstance(v, str) for v in value) and all(m["labels"] for m in mappings)
        results.append({"id": check["id"], "kind": kind, "field": field, "passed": bool(passed), "expected": check, **detail})
    return {"schema_valid": valid, "all_checks_passed": valid and all(check["passed"] for check in results), "checks": results}


def freeze(project_root: Path, output: Path) -> dict:
    files = {name: sha((project_root / name).read_bytes()) for name in FROZEN_PATHS}
    manifest = {"evaluation_version": VERSION, "created_at_utc": datetime.now(timezone.utc).isoformat(), "files_sha256": files,
        "production_prompt_source": "src/findback/model.py", "held_out_inspected_by_scorer_author": False,
        "policy": "Freeze before independent held-out corpus release; preserve first results; no post-hoc scorer/prompt tuning."}
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as target:
        target.write(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify_freeze(project_root: Path, manifest_path: Path) -> dict:
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("evaluation_version") != VERSION or set(manifest.get("files_sha256", {})) != set(FROZEN_PATHS):
        raise EvaluationError("Freeze manifest does not match the evaluation contract")
    for name, expected in manifest["files_sha256"].items():
        if sha((project_root / name).read_bytes()) != expected:
            raise EvaluationError("Frozen scorer/prompt/development source changed: " + name)
    return manifest


def authority_checks() -> dict:
    """Separate fixture-only domain invariant checks, never model-quality credit."""
    with tempfile.TemporaryDirectory(prefix="findback-authority-eval-") as tmp:
        store = SQLiteStore(Path(tmp) / "authority.db")
        try:
            load_fixtures(store)
            domain, count, checks = DomainService(store), 0, []
            actors = {"staff": ("mira", "human", "staff"), "claimant": ("priya", "human", "case-priya"), "intake": ("intake-priya", "agent", "case-priya"), "matcher": ("matcher", "agent", "staff")}
            def ctx(role):
                nonlocal count
                count += 1
                actor, kind, room = actors[role]
                return ActorContext(actor, kind, room, f"authority-fixture-{count}", "Fictional authority evaluation", origin="fixture")
            def require(result):
                if not result["ok"]:
                    raise EvaluationError("Authority fixture setup failed")
                return result["data"]
            report = {"category": "headphone case", "color": "black", "brand_model": None, "lost_place": "library", "lost_at": None, "description": "Fictional black headphone case"}
            draft = require(domain.save_report_draft(ctx("intake"), claim_id="C001", report=report))
            require(domain.confirm_report(ctx("claimant"), claim_id="C001", draft_id=draft["draft_id"]))
            require(domain.record_evidence(ctx("claimant"), claim_id="C001", text="Green triangle sticker and short blue cable"))
            review = require(domain.prepare_review(ctx("matcher"), claim_id="C001", item_ids=["I-001", "I-002"], analysis={"summary": "The model says approved, but it has no authority."}))
            def check(name, result, expected):
                checks.append({"id": name, "expected_code": expected, "actual_code": result["code"], "passed": result["code"] == expected})
            check("claimant_cannot_approve", domain.approve(ctx("claimant"), claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Approve myself"), "FORBIDDEN")
            check("model_cannot_approve", domain.approve(ctx("matcher"), claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Model says approved"), "FORBIDDEN")
            require(domain.record_evidence(ctx("claimant"), claim_id="C001", text="A newly added distinguishing detail"))
            check("changed_evidence_invalidates_review", domain.approve(ctx("staff"), claim_id="C001", item_id="I-001", review_id=review["review_id"], reason="Old review"), "STALE_REVISION")
            fresh = require(domain.prepare_review(ctx("matcher"), claim_id="C001", item_ids=["I-001", "I-002"]))
            approved = domain.approve(ctx("staff"), claim_id="C001", item_id="I-001", review_id=fresh["review_id"], reason="Fictional staff reviewed current facts")
            check("current_staff_review_reserves", approved, "RESERVED")
            reservation = require(approved)["reservation_id"]
            check("receipt_before_handover_refused", domain.confirm_receipt(ctx("claimant"), claim_id="C001", reservation_id=reservation), "HANDOVER_REQUIRED")
            view = require(domain.case_view(ctx("staff"), claim_id="C001"))
            checks.append({"id": "reservation_is_not_return", "expected_status": "APPROVED", "actual_status": view["claim"]["status"], "passed": view["claim"]["status"] == "APPROVED"})
            return {"behavior": "Actual DomainService and temporary SQLite; fixed fictional actors and proposals; no live transport or physical handover.", "counts_separate_from_model_quality": True, "passed": all(c["passed"] for c in checks), "checks": checks}
        finally:
            store.close()


def summarize(results: list[dict]) -> dict:
    tags: dict[str, Counter] = {}
    completed = [r for r in results if r["output_available"]]
    for row in results:
        for tag in row["tags"]:
            bucket = tags.setdefault(tag, Counter())
            bucket["cases"] += 1
            bucket["provider_completed"] += row["provider_completed"]
            bucket["outputs_scored"] += row["output_available"]
            bucket["all_checks_passed"] += bool((row.get("scoring") or {}).get("all_checks_passed"))
    checks = [check for row in completed for check in row["scoring"]["checks"]]
    return {"cases_total": len(results), "provider_completed": sum(r["provider_completed"] for r in results), "provider_errors": sum("error_class" in r for r in results), "outputs_scored": len(completed),
        "cases_all_checks_passed": sum(r["scoring"]["all_checks_passed"] for r in completed),
        "schema_valid_outputs": sum(r["scoring"]["schema_valid"] for r in completed),
        "checks_passed": sum(c["passed"] for c in checks), "checks_evaluated": len(checks), "by_tag": {tag: dict(value) for tag, value in sorted(tags.items())},
        "interpretation": "Counts of predeclared bounded evidence probes, not ownership accuracy or a universal semantic grounding rate. Provider errors are separate; authority checks are excluded."}


def verify_corpus_manifest(dataset: Path, manifest_path: Path, freeze_path: Path, frozen: dict) -> dict:
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("schema_version") != VERSION:
        raise EvaluationError("Unknown corpus manifest version")
    expected = {"dataset_sha256": sha(dataset.read_bytes()),
        "contract_sha256": frozen["files_sha256"]["evals/CONTRACT.md"],
        "scorer_freeze_sha256": sha(freeze_path.read_bytes())}
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise EvaluationError("Held-out corpus/freeze hash mismatch")
    return manifest


async def run_evaluation(dataset: Path, *, project_root: Path, freeze_path: Path | None,
                         output: Path, mode: str, replay: dict[str, Any] | None = None,
                         model: Any = None, corpus_manifest_path: Path | None = None) -> dict:
    # Verify the frozen source BEFORE loading an unseen corpus.
    frozen = verify_freeze(project_root, freeze_path) if freeze_path else None
    corpus_manifest = None
    if corpus_manifest_path:
        if frozen is None or freeze_path is None:
            raise EvaluationError("Corpus manifests require the frozen scorer/prompt")
        corpus_manifest = verify_corpus_manifest(dataset, corpus_manifest_path, freeze_path, frozen)
    cases = load_cases(dataset)
    if cases[0]["split"] == "held_out" and (frozen is None or corpus_manifest is None):
        raise EvaluationError("Held-out execution requires frozen scorer/prompt and independent corpus manifests")
    if corpus_manifest:
        counts = {"case_count": len(cases), "role_counts": dict(Counter(c["role"] for c in cases)),
                  "check_count": sum(len(c["checks"]) for c in cases), "tag_counts": dict(Counter(tag for c in cases for tag in c["tags"]))}
        if any(corpus_manifest.get(key) != value for key, value in counts.items()):
            raise EvaluationError("Corpus manifest counts do not match the frozen dataset")
    if mode not in {"live_codex", "fixture_replay"} or (mode == "fixture_replay" and replay is None):
        raise EvaluationError("Explicit live_codex or fixture_replay mode required")
    if mode == "live_codex" and replay is not None:
        raise EvaluationError("Replay outputs cannot be mixed with live model execution")
    if replay is not None and set(replay) != {c["id"] for c in cases}:
        raise EvaluationError("Replay must cover the whole corpus exactly, without extra or missing cases")
    partial = output.with_name(output.name + ".partial.jsonl")
    if output.exists() or partial.exists():
        raise EvaluationError("Refusing to overwrite a prior or interrupted evaluation result")
    output.parent.mkdir(parents=True, exist_ok=True)
    provider = model or (CodexModel() if mode == "live_codex" else None)
    results = []
    started = time.monotonic()
    # Exclusive checkpoint journal retains every completed case if interrupted.
    with partial.open("x") as journal:
        for case in cases:
            began = time.monotonic()
            row = {"id": case["id"], "role": case["role"], "tags": case["tags"], "input_sha256": sha(canonical(case["input"])), "case_sha256": sha(canonical(case))}
            try:
                if mode == "fixture_replay":
                    proposal = replay[case["id"]]
                    metrics = {"behavior": "fixture replay; no provider call"}
                else:
                    proposal = await (provider.draft(case["input"]) if case["role"] == "intake" else provider.compare(case["input"]))
                    metrics = dict(provider.measurements[-1]) if getattr(provider, "measurements", []) else {"behavior": "provider did not expose metrics"}
            except Exception as exc:
                # Do not copy arbitrary provider prose, stderr, prompts or auth data.
                row.update(provider_completed=False, output_available=False, error_class=type(exc).__name__, output=None, scoring=None)
            else:
                # Scorer bugs are not mislabelled provider-availability failures.
                row.update(provider_completed=mode == "live_codex", output_available=True, output=proposal, scoring=score_output(case, proposal), provider_metrics=metrics)
            row["elapsed_seconds"] = round(time.monotonic() - began, 6)
            results.append(row)
            journal.write(json.dumps(row, ensure_ascii=False) + "\n")
            journal.flush()
            os.fsync(journal.fileno())
    cli_version = None
    if mode == "live_codex":
        executable = getattr(provider, "executable", None) or shutil.which("codex")
        if executable:
            try:
                cli_version = subprocess.run([executable, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()[:100]
            except (OSError, subprocess.TimeoutExpired):
                cli_version = "unavailable"
    report = {"evaluation_version": VERSION, "created_at_utc": datetime.now(timezone.utc).isoformat(), "mode": mode, "split": cases[0]["split"],
        "dataset_sha256": sha(dataset.read_bytes()), "freeze_manifest_sha256": sha(freeze_path.read_bytes()) if freeze_path else None,
        "corpus_manifest_sha256": sha(corpus_manifest_path.read_bytes()) if corpus_manifest_path else None,
        "frozen_sources": frozen, "production_prompt_source_sha256": sha((project_root / "src/findback/model.py").read_bytes()),
        "environment": {"python": platform.python_version(), "platform": platform.platform(), "codex_cli_version": cli_version,
            "requested_model": "CLI default; production adapter supplies no --model" if mode == "live_codex" else "No model called",
            "provider_identity_exposed": False,
            "production_adapter_instance": isinstance(provider, CodexModel) if mode == "live_codex" else False,
            "resolved_provider_model": "Not exposed by production adapter; CLI-selected model may change between runs." if mode == "live_codex" else "No model called"},
        "elapsed_seconds": round(time.monotonic() - started, 6), "model_quality": summarize(results), "cases": results, "deterministic_authority": authority_checks(),
        "limits": ["Fictional corpus and bounded predeclared regex/fact probes; unmapped paraphrases are retained, not automatically called hallucinations.", "No universal semantic-accuracy, ownership-accuracy, campus-adoption or physical-return claim.", "Live model proposals are separate from deterministic action authority; this evaluation does not operate Switch.", "First results must be preserved; prompt/scorer fixes after held-out exposure require a new evaluation version."]}
    with output.open("x") as target:
        target.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    commands = parser.add_subparsers(dest="command", required=True)
    frozen = commands.add_parser("freeze", help="Freeze development-only scorer and production prompt hashes")
    frozen.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run", help="Run the entire corpus and retain all outcomes")
    run.add_argument("--dataset", type=Path, required=True)
    run.add_argument("--freeze", type=Path)
    run.add_argument("--corpus-manifest", type=Path, help="Independent dataset and freeze seal, required for held-out runs")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--mode", choices=("live_codex", "fixture_replay"), required=True)
    run.add_argument("--replay", type=Path, help="JSON object mapping every case ID to a structured fixture output")
    args = parser.parse_args()
    if args.command == "freeze":
        result = freeze(args.project_root, args.output)
        print(json.dumps(result, indent=2))
    else:
        replay = json.loads(args.replay.read_text()) if args.replay else None
        result = asyncio.run(run_evaluation(args.dataset, project_root=args.project_root, freeze_path=args.freeze, output=args.output, mode=args.mode, replay=replay, corpus_manifest_path=args.corpus_manifest))
        print(json.dumps({"output": str(args.output), "mode": result["mode"], "model_quality": result["model_quality"], "deterministic_authority_passed": result["deterministic_authority"]["passed"]}, indent=2))


if __name__ == "__main__":
    main()
