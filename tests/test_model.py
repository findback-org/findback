from __future__ import annotations

import asyncio
import copy
import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from findback.model import CodexModel, ModelError


REPORT = {"category": "headphone case", "color": "black", "brand_model": None,
          "lost_place": "Library", "lost_at": None, "description": "A black headphone case"}
REVIEW = {"summary": "Two possible matches; staff review is needed.", "agreements": ["Black case"],
          "contradictions": [], "missing_information": ["Identifying marks"],
          "recommendation": "insufficient_information"}


class FakeProcess:
    def __init__(self, stdout, *, returncode=0, stderr=b"", delay=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode
        self.delay, self.killed, self.input = delay, False, None

    async def communicate(self, data):
        self.input = data
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.stdout, self.stderr

    def kill(self):
        self.killed, self.returncode = True, -9

    async def wait(self):
        return self.returncode


def provider_output(result, before=()):
    events = [*before, {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(result)}},
              {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 10}}]
    return "\n".join(json.dumps(event) for event in events).encode()


def install_process(monkeypatch, process):
    captured = {}

    async def spawn(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        directory = Path(args[args.index("-C") + 1])
        assert directory.exists()
        captured["working_files"] = sorted(p.name for p in directory.iterdir())
        captured["schema"] = json.loads(Path(args[args.index("--output-schema") + 1]).read_text())
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return captured


def test_model_uses_ephemeral_read_only_tool_disabled_session_and_filters_env(monkeypatch):
    monkeypatch.setenv("FINDBACK_PRIVATE_TOKEN", "must-not-reach-provider-process")
    monkeypatch.setenv("SWITCH_API_KEY", "must-not-reach-provider-process")
    process = FakeProcess(provider_output(REPORT))
    captured = install_process(monkeypatch, process)
    model = CodexModel(executable="test-codex")
    assert asyncio.run(model.draft("I lost a black headphone case.")) == REPORT
    args = captured["args"]
    assert "--ephemeral" in args and "--ignore-user-config" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    disabled = {args[index + 1] for index, argument in enumerate(args[:-1]) if argument == "--disable"}
    required = {
        "shell_tool", "unified_exec", "skill_search", "tool_suggest", "enable_mcp_apps",
        "apps", "plugins", "remote_plugin", "computer_use", "browser_use",
        "browser_use_external", "browser_use_full_cdp_access", "browser_annotation_api",
        "in_app_browser", "in_app_chat", "in_app_local_automation", "in_app_dictation", "in_app_voice",
        "image_generation", "view_image", "multi_agent", "hooks", "code_mode_host", "goals",
        "worktrees", "workspace_dependencies", "auth_elicitation", "skill_mcp_dependency_install",
        "plugin_sharing", "realtime_conversation", "sleep_tool", "shell_snapshot",
    }
    assert required <= disabled, f"Enabled-default tool surfaces remain: {sorted(required - disabled)}"
    assert 'web_search="disabled"' in args
    assert captured["working_files"] == ["schema.json"]
    assert "FINDBACK_PRIVATE_TOKEN" not in captured["kwargs"]["env"]
    assert "SWITCH_API_KEY" not in captured["kwargs"]["env"]
    assert "must-not-reach-provider-process" not in process.input.decode()
    assert model.measurements[-1]["role"] == "intake"


@pytest.mark.parametrize("item_type", [
    "command_execution", "mcp_tool_call", "web_search", "file_change",
    "image_view", "collab_tool_call", "dynamic_tool_call", "todo_list", "future_unknown_tool",
])
def test_tool_attempt_rejects_even_an_otherwise_valid_proposal(monkeypatch, item_type):
    process = FakeProcess(provider_output(REPORT, [{"type": "item.started", "item": {"type": item_type}}]))
    install_process(monkeypatch, process)
    model = CodexModel(executable="test-codex")
    with pytest.raises(ModelError, match="tool call"):
        asyncio.run(model.draft("A report"))
    assert model.measurements == []


def test_reasoning_items_are_allowed_without_expanding_the_action_surface(monkeypatch):
    process = FakeProcess(provider_output(REPORT, [
        {"type": "item.completed", "item": {"type": "reasoning", "text": "Extracting the stated facts."}},
    ]))
    install_process(monkeypatch, process)
    assert asyncio.run(CodexModel(executable="test-codex").draft("A black case")) == REPORT


def test_pre_turn_startup_diagnostic_is_counted_not_relayed_and_latency_stays_elapsed(monkeypatch, capsys):
    secret_diagnostic = "PRIVATE_STARTUP_DIAGNOSTIC_MUST_NOT_REACH_CHAT"
    process = FakeProcess(provider_output(REPORT, [
        {"type": "item.completed", "item": {"type": "error", "message": secret_diagnostic}},
        {"type": "turn.started"},
    ]))
    install_process(monkeypatch, process)
    # Replace only the model module's clock, not asyncio's clock. A boolean
    # turn-start flag accidentally replacing the latency start would yield 9999s.
    ticks = iter([10000.0, 10000.125])
    monkeypatch.setattr("findback.model.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    model = CodexModel(executable="test-codex")
    proposal = asyncio.run(model.draft("A black case"))
    assert proposal == REPORT
    measurement = model.measurements[-1]
    assert measurement["startup_diagnostics"] == 1
    assert math.isfinite(measurement["seconds"])
    assert 0 <= measurement["seconds"] < 1
    assert measurement["seconds"] == 0.125
    captured = capsys.readouterr()
    assert secret_diagnostic not in captured.out + captured.err + json.dumps(proposal) + json.dumps(measurement)


def test_error_item_after_turn_started_cannot_be_treated_as_startup_diagnostic(monkeypatch):
    install_process(monkeypatch, FakeProcess(provider_output(REPORT, [
        {"type": "turn.started"},
        {"type": "item.completed", "item": {"type": "error", "message": "PRIVATE_LATE_ERROR"}},
    ])))
    model = CodexModel(executable="test-codex")
    with pytest.raises(ModelError) as raised:
        asyncio.run(model.draft("A black case"))
    assert "PRIVATE_LATE_ERROR" not in str(raised.value)
    assert model.measurements == []


def test_failed_turn_is_rejected_even_if_a_later_completed_event_exists(monkeypatch):
    install_process(monkeypatch, FakeProcess(provider_output(REPORT, [
        {"type": "turn.started"},
        {"type": "turn.failed", "error": {"message": "PRIVATE_FAILURE_DETAILS"}},
    ])))
    model = CodexModel(executable="test-codex")
    with pytest.raises(ModelError) as raised:
        asyncio.run(model.draft("A black case"))
    assert "PRIVATE_FAILURE_DETAILS" not in str(raised.value)
    assert model.measurements == []


@pytest.mark.parametrize("ending", [None, {"type": "turn.failed", "error": {"message": "Interrupted"}}])
def test_valid_json_without_successful_turn_completion_is_not_a_proposal(monkeypatch, ending):
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(REPORT)}}]
    if ending:
        events.append(ending)
    output = "\n".join(json.dumps(event) for event in events).encode()
    install_process(monkeypatch, FakeProcess(output))
    model = CodexModel(executable="test-codex")
    with pytest.raises(ModelError):
        asyncio.run(model.draft("A black case"))
    assert model.measurements == []


