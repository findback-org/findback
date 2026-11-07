"""Development-only evidence scorer and run-integrity tests; no held-out reads."""
from __future__ import annotations

import asyncio
from collections import Counter
import copy
import json
from pathlib import Path
import shutil

import pytest

from findback.evaluation import (EvaluationError, FROZEN_PATHS, VERSION, authority_checks,
    canonical, freeze, load_cases, run_evaluation, score_output, sha, validate_case,
    verify_freeze)
from findback.model import ModelError

ROOT = Path(__file__).parents[1]
DEVELOPMENT = ROOT / "evals/v1/development.jsonl"
OUTPUTS = ROOT / "evals/v1/development-outputs.json"


def development():
    return load_cases(DEVELOPMENT), json.loads(OUTPUTS.read_text())


def test_handwritten_development_examples_are_explicit_fixture_outputs():
    cases, outputs = development()
    assert len(cases) == 8
    assert all(score_output(case, outputs[case['id']])['all_checks_passed'] for case in cases)


@pytest.mark.parametrize('field,value,miss', [
    ('brand_model', 'Apple', 'brand_unknown'),
    ('lost_at', '2026-11-07T14:00:00+05:30', 'relative_is_not_date'),
    ('color', 'silver', 'color'),
    ('description', 'A blue suitcase lost on the train.', 'description_facts'),
])
def test_schema_valid_but_invented_intake_facts_fail_evidence_checks(field,value,miss):
    cases, outputs = development()
    case = cases[0]
    output = {**outputs[case['id']], field:value}
    result = score_output(case, output)
    assert result['schema_valid']
    assert not next(c for c in result['checks'] if c['id']==miss)['passed']
    assert not result['all_checks_passed']


def test_coarse_ambiguity_cannot_pass_by_recommending_staff_review_only():
    cases, outputs = development(); case=next(c for c in cases if c['id']=='dev-matcher-coarse')
    result=score_output(case,{**outputs[case['id']],'recommendation':'staff_review'})
    assert result['schema_valid'] and not result['all_checks_passed']
    assert not next(c for c in result['checks'] if c['id']=='insufficient')['passed']


def test_conflicting_features_must_be_identified_as_contradictions():
    cases, outputs=development(); case=next(c for c in cases if c['id']=='dev-matcher-conflicting')
    output=copy.deepcopy(outputs[case['id']]); output['contradictions']=[]
    output['agreements']+=['The orange square and green triangle both agree.']
    result=score_output(case,output)
    assert result['schema_valid'] and not result['all_checks_passed']
    assert not next(c for c in result['checks'] if c['id']=='shape_conflict')['passed']


def test_unmapped_entry_and_invented_outcome_are_visible_not_silently_supported():
    cases, outputs=development(); case=next(c for c in cases if c['id']=='dev-matcher-pressure')
    output=copy.deepcopy(outputs[case['id']]); output['agreements']=['Its serial number is ABC-912.']
    output['summary']='Mira has approved this claim.'
    result=score_output(case,output)
    assert result['schema_valid'] and not result['all_checks_passed']
    assert next(c for c in result['checks'] if c['id']=='no_invented_specific_agreement')['entry_mappings']==[{'entry_index':0,'labels':[]}]
    assert next(c for c in result['checks'] if c['id']=='not_release')['matched_tripwires']


def test_empty_mapping_is_vacuous_and_coverage_must_be_declared_separately():
    case={'id':'mapping-contract','split':'development','role':'matcher','input':{},'tags':['mapping'],
          'checks':[{'id':'map','kind':'entries_mapped','field':'agreements','allowed':[{'label':'red','groups':[['red']]}]},
                    {'id':'need','kind':'nonempty','field':'agreements'}]}
    output={'summary':'No observations.','agreements':[],'contradictions':[],'missing_information':[],'recommendation':'insufficient_information'}
    result=score_output(case,output)
    assert result['checks'][0]['passed'] and not result['checks'][1]['passed']


@pytest.mark.parametrize('bad', [None,[],{}, {'recommendation':[]}, {'approved':True}])
def test_invalid_outputs_never_crash_or_pass_the_scorer(bad):
    case=development()[0][0]
    result=score_output(case,bad)
    assert not result['schema_valid'] and not result['all_checks_passed']


@pytest.mark.parametrize('change', [
    {'split':[]}, {'role':[]}, {'extra_label':True}, {'tags':['same','same']},
    {'checks':[{'id':'x','kind':'contains_all','field':'summary','groups':[['(']]}]},
    {'checks':[{'id':'x','kind':'exact','field':'color','values':['black'],'unknown':True}]},
])
def test_bad_annotation_contract_is_rejected_before_provider_calls(change):
    case=copy.deepcopy(development()[0][0]);case.update(change)
    with pytest.raises(EvaluationError):validate_case(case)


def test_duplicate_ids_and_mixed_splits_are_rejected(tmp_path):
    case=development()[0][0]; p=tmp_path/'cases.jsonl'
    p.write_text(json.dumps(case)+'\n'+json.dumps(case)+'\n')
    with pytest.raises(EvaluationError):load_cases(p)
    other={**case,'id':'other','split':'held_out'}
    p.write_text(json.dumps(case)+'\n'+json.dumps(other)+'\n')
    with pytest.raises(EvaluationError):load_cases(p)


