"""Operator-only local demo enrollment for Switch Console's native runtime.

The privileged native environment is read only during setup. Runtime config
contains scoped agent keys and fictional demo users, never Core/DB admin keys.
This creates private rooms in the existing local tenant, not a separate tenant.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import tempfile
import time
from typing import Any
import uuid

import httpx


class BootstrapError(RuntimeError):
    """Safe setup failure without credentials or arbitrary remote response text."""


_NATIVE = Path.home() / "Library/Application Support/switchdash/local-switch-server"
_BUNDLE = Path("/Applications/Switch Console.app/Contents/Resources/local-server")


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(prefix=".findback-setup-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if value.startswith(("'", '"')):
            parts = shlex.split(value)
            value = parts[0] if parts else ""
        values[key.strip()] = value
    return values


# Runs only as an operator setup subprocess with bundled asyncpg/cryptography.
# Stdout is captured in memory, never printed or added wholesale to config.
_METADATA = r'''
import asyncio,json,os,shlex,sys
from pathlib import Path
import asyncpg
from switch_core.keys import Keyring
async def main():
 req=json.load(sys.stdin); env={}
 for line in Path(req['env_path']).read_text().splitlines():
  if line.strip() and not line.lstrip().startswith('#') and '=' in line:
   key,value=line.split('=',1)
   if value.startswith(('"',"'")):
    parts=shlex.split(value);value=parts[0] if parts else ''
   env[key.strip()]=value
 con=await asyncpg.connect(host=env['DB_HOST'],port=int(env['DB_PORT']),
  user=env.get('DB_OWNER_USER') or env['DB_USER'],
  password=env.get('DB_OWNER_PASSWORD') or env['DB_PASSWORD'],database=env['DB_NAME'])
 try:
  async with con.transaction(readonly=True):
   if req['mode']=='bridge':
    if req.get('bridge_id'):
     row=await con.fetchrow('SELECT id,tenant_id,connection_config FROM collaboration_bridges WHERE id=$1 AND type=$2',req['bridge_id'],'mattermost')
    else:
     row=await con.fetchrow('SELECT id,tenant_id,connection_config FROM collaboration_bridges WHERE type=$1 ORDER BY is_default DESC LIMIT 1','mattermost')
    if not row: raise ValueError('missing local Mattermost bridge')
    config=row['connection_config'];config=json.loads(config) if isinstance(config,str) else config
    if isinstance(config,dict) and set(config)=={'_enc'}:
     ring=Keyring.parse(env['SECRET_KEYS'],legacy_secret=env.get('JWT_SECRET_KEY'))
     config=json.loads(ring.decrypt(config['_enc']))
    result={'bridge_id':row['id'],'tenant_id':row['tenant_id'],'connection_config':config}
   else:
    humans=await con.fetch('SELECT c.id AS client_id,c.matrix_user_id AS actor_id,e.id AS external_identity_id,e.external_user_id FROM external_users e JOIN clients c ON c.id=e.client_id AND c.tenant_id=e.tenant_id WHERE e.bridge_id=$1 AND e.external_user_id=ANY($2::text[])',req['bridge_id'],req['human_ids'])
    agents=await con.fetch('SELECT a.id,c.id AS client_id,c.matrix_user_id AS actor_id,a.owner_id FROM agents a JOIN clients c ON c.id=a.client_id AND c.tenant_id=a.tenant_id WHERE a.id=ANY($1::text[])',req['agent_ids'])
    result={'humans':[dict(r) for r in humans],'agents':[dict(r) for r in agents]}
   print(json.dumps(result))
 finally: await con.close()
try: asyncio.run(main())
except Exception:
 print('Native setup metadata lookup failed.',file=sys.stderr);sys.exit(1)
'''


def _metadata(mode: str, **kwargs: Any) -> dict:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join([
        str(_BUNDLE / "switch/site-packages"), str(_BUNDLE / "switch/src/core")])
    request = {"mode": mode, "env_path": str(_NATIVE / "native/run/switch-core.env"), **kwargs}
    try:
        result = subprocess.run([str(_BUNDLE / "python/bin/python3"), "-c", _METADATA],
                                input=json.dumps(request), capture_output=True, text=True,
                                timeout=20, env=environment)
        if result.returncode:
            raise BootstrapError("Native setup metadata lookup failed; check the local Switch runtime.")
        return json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise BootstrapError("Could not inspect the installed native Switch runtime.") from None


class _API:
    def __init__(self, base: str):
        self.client = httpx.Client(base_url=base.rstrip("/"), timeout=40, follow_redirects=False)

    def call(self, method: str, path: str, data: dict | None = None, *, allow: tuple[int, ...] = ()):
        try:
            response = self.client.request(method, path, json=data)
        except httpx.HTTPError:
            raise BootstrapError("A local setup request failed; rerun setup to reconcile saved resources.") from None
        if response.status_code in allow:
            return response
        if response.status_code >= 300:
            raise BootstrapError(f"Local setup API refused {method} {path.split('?')[0]} (status {response.status_code}).")
        return response

    def json(self, method: str, path: str, data: dict | None = None):
        response = self.call(method, path, data)
        try:
            return response.json()
        except ValueError:
            raise BootstrapError("Local setup API returned an invalid result.") from None

    def gateway_login(self, email: str, password: str) -> dict:
        return self.json("POST", "/gateway/auth/login", {"email": email, "password": password})

    def mm_login(self, login_id: str, password: str) -> tuple[dict, str]:
        response = self.call("POST", "/api/v4/users/login", {"login_id": login_id, "password": password})
        token = response.headers.get("Token")
        if not token:
            raise BootstrapError("Mattermost did not return an authentication token.")
        self.client.headers["Authorization"] = "Bearer " + token
        return response.json(), token

    def close(self):
        self.client.close()


def configure_local_addressing(config: dict, state_dir: Path) -> dict:
    """Set explicit per-room address allowlists without opening a connection.

    Address denial demotes a mention to ordinary room chatter in Core 0.31;
    these rules supplement membership, never replace domain authorization.
    """
    state = json.loads((Path(state_dir) / "provisioning.json").read_text())
    human_entries = [("staff", config["staff"]["human"])] + [
        (key, case["human"]) for key, case in config["cases"].items()]
    bindings = _metadata("identities", bridge_id=config["bridge_id"],
                         human_ids=[human["id"] for _, human in human_entries], agent_ids=[])
    by_mm_id = {value["external_user_id"]: value for value in bindings["humans"]}
    rules_by_agent: dict[str, list[dict]] = {"service": []}
    service_id = config["agents"]["service"]["id"]
    for case_id, owner_key in (("C001", "intake_priya"), ("C002", "intake_alex"), ("staff", "matcher")):
        room = config["staff_room"]["id"] if case_id == "staff" else config["cases"][case_id]["room_id"]
        human = config["staff"]["human"] if case_id == "staff" else config["cases"][case_id]["human"]
        human["external_identity_id"] = by_mm_id[human["id"]]["external_identity_id"]
        rules_by_agent[owner_key] = [{"rooms": [room], "room_groups": "*",
                                     "users": [human["external_identity_id"]], "agents": [service_id],
                                     "owner": False, "owner_agents": False, "platform": False}]
        rules_by_agent["service"].append({"rooms": [room], "room_groups": "*",
                                           "users": [human["external_identity_id"]], "agents": [],
                                           "owner": False, "owner_agents": False, "platform": False})
    for owner_key, rules in rules_by_agent.items():
        api = _API(config["switch_url"])
        try:
            owner = state["owners"][owner_key]
            api.gateway_login(owner["email"], owner["password"])
            agent_id = state["agents"][owner_key]["id"]
            response = api.json("PUT", f"/gateway/agents/{agent_id}/addressing-policy", {"policy": {"rules": rules}})
            if response.get("addressing_policy") != {"rules": rules}:
                raise BootstrapError("Switch did not retain the configured addressing allowlist.")
        finally:
            api.close()
    config["addressing_policy"] = "explicit_human_and_room_allowlists; service may address role agents"
    for case_id, case in config["cases"].items():
        case["url"] = state["rooms"][case_id].get("external_channel_url")
    config["staff_room"]["url"] = state["rooms"]["staff"].get("external_channel_url")
    _save(Path(state_dir) / "config.json", config)
    return config


def bootstrap_local(state_dir: Path) -> dict:
    """Create/reuse fictional private demo enrollment and save config.json (0600).

    Resources are checkpointed after each successful creation. Existing state is
    reused; this does not delete rooms, replace agents, or reset human passwords.
    Only this operator function reads native admin credentials/identity metadata.
    """
    state_dir = Path(state_dir)
    config_path = state_dir / "config.json"
    checkpoint = state_dir / "provisioning.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
        os.chmod(config_path, 0o600)
        return configure_local_addressing(config, state_dir)
    if not (_NATIVE / "native/run/switch-core.env").exists():
        raise BootstrapError("Start the native local server in Switch Console before setup.")
    ports = json.loads((_NATIVE / "ports.json").read_text())
    env = _env(_NATIVE / "native/run/switch-core.env")
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {
        "run_id": uuid.uuid4().hex[:8], "owners": {}, "agents": {}, "humans": {}, "rooms": {}}
    _save(checkpoint, state)
    spike_path = state_dir / "spike.json"
    spike = json.loads(spike_path.read_text()) if spike_path.exists() else {}
    bridge = _metadata("bridge", bridge_id=spike.get("room", {}).get("bridge_id"))
    switch_url = f"http://127.0.0.1:{ports['api']}"
    mattermost_url = f"http://127.0.0.1:{ports['mattermost']}"
    connection = bridge["connection_config"]
    admin, mm = _API(switch_url), _API(mattermost_url)
    owners: dict[str, _API] = {}
    try:
        admin.gateway_login(env["GATEWAY_ADMIN_EMAIL"], env["GATEWAY_ADMIN_PASSWORD"])
        mm.mm_login(connection["admin_user"], connection["admin_password"])
        team = mm.json("GET", "/api/v4/teams/name/" + connection["team_name"])
        for key, label in (("intake_priya", "Intake Priya"), ("intake_alex", "Intake Alex"),
                           ("matcher", "Matcher"), ("service", "Service")):
            owner = state["owners"].get(key)
            if owner is None:
                owner = {"name": "FindBack " + label + " owner",
                         "email": f"findback-{state['run_id']}-{key}@demo.invalid",
                         "password": secrets.token_urlsafe(24)}
                state["owners"][key] = owner
                _save(checkpoint, state)
            if "id" not in owner:
                response = admin.call("POST", "/gateway/users", {**owner, "role": "user"}, allow=(409,))
                if response.status_code != 409:
                    owner["id"] = response.json()["id"]
                    _save(checkpoint, state)
            api = _API(switch_url)
            owners[key] = api
            login = api.gateway_login(owner["email"], owner["password"])
            owner["id"] = login["id"]
            _save(checkpoint, state)
            if key not in state["agents"]:
                agent = api.json("POST", "/gateway/agents/register-other", {
                    "name": f"findback-{state['run_id']}-{key.replace('_', '-')}",
                    "description": f"FindBack {label}; constrained application transport identity.",
                    "display_name": "FindBack " + label, "overwrite": False})
                state["agents"][key] = agent
                _save(checkpoint, state)
        for key, label in (("priya", "Priya"), ("alex", "Alex"), ("staff", "Custodian")):
            human = state["humans"].get(key)
            if human is None:
                if key == "priya" and spike.get("human"):
                    human = dict(spike["human"])
                else:
                    human = {"username": f"findback-{key}-{state['run_id']}",
                             "password": secrets.token_urlsafe(24),
                             "email": f"findback-{state['run_id']}-{key}@demo.invalid"}
                state["humans"][key] = human
                _save(checkpoint, state)
            if "id" not in human:
                created = mm.json("POST", "/api/v4/users", {
                    "username": human["username"], "password": human["password"],
                    "email": human["email"], "first_name": label})
                human["id"] = created["id"]
                _save(checkpoint, state)
            membership = mm.call("GET", f"/api/v4/teams/{team['id']}/members/{human['id']}", allow=(404,))
            if membership.status_code == 404:
                mm.json("POST", f"/api/v4/teams/{team['id']}/members", {
                    "team_id": team["id"], "user_id": human["id"]})
            user_api = _API(mattermost_url)
            try:
                user, token = user_api.mm_login(human["username"], human["password"])
                if user["id"] != human["id"]:
                    raise BootstrapError("A saved demo user binding changed; setup halted.")
                human["token"] = token
                _save(checkpoint, state)
            finally:
                user_api.close()
        for key, owner_key, human_key, label in (
            ("C001", "intake_priya", "priya", "Priya"),
            ("C002", "intake_alex", "alex", "Alex"),
            ("staff", "matcher", "staff", "Staff")):
            if key not in state["rooms"]:
                room = owners[owner_key].json("POST", "/gateway/rooms", {
                    "name": f"FindBack {label} {state['run_id']}",
                    "description": "Fictional FindBack demo. Private " + ("staff inventory and review." if key == "staff" else "claimant recovery case."),
                    "channel_type": "channel_private", "read_visibility": "private", "write_visibility": "private",
                    "bridge_id": bridge["bridge_id"],
                    "agent_ids": [state["agents"][owner_key]["id"], state["agents"]["service"]["id"]],
                    "user_names": [state["humans"][human_key]["username"]]})
                state["rooms"][key] = room
                _save(checkpoint, state)
            room = state["rooms"][key]
            if room["read_visibility"] != "private" or room["write_visibility"] != "private" or room["channel_type"] != "channel_private":
                raise BootstrapError("A configured room is not private; setup halted.")
            channel = mm.json("GET", "/api/v4/channels/" + room["external_channel_id"])
            if channel["type"] != "P":
                raise BootstrapError("A Mattermost channel is not private; setup halted.")
        human_ids = [h["id"] for h in state["humans"].values()]
        for attempt in range(30):
            bindings = _metadata("identities", bridge_id=bridge["bridge_id"], human_ids=human_ids,
                                 agent_ids=[a["id"] for a in state["agents"].values()])
            if len(bindings["humans"]) == len(human_ids) and len(bindings["agents"]) == len(state["agents"]):
                break
            time.sleep(0.25)
        else:
            raise BootstrapError("Switch has not enrolled all demo identities; rerun setup shortly.")
        humans_by_id = {h["external_user_id"]: h for h in bindings["humans"]}
        agents_by_id = {a["id"]: a for a in bindings["agents"]}
        def agent_config(key: str) -> dict:
            value = dict(state["agents"][key])
            binding = agents_by_id[value["id"]]
            if binding["owner_id"] != state["owners"][key]["id"]:
                raise BootstrapError("An agent owner binding changed; setup halted.")
            return {**value, "actor_id": binding["actor_id"], "client_id": binding["client_id"],
                    "owner_id": binding["owner_id"]}
        def human_config(key: str) -> dict:
            human = dict(state["humans"][key])
            binding = humans_by_id[human["id"]]
            return {**human, "client_id": binding["client_id"]}
        config = {"schema_version": 1, "switch_url": switch_url, "mattermost_url": mattermost_url,
                  "bridge_id": bridge["bridge_id"], "tenant_id": bridge["tenant_id"],
                  "workspace_kind": "private_rooms_existing_local_tenant",
                  "staff_room": {"id": state["rooms"]["staff"]["id"],
                                 "channel_id": state["rooms"]["staff"]["external_channel_id"]},
                  "cases": {}, "agents": {"service": agent_config("service"), "matcher": agent_config("matcher")},
                  "staff": {"actor_id": humans_by_id[state["humans"]["staff"]["id"]]["actor_id"], "human": human_config("staff")}}
        for case_id, key in (("C001", "priya"), ("C002", "alex")):
            config["cases"][case_id] = {
                "room_id": state["rooms"][case_id]["id"],
                "channel_id": state["rooms"][case_id]["external_channel_id"],
                "claimant_actor_id": humans_by_id[state["humans"][key]["id"]]["actor_id"],
                "intake_agent": agent_config("intake_" + key), "human": human_config(key)}
        return configure_local_addressing(config, state_dir)
    finally:
        admin.close()
        mm.close()
        for api in owners.values():
            api.close()
