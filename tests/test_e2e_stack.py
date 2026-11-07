"""No Docker required: fresh-resource ownership and cleanup safety boundaries."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from findback import e2e_stack


def plan(stack):
    services = {}
    for name in e2e_stack.ACTIVE:
        service = {"image": stack.lock["images"]["postgres" if name == "init-db" else name],
                   "labels": {e2e_stack.OWNER_LABEL: stack.owner}}
        if name in {"switch", "mattermost"}:
            service["ports"] = [{"host_ip": "127.0.0.1", "published": "0", "target": 8000 if name == "switch" else 8065}]
        if name in {"postgres", "mattermost"}:
            service["volumes"] = [{"type": "volume", "source": "pgdata"}]
        services[name] = service
    return {"name": stack.project, "services": services,
            "volumes": {"pgdata": {"name": stack.project + "_pgdata", "labels": {e2e_stack.OWNER_LABEL: stack.owner}}},
            "networks": {"default": {"name": stack.project + "_default", "labels": {e2e_stack.OWNER_LABEL: stack.owner}}}}


def test_new_stack_has_unique_credentials_private_files_and_pinned_source(tmp_path):
    one, two = e2e_stack.FreshStack(tmp_path / "first"), e2e_stack.FreshStack(tmp_path / "second")
    assert one.project != two.project and one.owner != two.owner
    assert one.env["DB_OWNER_PASSWORD"] != two.env["DB_OWNER_PASSWORD"]
    assert e2e_stack.sha256(one.state_dir / "compose.upstream.yaml") == one.lock["upstream_compose_sha256"]
    assert all("@sha256:" in image for image in one.lock["images"].values())
    assert one.state_dir.stat().st_mode & 0o777 == 0o700
    assert all(file.stat().st_mode & 0o777 == 0o600 for file in one.state_dir.iterdir())
    assert not (one.state_dir / "findback.db").exists()
    assert "CODEX" not in (one.state_dir / "stack.env").read_text()


def test_existing_state_is_never_reused_or_modified(tmp_path):
    state = tmp_path / "old"
    state.mkdir()
    saved = state / "config.json"
    saved.write_text("old existing configuration")
    with pytest.raises(e2e_stack.StackError, match="FRESH_STATE_DIRECTORY_REQUIRED"):
        e2e_stack.FreshStack(state)
    assert saved.read_text() == "old existing configuration"


@pytest.mark.parametrize("tamper", ["project", "path", "compose", "env", "symlink"])
def test_cleanup_attachment_requires_original_owned_snapshot(tmp_path, tamper):
    stack = e2e_stack.FreshStack(tmp_path / "fresh")
    marker_path = stack.state_dir / "stack-owner.json"
    marker = json.loads(marker_path.read_text())
    if tamper == "project":
        marker["project"] = "existing-native-demo"
    elif tamper == "path":
        marker["state_dir"] = str(tmp_path / "different")
    elif tamper == "compose":
        (stack.state_dir / "compose.e2e.yaml").write_text("services: {}")
    elif tamper == "env":
        (stack.state_dir / "stack.env").write_text("COMPOSE_PROJECT_NAME=existing-native-demo")
    else:
        original = stack.state_dir / "compose.e2e.yaml"
        contents = original.read_bytes()
        original.unlink()
        target = tmp_path / "foreign.yaml"
        target.write_bytes(contents)
        original.symlink_to(target)
    marker_path.write_text(json.dumps(marker))
    with pytest.raises(e2e_stack.StackError, match="OWNED_STACK_MANIFEST_REQUIRED"):
        e2e_stack.FreshStack.attach(stack.state_dir)


def test_compose_environment_does_not_import_ambient_stack_settings_or_tokens(monkeypatch):
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "old-project")
    monkeypatch.setenv("DB_OWNER_PASSWORD", "existing-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "host-model-secret")
    monkeypatch.setenv("DOCKER_CONTEXT", "test-context")
    cleaned = e2e_stack.compose_environment()
    assert "COMPOSE_PROJECT_NAME" not in cleaned and "DB_OWNER_PASSWORD" not in cleaned and "OPENAI_API_KEY" not in cleaned
    assert cleaned["DOCKER_CONTEXT"] == "test-context"


@pytest.mark.parametrize("tamper", ["public_port", "fixed_port", "db_port", "bind_mount", "privileged", "host_network", "image", "external_volume", "foreign_volume_name", "undeclared_volume", "owner"])
def test_unsafe_effective_compose_plan_is_refused_before_start(tmp_path, monkeypatch, tamper):
    stack = e2e_stack.FreshStack(tmp_path / "fresh")
    effective = plan(stack)
    service = effective["services"]["switch"]
    if tamper == "public_port":
        service["ports"][0]["host_ip"] = "0.0.0.0"
    elif tamper == "fixed_port":
        service["ports"][0]["published"] = "57897"
    elif tamper == "db_port":
        effective["services"]["postgres"]["ports"] = [{"target": 5432, "published": "5432"}]
    elif tamper == "bind_mount":
        service["volumes"] = [{"type": "bind", "source": str(Path.home()), "target": "/credentials"}]
    elif tamper == "privileged":
        service["privileged"] = True
    elif tamper == "host_network":
        service["network_mode"] = "host"
    elif tamper == "image":
        service["image"] = "unknown/switch:latest"
    elif tamper == "external_volume":
        effective["volumes"]["pgdata"]["external"] = True
    elif tamper == "foreign_volume_name":
        effective["volumes"]["pgdata"]["name"] = "existing_native_pgdata"
    elif tamper == "undeclared_volume":
        service["volumes"] = [{"type": "volume", "source": "existing_native_pgdata"}]
    else:
        service["labels"][e2e_stack.OWNER_LABEL] = "foreign-owner"
    monkeypatch.setattr(stack, "compose", lambda *args, **kwargs: json.dumps(effective))
    with pytest.raises(e2e_stack.StackError, match="UNSAFE_COMPOSE_PLAN"):
        stack.validate_config()


def test_owned_snapshot_passes_effective_plan_validation(tmp_path, monkeypatch):
    stack = e2e_stack.FreshStack(tmp_path / "fresh")
    monkeypatch.setattr(stack, "compose", lambda *args, **kwargs: json.dumps(plan(stack)))
    stack.validate_config()


def test_cleanup_refuses_foreign_label_without_running_down(tmp_path):
    commands = []
    def runner(args, **kwargs):
        commands.append(args)
        value = '[{"ID":"foreign-container"}]' if "ps" in args else '{"findback.e2e.owner":"someone-else"}'
        return subprocess.CompletedProcess(args, 0, stdout=value, stderr="")
    stack = e2e_stack.FreshStack(tmp_path / "fresh", runner=runner)
    with pytest.raises(e2e_stack.StackError, match="FOREIGN_RESOURCE_REFUSED"):
        stack.cleanup()
    assert not any("down" in args for args in commands)


def test_cleanup_only_targets_saved_project_and_checks_removal(tmp_path):
    commands = []
    def runner(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="[]" if "ps" in args else "", stderr="")
    stack = e2e_stack.FreshStack(tmp_path / "fresh", runner=runner)
    e2e_stack.FreshStack.attach(stack.state_dir, runner=runner).cleanup()
    down = next(args for args in commands if "down" in args)
    assert down[down.index("--project-name") + 1] == stack.project
    assert "--volumes" in down and "prune" not in " ".join(down)
    assert all("prune" not in args for args in commands)


@pytest.mark.parametrize("binding", ["0.0.0.0:1234", "localhost:1234", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:1234\n0.0.0.0:1234"])
def test_only_one_actual_loopback_port_is_accepted(binding):
    with pytest.raises(e2e_stack.StackError, match="PORT_BINDING"):
        e2e_stack.loopback_port(binding)


def test_actual_loopback_port_is_accepted():
    assert e2e_stack.loopback_port("127.0.0.1:47891\n") == "http://127.0.0.1:47891"


def test_command_failures_store_diagnostics_privately_but_raise_fixed_code(tmp_path):
    def runner(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="PRIVATE_OUT", stderr="PRIVATE_TOKEN")
    stack = e2e_stack.FreshStack(tmp_path / "fresh", runner=runner)
    with pytest.raises(e2e_stack.StackError, match="PINNED_IMAGE_PULL_FAILED") as raised:
        stack.compose("pull", code="PINNED_IMAGE_PULL_FAILED")
    assert "PRIVATE" not in str(raised.value)
    assert (stack.state_dir / "compose-last-error.log").stat().st_mode & 0o777 == 0o600


def test_source_manifest_pins_actual_source_and_records_changes(tmp_path):
    (tmp_path / "src/findback").mkdir(parents=True)
    (tmp_path / "infra/e2e").mkdir(parents=True)
    for name in ("src/findback/e2e_verify.py", "pyproject.toml", "uv.lock", "infra/e2e/compose.e2e.yaml", "infra/e2e/images.lock.json"):
        (tmp_path / name).write_text("initial source")
    initial = e2e_stack.source_manifest(tmp_path)
    assert "src/findback/e2e_verify.py" in initial["files_sha256"]
    (tmp_path / "src/findback/e2e_verify.py").write_text("changed source")
    changed = e2e_stack.source_manifest(tmp_path)
    assert changed["manifest_sha256"] != initial["manifest_sha256"]
    assert initial["files_sha256"]["uv.lock"] == changed["files_sha256"]["uv.lock"]


def test_readiness_rejects_failed_setup_even_when_process_is_up(tmp_path, monkeypatch):
    stack = e2e_stack.FreshStack(tmp_path / "fresh")
    monkeypatch.setattr(stack, "validate_config", lambda: None)
    monkeypatch.setattr(stack, "_listed", lambda _: [])
    checks = []
    def compose(*args, **kwargs):
        checks.append(args)
        if args[0] == "ps":
            return "[]" if len(checks) == 1 else '[{"Service":"setup","State":"exited","ExitCode":1}]'
        if args[0] == "port":
            return "127.0.0.1:47891"
        return ""
    monkeypatch.setattr(stack, "compose", compose)
    with pytest.raises(e2e_stack.StackError, match="OFFICIAL_SETUP_FAILED"):
        stack.start()


@pytest.mark.parametrize("interrupt", [False, True])
def test_cli_always_cleans_owned_resources_and_writes_safe_failure_report(tmp_path, monkeypatch, capsys, interrupt):
    state, report = tmp_path / "fresh", tmp_path / "report.json"
    cleaned = []
    original = e2e_stack.FreshStack
    class FailedStack(original):
        def start(self):
            if interrupt:
                raise KeyboardInterrupt()
            raise RuntimeError("PRIVATE provider/body credential")
        def cleanup(self):
            cleaned.append(self.project)
    monkeypatch.setattr(e2e_stack, "FreshStack", FailedStack)
    monkeypatch.setattr(sys, "argv", ["e2e_stack", "--state-dir", str(state), "--output", str(report)])
    with pytest.raises(SystemExit) as exited:
        e2e_stack.main()
    assert exited.value.code == 1 and len(cleaned) == 1
    result = json.loads(report.read_text())
    assert result["passed"] is False and result["teardown"] == "owned_resources_removed"
    assert result["error_code"] == ("INTERRUPTED" if interrupt else "RuntimeError")
    assert "PRIVATE" not in report.read_text() and "PRIVATE" not in capsys.readouterr().out