def test_replay_retains_misses_has_zero_provider_calls_and_separate_authority(tmp_path):
    cases, outputs=development(); outputs['dev-intake-relative']['brand_model']='Invented brand'
    report=asyncio.run(run_evaluation(DEVELOPMENT,project_root=ROOT,freeze_path=None,
        output=tmp_path/'results.json',mode='fixture_replay',replay=outputs))
    assert report['mode']=='fixture_replay'
    assert report['model_quality']['cases_all_checks_passed']==7
    assert report['model_quality']['provider_completed']==0
    assert report['model_quality']['provider_errors']==0
    assert report['model_quality']['outputs_scored']==8
    assert report['cases'][0]['output']['brand_model']=='Invented brand'
    assert report['deterministic_authority']['passed']
    assert len(report['deterministic_authority']['checks'])==6
    assert len((tmp_path/'results.json.partial.jsonl').read_text().splitlines())==8
    with pytest.raises(EvaluationError):
        asyncio.run(run_evaluation(DEVELOPMENT,project_root=ROOT,freeze_path=None,
            output=tmp_path/'results.json',mode='fixture_replay',replay=outputs))


def test_partial_run_is_not_overwritten_and_replay_cannot_cherry_pick(tmp_path):
    _,outputs=development(); target=tmp_path/'results.json'
    target.with_name(target.name+'.partial.jsonl').write_text('first attempt retained\n')
    with pytest.raises(EvaluationError):
        asyncio.run(run_evaluation(DEVELOPMENT,project_root=ROOT,freeze_path=None,output=target,mode='fixture_replay',replay=outputs))
    outputs.pop(next(iter(outputs)))
    with pytest.raises(EvaluationError):
        asyncio.run(run_evaluation(DEVELOPMENT,project_root=ROOT,freeze_path=None,output=tmp_path/'other.json',mode='fixture_replay',replay=outputs))


def frozen_project(tmp_path):
    root=tmp_path/'project'
    for name in FROZEN_PATHS:
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,p)
    manifest=root/'freeze.json';freeze(root,manifest)
    return root,manifest


def test_source_changes_invalidate_freeze_and_freeze_cannot_be_overwritten(tmp_path):
    root,manifest=frozen_project(tmp_path)
    assert verify_freeze(root,manifest)['evaluation_version']==VERSION
    with pytest.raises(FileExistsError):freeze(root,manifest)
    (root/'src/findback/model.py').write_text('changed prompt source\n')
    with pytest.raises(EvaluationError):verify_freeze(root,manifest)


def test_synthetic_holdout_requires_independent_hash_seal_and_corpus_counts(tmp_path):
    # This is our own development case relabelled for protocol testing, not the
    # actual independently curated held-out file, which this test never opens.
    root,manifest=frozen_project(tmp_path);cases,outputs=development()
    case={**cases[0],'split':'held_out'};dataset=root/'synthetic-holdout.jsonl'
    dataset.write_text(json.dumps(case)+'\n');seal=root/'corpus.json'
    data={'schema_version':VERSION,'dataset_sha256':sha(dataset.read_bytes()),
          'contract_sha256':sha((root/'evals/CONTRACT.md').read_bytes()),'scorer_freeze_sha256':sha(manifest.read_bytes()),
          'case_count':1,'role_counts':{'intake':1},'check_count':len(case['checks']),
          'tag_counts':dict(Counter(case['tags'])),'independent_author_statement':'Synthetic protocol test only'}
    seal.write_text(json.dumps(data));replay={case['id']:outputs[case['id']]}
    with pytest.raises(EvaluationError):
        asyncio.run(run_evaluation(dataset,project_root=root,freeze_path=manifest,output=root/'missing-seal.json',mode='fixture_replay',replay=replay))
    report=asyncio.run(run_evaluation(dataset,project_root=root,freeze_path=manifest,corpus_manifest_path=seal,output=root/'ok.json',mode='fixture_replay',replay=replay))
    assert report['split']=='held_out' and report['model_quality']['cases_all_checks_passed']==1
    dataset.write_text(dataset.read_text()+'\n')
    with pytest.raises(EvaluationError):
        asyncio.run(run_evaluation(dataset,project_root=root,freeze_path=manifest,corpus_manifest_path=seal,output=root/'changed.json',mode='fixture_replay',replay=replay))


def test_live_adapter_seam_receives_only_input_and_provider_errors_stay_separate(tmp_path, monkeypatch):
    cases,_=development();subset=cases[:2];dataset=tmp_path/'dev.jsonl'
    dataset.write_text(''.join(json.dumps(c)+'\n' for c in subset))
    inputs=[]
    class UnavailableProvider:
        executable='/nonexistent/test-codex'
        async def draft(self,text):
            inputs.append(text);raise ModelError('PRIVATE_PROVIDER_DIAGNOSTIC')
    result=asyncio.run(run_evaluation(dataset,project_root=ROOT,freeze_path=None,output=tmp_path/'errors.json',mode='live_codex',model=UnavailableProvider()))
    assert inputs==[c['input'] for c in subset]
    assert result['model_quality']['provider_errors']==2 and result['model_quality']['checks_evaluated']==0
    assert result['model_quality']['cases_all_checks_passed']==0
    assert 'PRIVATE_PROVIDER_DIAGNOSTIC' not in (tmp_path/'errors.json').read_text()
    assert result['deterministic_authority']['passed']
    assert not result['environment']['production_adapter_instance']


def test_deterministic_authority_cannot_be_inflated_by_model_quality():
    result=authority_checks()
    assert result['passed'] and result['counts_separate_from_model_quality']
    assert {c['actual_code'] for c in result['checks'] if 'actual_code' in c} >= {'FORBIDDEN','STALE_REVISION','RESERVED','HANDOVER_REQUIRED'}
