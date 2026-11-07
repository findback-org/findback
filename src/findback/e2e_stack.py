"""Opt-in, fresh Docker Switch/MM verification; never borrows the native stack."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid

import httpx

from .bootstrap import _API, _save


OWNER_LABEL = "findback.e2e.owner"
PROJECT = re.compile(r"findback-e2e-[0-9a-f]{16}\Z")
ACTIVE = ("postgres", "init-db", "switch", "mattermost", "setup")
ASSETS = Path(__file__).resolve().parents[2] / "infra" / "e2e"


class StackError(RuntimeError):
    """Safe local diagnostic; raw command/API output stays in the private state."""


def sha256(filename: Path) -> str:
    return hashlib.sha256(filename.read_bytes()).hexdigest()


def json_records(value: str) -> list[dict]:
    try:
        parsed = json.loads(value)
    except ValueError:
        try:
            parsed = [json.loads(line) for line in value.splitlines() if line.strip()]
        except ValueError:
            raise StackError("COMPOSE_INVALID_RESULT") from None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not all(isinstance(row, dict) for row in parsed):
        raise StackError("COMPOSE_INVALID_RESULT")
    return parsed


def loopback_port(value: str) -> str:
    match = re.fullmatch(r"127\.0\.0\.1:([0-9]+)\s*", value)
    if not match or not 0 < int(match[1]) < 65536:
        raise StackError("UNSAFE_OR_MISSING_PORT_BINDING")
    return "http://127.0.0.1:" + match[1]


def compose_environment() -> dict:
    # Ambient COMPOSE_*, DB_* and service credentials must not override our
    # explicit env file. Docker client settings locate the user's daemon only.
    allowed = {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
               "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "SYSTEMROOT", "TMPDIR", "LANG", "LC_ALL"}
    return {key: value for key, value in os.environ.items() if key in allowed}


def source_manifest(root: Path = ASSETS.parents[1]) -> dict:
    """Pin the actual host source, including uncommitted E2E implementation."""
    files = sorted((root / "src" / "findback").rglob("*.py"))
    files += [root / "pyproject.toml", root / "uv.lock"]
    files += sorted((root / "infra" / "e2e").glob("*.yaml"))
    files += [root / "infra" / "e2e" / "images.lock.json"]
    hashes = {str(filename.relative_to(root)): sha256(filename) for filename in files}
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        commit = result.stdout.strip() if result.returncode == 0 else None
        if commit is not None and not re.fullmatch(r"[0-9a-f]{40}", commit):
            commit = None
    except (OSError, subprocess.TimeoutExpired):
        commit = None
    return {"base_git_commit": commit, "files_sha256": hashes,
            "manifest_sha256": hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


class FreshStack:
    def __init__(self, state_dir: Path, *, assets: Path = ASSETS, runner=subprocess.run):
        self.state_dir = Path(state_dir).expanduser().resolve()
        if self.state_dir.exists():
            raise StackError("FRESH_STATE_DIRECTORY_REQUIRED")
        self.state_dir.mkdir(mode=0o700, parents=True)
        self.runner = runner
        self.owner = uuid.uuid4().hex
        self.project = "findback-e2e-" + self.owner[:16]
        self.lock = json.loads((assets / "images.lock.json").read_text())
        if sha256(assets / "compose.upstream.yaml") != self.lock["upstream_compose_sha256"]:
            raise StackError("UPSTREAM_COMPOSE_HASH_MISMATCH")
        for name in ("compose.upstream.yaml", "compose.e2e.yaml", "images.lock.json"):
            shutil.copyfile(assets / name, self.state_dir / name)
            os.chmod(self.state_dir / name, 0o600)
        self.env = {
            "SWITCH_VERSION": "0.31.0", "SWITCH_BIND_ADDR": "127.0.0.1",
            "API_HOST_PORT": "0", "MATTERMOST_HOST_PORT": "0", "POSTGRES_HOST_PORT": "0",
            "DB_USER": "switch_app", "DB_OWNER_USER": "postgres", "DB_NAME": "switch",
            "DB_PASSWORD": secrets.token_hex(24), "DB_OWNER_PASSWORD": secrets.token_hex(24),
            "AGENT_REGISTRATION_TOKEN": secrets.token_hex(24), "SECRET_KEYS": "e2e:" + secrets.token_hex(32),
            "GATEWAY_ADMIN_EMAIL": "admin@findback.invalid", "GATEWAY_ADMIN_PASSWORD": secrets.token_hex(24),
            "MATTERMOST_ADMIN_USER": "e2e-admin", "MATTERMOST_ADMIN_PASSWORD": secrets.token_hex(24),
            "MATTERMOST_TEAM_NAME": "findback", "MATTERMOST_USER": "e2e-owner",
            "MATTERMOST_USER_PASSWORD": secrets.token_hex(24), "FRONTEND_BASE_URL": "http://127.0.0.1",
            "FINDBACK_E2E_OWNER": self.owner, "TELEMETRY_ENABLED": "false", "THIRD_PARTY_AVATARS_ENABLED": "false",
        }
        env_file = self.state_dir / "stack.env"
        env_file.write_text("".join(f"{key}={value}\n" for key, value in self.env.items()))
        os.chmod(env_file, 0o600)
        _save(self.state_dir / "stack-owner.json", {
            "schema": 1, "project": self.project, "owner": self.owner,
            "state_dir": str(self.state_dir), "created_at": datetime.now(timezone.utc).isoformat(),
            "files": {name: sha256(self.state_dir / name) for name in
                      ("compose.upstream.yaml", "compose.e2e.yaml", "images.lock.json", "stack.env")}})

    @classmethod
    def attach(cls, state_dir: Path, *, runner=subprocess.run) -> "FreshStack":
        instance = cls.__new__(cls)
        instance.state_dir = Path(state_dir).expanduser().resolve()
        instance.runner = runner
        try:
            marker = json.loads((instance.state_dir / "stack-owner.json").read_text())
            instance.project, instance.owner = marker["project"], marker["owner"]
            if (not PROJECT.fullmatch(instance.project) or not re.fullmatch(r"[0-9a-f]{32}", instance.owner)
                    or instance.project != "findback-e2e-" + instance.owner[:16]
                    or marker["state_dir"] != str(instance.state_dir)):
                raise ValueError()
            expected = {"compose.upstream.yaml", "compose.e2e.yaml", "images.lock.json", "stack.env"}
            if set(marker["files"]) != expected:
                raise ValueError()
            for name, expected_hash in marker["files"].items():
                target = instance.state_dir / name
                if target.is_symlink() or sha256(target) != expected_hash:
                    raise ValueError()
            instance.lock = json.loads((instance.state_dir / "images.lock.json").read_text())
        except (OSError, ValueError, KeyError, TypeError):
            raise StackError("OWNED_STACK_MANIFEST_REQUIRED") from None
        return instance

    def _base(self) -> list[str]:
        return ["docker", "compose", "--project-name", self.project, "--project-directory", str(self.state_dir),
                "--env-file", str(self.state_dir / "stack.env"), "--profile", "collab",
                "-f", str(self.state_dir / "compose.upstream.yaml"), "-f", str(self.state_dir / "compose.e2e.yaml")]

    def _run(self, command: list[str], *, code: str, timeout: int = 90) -> str:
        try:
            result = self.runner(command, capture_output=True, text=True, timeout=timeout, env=compose_environment())
        except (OSError, subprocess.TimeoutExpired):
            raise StackError(code) from None
        if result.returncode:
            # Diagnostics can contain setup secrets: local0600 only, never report.
            logfile = self.state_dir / "compose-last-error.log"
            logfile.write_text(str(result.stderr)[-200_000:])
            os.chmod(logfile, 0o600)
            raise StackError(code)
        return result.stdout

    def compose(self, *arguments: str, code="COMPOSE_COMMAND_FAILED", timeout=90) -> str:
        return self._run(self._base() + list(arguments), code=code, timeout=timeout)

    def validate_config(self) -> None:
        try:
            plan = json.loads(self.compose("config", "--format", "json", code="COMPOSE_CONFIG_INVALID"))
            services = plan["services"]
            if plan.get("name") != self.project:
                raise ValueError()
            for name in ACTIVE:
                service = services[name]
                expected_image = self.lock["images"]["postgres" if name == "init-db" else name]
                if (service["image"] != expected_image or service.get("privileged")
                        or service.get("network_mode") == "host"
                        or service.get("labels", {}).get(OWNER_LABEL) != self.owner):
                    raise ValueError()
                for mount in service.get("volumes", []):
                    if mount.get("type") != "volume" or mount.get("source") not in plan.get("volumes", {}):
                        raise ValueError()
                ports = service.get("ports", [])
                if name in {"switch", "mattermost"}:
                    target = 8000 if name == "switch" else 8065
                    if (len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1"
                            or str(ports[0].get("published")) != "0" or ports[0].get("target") != target):
                        raise ValueError()
                elif ports:
                    raise ValueError()
            for resource in [*plan.get("volumes", {}).values(), *plan.get("networks", {}).values()]:
                if (resource.get("external") or resource.get("labels", {}).get(OWNER_LABEL) != self.owner
                        or not resource.get("name", "").startswith(self.project + "_")):
                    raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise StackError("UNSAFE_COMPOSE_PLAN") from None

    def _listed(self, kind: str) -> list[str]:
        return self._run(["docker", kind, "ls", "--filter", "label=com.docker.compose.project=" + self.project,
                          "--quiet"], code="RESOURCE_INSPECTION_FAILED").split()

    def _assert_owned_resources(self) -> dict:
        ids = [record["ID"] for record in json_records(self.compose("ps", "--all", "--format", "json"))]
        counts = {"containers": len(ids)}
        if ids:
            labels = json_records(self._run(["docker", "inspect", "--format", "{{json .Config.Labels}}", *ids],
                                           code="RESOURCE_INSPECTION_FAILED"))
            if any(row.get(OWNER_LABEL) != self.owner or row.get("com.docker.compose.project") != self.project for row in labels):
                raise StackError("FOREIGN_RESOURCE_REFUSED")
        for kind in ("volume", "network"):
            names = self._listed(kind)
            counts[kind + "s"] = len(names)
            if names:
                records = json_records(self._run(["docker", kind, "inspect", *names], code="RESOURCE_INSPECTION_FAILED"))
                if any(row.get("Labels", {}).get(OWNER_LABEL) != self.owner for row in records):
                    raise StackError("FOREIGN_RESOURCE_REFUSED")
        return counts

    def start(self) -> dict:
        self.validate_config()
        if json_records(self.compose("ps", "--all", "--format", "json")) or self._listed("volume") or self._listed("network"):
            raise StackError("PROJECT_ALREADY_EXISTS")
        self.compose("pull", *ACTIVE, code="PINNED_IMAGE_PULL_FAILED", timeout=900)
        self.compose("up", "--detach", "postgres", "switch", "mattermost", "setup", code="STACK_START_FAILED", timeout=180)
        self.switch_url = loopback_port(self.compose("port", "switch", "8000"))
        self.mattermost_url = loopback_port(self.compose("port", "mattermost", "8065"))
        deadline = time.monotonic() + 360
        with httpx.Client(timeout=3, follow_redirects=False) as http:
            while time.monotonic() < deadline:
                states = json_records(self.compose("ps", "--all", "--format", "json"))
                setup = next((row for row in states if row.get("Service") == "setup"), None)
                if setup and setup.get("State") == "exited" and setup.get("ExitCode") != 0:
                    raise StackError("OFFICIAL_SETUP_FAILED")
                try:
                    ready = all(http.get(url).status_code == 200 for url in
                                (self.switch_url + "/health", self.mattermost_url + "/api/v4/system/ping"))
                except httpx.HTTPError:
                    ready = False
                if ready and setup and setup.get("State") == "exited" and setup.get("ExitCode") == 0:
                    self.start_evidence = {"preexisting_project_resources": 0, "official_setup_exit_code": 0,
                        "core_health_http_status": 200, "mattermost_ping_http_status": 200,
                        "owned_resources": self._assert_owned_resources()}
                    return {"switch_url": self.switch_url, "mattermost_url": self.mattermost_url}
                time.sleep(1)
        raise StackError("STACK_READINESS_TIMEOUT")

    def metadata(self) -> dict:
        # Setup-only read from THIS fresh project. Internal client/matrix IDs
        # required by Core0.31 source hashing are not exposed in gateway lists.
        query = """SELECT json_build_object(
          'bridges',(SELECT coalesce(json_agg(json_build_object('bridge_id',id,'tenant_id',tenant_id)),'[]') FROM collaboration_bridges WHERE type='mattermost'),
          'humans',(SELECT coalesce(json_agg(json_build_object('client_id',c.id,'actor_id',c.matrix_user_id,'external_identity_id',e.id,'external_user_id',e.external_user_id,'bridge_id',e.bridge_id)),'[]') FROM external_users e JOIN clients c ON c.id=e.client_id AND c.tenant_id=e.tenant_id),
          'agents',(SELECT coalesce(json_agg(json_build_object('id',a.id,'client_id',c.id,'actor_id',c.matrix_user_id,'owner_id',a.owner_id)),'[]') FROM agents a JOIN clients c ON c.id=a.client_id AND c.tenant_id=a.tenant_id));"""
        try:
            return json.loads(self.compose("exec", "--no-TTY", "postgres", "psql", "--set=ON_ERROR_STOP=1",
                "--username=postgres", "--dbname=switch", "--tuples-only", "--no-align", "--command", query,
                code="FRESH_IDENTITY_LOOKUP_FAILED"))
        except ValueError:
            raise StackError("FRESH_IDENTITY_LOOKUP_FAILED") from None

    def cleanup(self) -> None:
        # Revalidate the original immutable snapshot before any destructive call.
        FreshStack.attach(self.state_dir, runner=self.runner)
        self._assert_owned_resources()
        self.compose("down", "--volumes", "--timeout", "20", code="OWNED_STACK_CLEANUP_FAILED", timeout=120)
        if json_records(self.compose("ps", "--all", "--format", "json")) or self._listed("volume") or self._listed("network"):
            raise StackError("OWNED_STACK_CLEANUP_INCOMPLETE")


def provision(stack: FreshStack) -> dict:
    """Fresh HTTP enrollment only; never invokes native Mac bootstrap/metadata."""
    env, run_id = stack.env, stack.owner[:8]
    state = {"run_id": run_id, "owners": {}, "agents": {}, "humans": {}, "rooms": {}}
    with ExitStack() as opened:
        def api(url):
            client = _API(url)
            opened.callback(client.close)
            return client
        admin, mm = api(stack.switch_url), api(stack.mattermost_url)
        login = admin.gateway_login(env["GATEWAY_ADMIN_EMAIL"], env["GATEWAY_ADMIN_PASSWORD"])
        if login.get("server", {}).get("version") != "0.31.0":
            raise StackError("UNEXPECTED_SWITCH_VERSION")
        mm.mm_login(env["MATTERMOST_ADMIN_USER"], env["MATTERMOST_ADMIN_PASSWORD"])
        team = mm.json("GET", "/api/v4/teams/name/" + env["MATTERMOST_TEAM_NAME"])
        bridges = stack.metadata()["bridges"]
        if len(bridges) != 1:
            raise StackError("FRESH_MATTERMOST_BRIDGE_REQUIRED")
        bridge = bridges[0]
        owners = {}
        for key in ("service", "matcher", "intake_priya", "intake_alex"):
            owner = {"name": "E2E " + key, "email": f"{key}-{run_id}@findback.invalid", "password": secrets.token_hex(24)}
            created = admin.json("POST", "/gateway/users", {**owner, "role": "user"})
            owner["id"] = created["id"]
            state["owners"][key] = owner
            owners[key] = api(stack.switch_url)
            authenticated = owners[key].gateway_login(owner["email"], owner["password"])
            if authenticated["id"] != owner["id"] or authenticated.get("role") == "admin":
                raise StackError("FRESH_OWNER_IDENTITY_MISMATCH")
            state["agents"][key] = owners[key].json("POST", "/gateway/agents/register-other", {
                "name": f"fb-e2e-{run_id}-{key.replace('_','-')}", "display_name": "E2E " + key,
                "description": "Isolated FindBack verification identity", "overwrite": False})
        for key in ("priya", "alex", "staff"):
            human = {"username": f"fb-e2e-{run_id}-{key}", "email": f"{run_id}-{key}@findback.invalid", "password": secrets.token_hex(24)}
            created = mm.json("POST", "/api/v4/users", human)
            human["id"] = created["id"]
            mm.json("POST", f"/api/v4/teams/{team['id']}/members", {"team_id": team["id"], "user_id": human["id"]})
            account, human["token"] = api(stack.mattermost_url).mm_login(human["username"], human["password"])
            if account["id"] != human["id"] or account.get("is_bot") or "system_admin" in account.get("roles", "").split():
                raise StackError("FRESH_HUMAN_IDENTITY_MISMATCH")
            state["humans"][key] = human
        for cid, owner_key, human_key in (("C001", "intake_priya", "priya"), ("C002", "intake_alex", "alex"), ("staff", "matcher", "staff")):
            room = owners[owner_key].json("POST", "/gateway/rooms", {
                "name": f"FindBack E2E {run_id} {cid}", "description": "Isolated fictional verification room",
                "channel_type": "channel_private", "read_visibility": "private", "write_visibility": "private",
                "bridge_id": bridge["bridge_id"], "agent_ids": [state["agents"][owner_key]["id"], state["agents"]["service"]["id"]],
                "user_names": [state["humans"][human_key]["username"]]})
            if any(room.get(key) != "private" for key in ("read_visibility", "write_visibility")) or room.get("channel_type") != "channel_private":
                raise StackError("FRESH_ROOM_NOT_PRIVATE")
            channel = mm.json("GET", "/api/v4/channels/" + room["external_channel_id"])
            if channel.get("type") != "P":
                raise StackError("FRESH_CHANNEL_NOT_PRIVATE")
            state["rooms"][cid] = room
        _save(stack.state_dir / "provisioning.json", state)
        deadline = time.monotonic() + 60
        while True:
            identities = stack.metadata()
            humans = {h["external_user_id"]: h for h in identities["humans"] if h["bridge_id"] == bridge["bridge_id"]}
            agents = {a["id"]: a for a in identities["agents"]}
            if all(h["id"] in humans for h in state["humans"].values()) and all(a["id"] in agents for a in state["agents"].values()):
                break
            if time.monotonic() >= deadline:
                raise StackError("FRESH_IDENTITY_ENROLLMENT_TIMEOUT")
            time.sleep(.25)
        def human(key):
            value = state["humans"][key]
            return {**value, "client_id": humans[value["id"]]["client_id"], "external_identity_id": humans[value["id"]]["external_identity_id"]}
        def agent(key):
            value = state["agents"][key]
            bound = agents[value["id"]]
            if bound["owner_id"] != state["owners"][key]["id"]:
                raise StackError("FRESH_AGENT_OWNER_MISMATCH")
            return {**value, **bound}
        config = {"schema_version": 1, "switch_url": stack.switch_url, "mattermost_url": stack.mattermost_url,
            "bridge_id": bridge["bridge_id"], "tenant_id": bridge["tenant_id"], "workspace_kind": "isolated_fresh_docker_stack",
            "e2e_stack": {"fresh": True, "project_name": stack.project},
            "staff_room": {"id": state["rooms"]["staff"]["id"], "channel_id": state["rooms"]["staff"]["external_channel_id"]},
            "staff": {"actor_id": humans[state["humans"]["staff"]["id"]]["actor_id"], "human": human("staff")},
            "agents": {key: agent(key) for key in ("service", "matcher")}, "cases": {}}
        for cid, key in (("C001", "priya"), ("C002", "alex")):
            config["cases"][cid] = {"room_id": state["rooms"][cid]["id"], "channel_id": state["rooms"][cid]["external_channel_id"],
                "claimant_actor_id": humans[state["humans"][key]["id"]]["actor_id"], "human": human(key), "intake_agent": agent("intake_" + key)}
        service_id = config["agents"]["service"]["id"]
        rules = {"service": []}
        for cid, owner_key in (("C001", "intake_priya"), ("C002", "intake_alex"), ("staff", "matcher")):
            room = config["staff_room"]["id"] if cid == "staff" else config["cases"][cid]["room_id"]
            person = config["staff"]["human"] if cid == "staff" else config["cases"][cid]["human"]
            common = {"rooms": [room], "room_groups": "*", "users": [person["external_identity_id"]],
                      "owner": False, "owner_agents": False, "platform": False}
            rules[owner_key] = [{**common, "agents": [service_id]}]
            rules["service"].append({**common, "agents": []})
        for key, policy in rules.items():
            response = owners[key].json("PUT", f"/gateway/agents/{state['agents'][key]['id']}/addressing-policy", {"policy": {"rules": policy}})
            if response.get("addressing_policy") != {"rules": policy}:
                raise StackError("FRESH_ADDRESSING_POLICY_MISMATCH")
        config["addressing_policy"] = "explicit_human_and_room_allowlists; service may address role agents"
        _save(stack.state_dir / "config.json", config)
        return config


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, help="Must not exist; default .findback/e2e-<random>")
    parser.add_argument("--model", choices=["fixture", "codex"], default="fixture")
    parser.add_argument("--output", type=Path, help="Required for a run: redacted machine-readable report")
    parser.add_argument("--keep-stack", action="store_true", help="Keep only this owned project for private debugging")
    parser.add_argument("--cleanup", type=Path, help="Tear down only a saved, ownership-validated E2E project")
    args = parser.parse_args()
    if args.cleanup:
        FreshStack.attach(args.cleanup).cleanup()
        print(json.dumps({"owned_stack_removed": True}))
        return
    if args.output is None:
        parser.error("--output is required for a verification run")
    state = args.state_dir or Path(".findback") / ("e2e-" + uuid.uuid4().hex[:12])
    stack = FreshStack(state)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "passed": False,
              "stack": {"kind": "fresh_isolated_docker", "source_commit": stack.lock["switch_source_commit"],
                        "images": stack.lock["images"], "project": stack.project, "borrowed_native_resources": False,
                        "host_credentials_mounted": False}, "model_mode": args.model,
              "findback_source": source_manifest()}
    try:
        print("E2E: starting isolated pinned services", file=sys.stderr, flush=True)
        stack.start()
        report["stack"]["startup"] = stack.start_evidence
        config = provision(stack)
        from .e2e_verify import run_scenario
        result = asyncio.run(run_scenario(config, stack.state_dir, model_mode=args.model))
        report["scenario"] = result
        report["passed"] = result.get("passed") is True
        if not report["passed"]:
            report["error_code"] = "SCENARIO_FAILED"
    except KeyboardInterrupt:
        report["error_code"] = "INTERRUPTED"
    except Exception as exc:
        report["error_code"] = str(exc) if isinstance(exc, StackError) else type(exc).__name__
    finally:
        report["source_unchanged_during_run"] = source_manifest()["manifest_sha256"] == report["findback_source"]["manifest_sha256"]
        if not report["source_unchanged_during_run"]:
            report["passed"] = False
            report["error_code"] = "SOURCE_CHANGED_DURING_RUN"
        if args.keep_stack:
            report["teardown"] = "kept_by_explicit_request"
        else:
            try:
                stack.cleanup()
                report["teardown"] = "owned_resources_removed"
            except Exception:
                report["teardown"] = "cleanup_failed_operator_attention"
                report["passed"] = False
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        _save(stack.state_dir / "verification-report.json", report)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"passed": report["passed"], "output": str(args.output), "private_state": str(stack.state_dir),
                      "teardown": report["teardown"], "error_code": report.get("error_code")}))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