@pytest.mark.parametrize("bad_report", [
    {**REPORT, "approved_by": "staff-mira"}, {**REPORT, "color": ["black"]},
    {key: value for key, value in REPORT.items() if key != "description"},
])
def test_model_report_rejects_extra_missing_or_wrong_type_fields(monkeypatch, bad_report):
    install_process(monkeypatch, FakeProcess(provider_output(bad_report)))
    with pytest.raises(ModelError):
        asyncio.run(CodexModel(executable="test-codex").draft("A report"))


@pytest.mark.parametrize("change", [
    {"summary": 42}, {"agreements": "not a list"}, {"contradictions": [1]},
    {"recommendation": "approve_and_release"},
])
def test_model_comparison_requires_typed_non_authoritative_output(monkeypatch, change):
    result = {**copy.deepcopy(REVIEW), **change}
    install_process(monkeypatch, FakeProcess(provider_output(result)))
    with pytest.raises(ModelError):
        asyncio.run(CodexModel(executable="test-codex").compare({"claim": "test data"}))


def test_provider_error_never_relays_private_stderr(monkeypatch):
    install_process(monkeypatch, FakeProcess(b"", returncode=1, stderr=b"SECRET_PROVIDER_DIAGNOSTIC"))
    with pytest.raises(ModelError) as raised:
        asyncio.run(CodexModel(executable="test-codex").draft("A report"))
    assert "SECRET_PROVIDER_DIAGNOSTIC" not in str(raised.value)


def test_model_timeout_kills_process_and_returns_no_proposal(monkeypatch):
    process = FakeProcess(b"", delay=1)
    install_process(monkeypatch, process)
    with pytest.raises(ModelError, match="timed out"):
        asyncio.run(CodexModel(executable="test-codex", timeout=0.001).draft("A report"))
    assert process.killed


@pytest.mark.parametrize("child_already_exited", [False, True])
def test_model_cancellation_kills_child_and_preserves_runtime_cancellation(monkeypatch, child_already_exited):
    async def check():
        started = asyncio.Event()

        class BlockingProcess(FakeProcess):
            async def communicate(self, data):
                started.set()
                await asyncio.Event().wait()

            def kill(self):
                self.killed = True
                if child_already_exited:
                    raise ProcessLookupError("Child exited at the cancellation boundary")
                super().kill()

        process = BlockingProcess(b"")
        install_process(monkeypatch, process)
        model = CodexModel(executable="test-codex")
        task = asyncio.create_task(model.draft("A fictional report"))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert process.killed
        assert model.measurements == []

    asyncio.run(check())
