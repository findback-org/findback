from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from findback.e2e_verify import (ChatScenario, E2EFixtureModel, E2EVerificationError,
                                 FailFirstDraft, replay_checkpoint, run_scenario, validate_fresh_config)
from findback.model import ModelError
from test_runtime import runtime


def config_marker():
    return {"e2e_stack": {"fresh": True, "project_name": "findback-e2e-test"},
            "cases": {"C001": {}, "C002": {}}}


@pytest.mark.parametrize("marker", [{}, {"fresh": False, "project_name": "findback-e2e-x"},
                                    {"fresh": True, "project_name": "production"}])
def test_scenario_refuses_nonisolated_config_before_creating_database(tmp_path, marker):
    config = config_marker()
    config["e2e_stack"] = marker
    with pytest.raises(E2EVerificationError, match="fresh_stack_marker_required"):
        asyncio.run(run_scenario(config, tmp_path))
    assert not (tmp_path / "findback.db").exists()


def test_scenario_refuses_existing_database_even_with_fresh_marker(tmp_path):
    path = tmp_path / "findback.db"
    path.write_bytes(b"preserve existing state")
    with pytest.raises(E2EVerificationError, match="fresh_findback_database_required"):
        validate_fresh_config(config_marker(), tmp_path)
    assert path.read_bytes() == b"preserve existing state"


def test_scenario_does_not_silently_replace_model_mode_or_cases(tmp_path):
    with pytest.raises(E2EVerificationError, match="unsupported_model_mode"):
        asyncio.run(run_scenario(config_marker(), tmp_path, model_mode="pretend-live"))
    config = config_marker()
    config["cases"]["C003"] = {}
    with pytest.raises(E2EVerificationError, match="expected_isolated_cases"):
        validate_fresh_config(config, tmp_path)


def test_deliberate_failure_is_once_and_never_mislabeled_as_model_inference():
    async def check():
        fixture = E2EFixtureModel()
        model = FailFirstDraft(fixture)
        with pytest.raises(ModelError):
            await model.draft("a fictional report")
        assert model.measurements == []
        result = await model.draft("a fictional report")
        assert result["description"] == "a fictional report"
        assert model.measurements[0]["behavior"] == "deterministic E2E fixture; no inference"
        assert len(model.measurements) == 1
    asyncio.run(check())


def test_fixture_comparison_uses_the_production_staff_snapshot_evidence_field():
    async def check():
        model = E2EFixtureModel()
        broad = await model.compare({"claim": {}, "evidence": []})
        supplied = await model.compare({"claim": {}, "evidence": [{"text": "A fictional identifying assertion"}]})
        assert broad["recommendation"] == "insufficient_information"
        assert broad["missing_information"]
        assert supplied["recommendation"] == "staff_review"
        assert supplied["missing_information"] == []
    asyncio.run(check())


def test_wait_does_not_hide_failed_runtime_task(runtime):
    async def check():
        async def fail():
            raise RuntimeError("controlled runtime failure")
        task = asyncio.create_task(fail())
        await asyncio.sleep(0)
        runtime.tasks = [task]
        scenario = ChatScenario(runtime.config, runtime, {"checks": [], "events": []}, timeout=0.1)
        with pytest.raises(RuntimeError, match="controlled runtime failure"):
            await scenario.wait("not-ready", lambda: False)
    asyncio.run(check())


def test_failed_inbox_cannot_be_reported_as_successful_scenario_event(runtime):
    runtime.tasks = []
    with runtime.store.transaction() as db:
        db.execute("INSERT INTO inbox(event_id,frame_json,state,error) VALUES ('bad','{}','FAILED','MODEL_UNAVAILABLE')")
    scenario = ChatScenario(runtime.config, runtime, {"checks": [], "events": []}, timeout=0.1)
    with pytest.raises(E2EVerificationError, match="step_inbox_state"):
        asyncio.run(scenario.event_result("bad", "step"))


def test_check_records_failed_assertion_before_raising(runtime):
    report = {"checks": [], "events": []}
    scenario = ChatScenario(runtime.config, runtime, report)
    with pytest.raises(E2EVerificationError, match="one_return"):
        scenario.check("one_return", False, observed=2)
    assert report["checks"] == [{"name": "one_return", "passed": False, "observed": 2}]


def test_lost_ack_checkpoint_uses_a_delivered_positive_cursor_not_handshake_zero(runtime):
    runtime.journal.capture("service-test", {"event": "connection_state", "data": {"cursor": 0}}, False)
    for cursor in (102, 101):
        runtime.journal.capture("service-test", {"id": cursor, "event": "message", "data": {
            "room_id": "one", "payload": {"message_id": str(cursor)}}}, True)
    assert replay_checkpoint(runtime.store, "service-test") == 101
    with pytest.raises(E2EVerificationError, match="positive_replay_checkpoint_required"):
        replay_checkpoint(runtime.store, "unrelated-service")
