"""Provision an opt-in, explicitly fictional multi-person local showcase.

Creates a fresh enrollment under its own state directory. It never starts a
runtime, resets earlier cases, posts as an AI, or changes an existing user's
roles/preferences. Only the pinned cohort notice is sent during setup.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit

from .bootstrap import BootstrapError, _API, _NATIVE, _env, _metadata, _save, bootstrap_local


PEOPLE = {
    "priya": ("Priya", "Shah", "claimant", "C001"),
    "alex": ("Alex", "Chen", "claimant", "C002"),
    "mira": ("Mira", "Library Desk", "custodian", None),
    "jordan": ("Jordan", "Ellis", "finder", None),
    "elena": ("Elena", "Ruiz", "volunteer", None),
    "omar": ("Omar", "Malik", "commuter", None),
    "maya": ("Maya", "Patel", "claimant", "C003"),
    "noah": ("Noah", "Brooks", "cyclist", None),
    "aisha": ("Aisha", "Khan", "student", None),
    "leo": ("Leo", "Martins", "claimant", "C004"),
    "ava": ("Ava", "Wilson", "finder", None),
    "sam": ("Sam", "Library Team", "library assistant", None),
}

NOTICE = (
    "### FindBack • a fictional campus, a real workflow\n"
    "This showcase uses 12 scripted fictional people and fictional objects. "
    "FindBack, the four Intake assistants and the staff Matcher are actual Switch agent identities; "
    "their messages are posted by the application through Switch.\n\n"
    "Use this community room for broad lost/found notices. Identifying marks and contents stay in private cases "
    "and the staff review room. For this demonstration only, the designated demo observer and the local viewer "
    "can read all six rooms. They have no claimant or custodian command authority. "
    "A simulated handover does not establish a physical return."
)

BACKGROUND_ITEMS = [
    {"id": "B-101", "category": "book", "color": "red", "found_place": "Library entrance", "description": "Red paperback; collected by the library desk."},
    {"id": "B-102", "category": "scarf", "color": "grey", "found_place": "Campus bus stop", "description": "Grey scarf; handed to the library team."},
    {"id": "B-103", "category": "keyring", "color": "silver", "found_place": "Bike racks", "description": "Small keyring; identifying details held by staff."},
    {"id": "B-104", "category": "bottle", "color": "yellow", "found_place": "Study lounge", "description": "Yellow water bottle; waiting at the desk."},
]


def canonical_browser_origin(api_origin: str, site_url: str | None) -> str:
    """Adopt a public SiteURL only for the same local server/port."""
    try:
        api, candidate = urlsplit(api_origin), urlsplit(site_url or "")
        host = candidate.hostname or ""
        loopback = host.lower() == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        api_port = api.port or (443 if api.scheme == "https" else 80)
        candidate_port = candidate.port or (443 if candidate.scheme == "https" else 80)
        if (candidate.scheme in {"http", "https"} and loopback and candidate_port == api_port
                and candidate.username is None and candidate.password is None
                and not candidate.query and not candidate.fragment):
            return candidate.scheme + "://" + candidate.netloc
    except ValueError:
        pass
    return api_origin.rstrip("/")


def normalize_room_urls(config: dict, site_url: str | None = None) -> dict:
    """Keep browser links on the canonical SiteURL, separate from API origin."""
    showcase = config.setdefault("showcase", {})
    origin = canonical_browser_origin(config["mattermost_url"], site_url or showcase.get("browser_origin"))
    showcase["browser_origin"] = origin
    rooms = [config["staff_room"], *config["cases"].values()]
    if showcase.get("community_room"):
        rooms.append(showcase["community_room"])
    for room in rooms:
        value = room.get("url")
        if isinstance(value, str) and value:
            path = urlsplit(value).path
            if path.startswith("/"):
                room["url"] = origin + path
    return config


def _member(mm: _API, team_id: str, user_id: str) -> None:
    current = mm.call("GET", f"/api/v4/teams/{team_id}/members/{user_id}", allow=(404,))
    if current.status_code == 404:
        mm.json("POST", f"/api/v4/teams/{team_id}/members", {"team_id": team_id, "user_id": user_id})


def _extend_cases(config: dict, core: dict, state: dict, state_dir: Path) -> None:
    """Provision C003/C004 with real scoped owners/agents, without a domain DB."""
    env = _env(_NATIVE / "native/run/switch-core.env")
    admin = _API(config["switch_url"])
    try:
        admin.gateway_login(env["GATEWAY_ADMIN_EMAIL"], env["GATEWAY_ADMIN_PASSWORD"])
        for case_id, key in (("C003", "maya"), ("C004", "leo")):
            owner_key = "intake_" + key
            if owner_key not in core["owners"]:
                core["owners"][owner_key] = {"name": "FindBack Intake " + key.title() + " owner",
                    "email": f"findback-{core['run_id']}-{owner_key}@demo.invalid", "password": secrets.token_urlsafe(24)}
                _save(state_dir / "provisioning.json", core)
            owner = core["owners"][owner_key]
            if "id" not in owner:
                response = admin.call("POST", "/gateway/users", {**owner, "role": "user"}, allow=(409,))
                if response.status_code != 409:
                    owner["id"] = response.json()["id"]
                    _save(state_dir / "provisioning.json", core)
            api = _API(config["switch_url"])
            try:
                login = api.gateway_login(owner["email"], owner["password"])
                owner["id"] = login["id"]
                _save(state_dir / "provisioning.json", core)
                if owner_key not in core["agents"]:
                    core["agents"][owner_key] = api.json("POST", "/gateway/agents/register-other", {
                        "name": f"findback-{core['run_id']}-intake-{key}", "display_name": "FindBack " + key.title() + " Intake",
                        "description": "Constrained private FindBack " + key.title() + " Intake identity.", "overwrite": False})
                    _save(state_dir / "provisioning.json", core)
                if case_id not in core["rooms"]:
                    core["rooms"][case_id] = api.json("POST", "/gateway/rooms", {
                        "name": f"FindBack {key.title()} {core['run_id']}", "description": "Fictional private FindBack recovery case.",
                        "channel_type": "channel_private", "read_visibility": "private", "write_visibility": "private",
                        "bridge_id": config["bridge_id"], "agent_ids": [core["agents"][owner_key]["id"], config["agents"]["service"]["id"]],
                        "user_names": [state["people"][key]["username"]]})
                    _save(state_dir / "provisioning.json", core)
            finally:
                api.close()
            agent = dict(core["agents"][owner_key])
            metadata = _metadata("identities", bridge_id=config["bridge_id"], human_ids=[], agent_ids=[agent["id"]])
            if len(metadata["agents"]) != 1:
                raise BootstrapError("Additional Intake identity could not be verified.")
            binding = metadata["agents"][0]
            if binding["owner_id"] != owner["id"]:
                raise BootstrapError("Additional Intake owner did not match enrollment.")
            agent.update({"actor_id": binding["actor_id"], "client_id": binding["client_id"], "owner_id": binding["owner_id"]})
            room = core["rooms"][case_id]
            config["cases"][case_id] = {"room_id": room["id"], "channel_id": room["external_channel_id"],
                "url": room.get("external_channel_url"), "human": dict(state["people"][key]), "intake_agent": agent}
    finally:
        admin.close()


def setup_showcase(state_dir: Path, *, viewer_username: str = "krishna") -> dict:
    """Create/reuse a separate showcase; only fictional notice/membership changes.

    Returns the standard FindBack config plus ``showcase``. Twelve simulated
    humans and one observer have private credentials. ``viewer`` records only
    the explicitly enrolled existing viewer's username/immutable Mattermost ID.
    """
    state_dir = Path(state_dir)
    config = bootstrap_local(state_dir)
    core = json.loads((state_dir / "provisioning.json").read_text())
    checkpoint = state_dir / "showcase-provisioning.json"
    state = json.loads(checkpoint.read_text()) if checkpoint.exists() else {"people": {}, "observer": {}}
    run_id = core["run_id"]
    bridge = _metadata("bridge", bridge_id=config["bridge_id"])
    connection = bridge["connection_config"]
    mm, service_owner = _API(config["mattermost_url"]), _API(config["switch_url"])
    try:
        mm.mm_login(connection["admin_user"], connection["admin_password"])
        public_config = mm.json("GET", "/api/v4/config/client?format=old")
        browser_origin = canonical_browser_origin(config["mattermost_url"], public_config.get("SiteURL"))
        team = mm.json("GET", "/api/v4/teams/name/" + connection["team_name"])
        viewer = mm.json("GET", "/api/v4/users/username/" + viewer_username)
        state["viewer"] = {"username": viewer["username"], "id": viewer["id"],
                           "purpose": "Explicit observer of fictional showcase rooms; no domain authority."}
        _save(checkpoint, state)
        for key, (first, last, role, case_id) in PEOPLE.items():
            human = state["people"].get(key)
            if human is None:
                human = dict(config["cases"][case_id]["human"]) if case_id in config["cases"] else (
                    dict(config["staff"]["human"]) if key == "mira" else {
                        "username": f"findback-{key}-{run_id}", "password": secrets.token_urlsafe(24),
                        "email": f"findback-{run_id}-showcase-{key}@demo.invalid"})
                state["people"][key] = human
                _save(checkpoint, state)
            if "id" not in human:
                user = mm.json("POST", "/api/v4/users", {"username": human["username"],
                    "password": human["password"], "email": human["email"], "first_name": first, "last_name": last})
                human["id"] = user["id"]
                _save(checkpoint, state)
            profile = mm.json("PUT", f"/api/v4/users/{human['id']}/patch", {
                "first_name": first, "last_name": last, "nickname": first + " " + last,
                "position": "Fictional FindBack showcase • " + role})
            if "system_admin" in profile.get("roles", "").split():
                raise BootstrapError("A showcase participant unexpectedly has administrator rights.")
            _member(mm, team["id"], human["id"])
            account = _API(config["mattermost_url"])
            try:
                actual, token = account.mm_login(human["username"], human["password"])
                if actual["id"] != human["id"]:
                    raise BootstrapError("A showcase human identity changed; setup stopped.")
                human.update({"token": token, "display_name": first + " " + last,
                              "role": role, "case_id": case_id, "fictional": True})
            finally:
                account.close()
            _save(checkpoint, state)
        _extend_cases(config, core, state, state_dir)
        observer = state["observer"]
        if not observer:
            observer.update({"username": f"findback-observer-{run_id}", "password": secrets.token_urlsafe(24),
                             "email": f"findback-{run_id}-observer@demo.invalid"})
            _save(checkpoint, state)
        if "id" not in observer:
            user = mm.json("POST", "/api/v4/users", {"username": observer["username"], "password": observer["password"],
                "email": observer["email"], "first_name": "FindBack", "last_name": "Demo Observer", "nickname": "FindBack Demo Observer"})
            observer["id"] = user["id"]
            _save(checkpoint, state)
        _member(mm, team["id"], observer["id"])
        account = _API(config["mattermost_url"])
        try:
            actual, token = account.mm_login(observer["username"], observer["password"])
            if actual["id"] != observer["id"] or "system_admin" in actual.get("roles", "").split():
                raise BootstrapError("The demo observer identity is not the enrolled nonadmin.")
            observer.update({"token": token, "display_name": "FindBack Demo Observer", "fictional": True,
                             "purpose": "Read all fictional showcase rooms; no domain authority."})
            # Apply only to this new fictional observer, never the existing viewer.
            account.call("PUT", f"/api/v4/users/{observer['id']}/preferences", [
                {"user_id": observer["id"], "category": "display_settings", "name": "name_format", "value": "full_name"}])
        finally:
            account.close()
        _save(checkpoint, state)
        owner = core["owners"]["service"]
        service_owner.gateway_login(owner["email"], owner["password"])
        if "community_room" not in state:
            room = service_owner.json("POST", "/gateway/rooms", {
                "name": f"findback-campus-{run_id}", "description": "Fictional campus community lost-and-found showcase.",
                "channel_type": "channel_public", "read_visibility": "public", "write_visibility": "public",
                "bridge_id": config["bridge_id"], "agent_ids": [config["agents"]["service"]["id"]],
                "user_names": [human["username"] for human in state["people"].values()] +
                              [observer["username"], state["viewer"]["username"]]})
            state["community_room"] = room
            _save(checkpoint, state)
        room = state["community_room"]
        if room["bridge_id"] != config["bridge_id"]:
            raise BootstrapError("The community room bridge changed; setup stopped.")
        names = [(room["external_channel_id"], "Campus • Lost & Found"),
                 *[(config["cases"][case_id]["channel_id"], first + " • FindBack")
                   for first, _, _, case_id in PEOPLE.values() if case_id],
                 (config["staff_room"]["channel_id"], "Library • Review Desk")]
        for channel_id, display_name in names:
            mm.json("PUT", f"/api/v4/channels/{channel_id}/patch", {"display_name": display_name,
                "header": "Fictional FindBack showcase. Designated demo observers can view all rooms; identifying details stay private."})
            for user_id in [observer["id"], state["viewer"]["id"]]:
                current = mm.call("GET", f"/api/v4/channels/{channel_id}/members/{user_id}", allow=(404,))
                if current.status_code == 404:
                    mm.json("POST", f"/api/v4/channels/{channel_id}/members", {"user_id": user_id})
        if "notice_post_id" not in state:
            post = mm.json("POST", "/api/v4/posts", {"channel_id": room["external_channel_id"], "message": NOTICE})
            state["notice_post_id"] = post["id"]
            _save(checkpoint, state)
        else:
            mm.json("PUT", f"/api/v4/posts/{state['notice_post_id']}/patch", {"message": NOTICE})
        mm.call("POST", f"/api/v4/posts/{state['notice_post_id']}/pin")
        human_ids = [human["id"] for human in state["people"].values()] + [observer["id"], state["viewer"]["id"]]
        for _ in range(40):
            identities = _metadata("identities", bridge_id=config["bridge_id"], human_ids=human_ids, agent_ids=[])
            if len(identities["humans"]) == len(set(human_ids)):
                break
            time.sleep(.25)
        else:
            raise BootstrapError("Switch has not imported every showcase identity; rerun setup shortly.")
        by_id = {value["external_user_id"]: value for value in identities["humans"]}
        for human in [*state["people"].values(), observer, state["viewer"]]:
            identity = by_id[human["id"]]
            human.update({"actor_id": identity["actor_id"], "client_id": identity["client_id"],
                          "external_identity_id": identity["external_identity_id"]})
        # Keep the standard runtime aliases synchronized with the same people.
        for key, (_, _, _, case_id) in PEOPLE.items():
            if case_id:
                config["cases"][case_id]["human"] = dict(state["people"][key])
                config["cases"][case_id]["claimant_actor_id"] = state["people"][key]["actor_id"]
        config["staff"]["human"] = dict(state["people"]["mira"])
        agent = service_owner.json("GET", "/gateway/agents/" + config["agents"]["service"]["id"])
        rules = [{"rooms": [case["room_id"]], "room_groups": "*", "users": [case["human"]["external_identity_id"]],
                  "agents": [], "owner": False, "owner_agents": False, "platform": False} for case in config["cases"].values()]
        rules.append({"rooms": [config["staff_room"]["id"]], "room_groups": "*", "users": [config["staff"]["human"]["external_identity_id"]],
                      "agents": [], "owner": False, "owner_agents": False, "platform": False})
        rules.append({"rooms": [room["id"]], "room_groups": "*",
                      "users": [human["external_identity_id"] for human in state["people"].values()],
                      "agents": [], "owner": False, "owner_agents": False, "platform": False})
        service_owner.json("PUT", "/gateway/agents/" + config["agents"]["service"]["id"] + "/addressing-policy",
                           {"policy": {"rules": rules}})
        roles = [("service", "service", "FindBack"), ("matcher", "matcher", "FindBack • Evidence")]
        roles.extend((case_id, "intake_" + key, "FindBack • " + first + " Intake")
                     for key, (first, _, _, case_id) in PEOPLE.items() if case_id)
        for key, owner_key, label in roles:
            role_owner = _API(config["switch_url"])
            try:
                value = core["owners"][owner_key]
                role_owner.gateway_login(value["email"], value["password"])
                role_owner.json("PUT", "/gateway/agents/" + core["agents"][owner_key]["id"] + "/display-name", {"display_name": label})
                if key in config["cases"]:
                    case = config["cases"][key]
                    rule = {"rooms": [case["room_id"]], "room_groups": "*", "users": [case["human"]["external_identity_id"]],
                            "agents": [config["agents"]["service"]["id"]], "owner": False, "owner_agents": False, "platform": False}
                    role_owner.json("PUT", "/gateway/agents/" + core["agents"][owner_key]["id"] + "/addressing-policy", {"policy": {"rules": [rule]}})
            finally:
                role_owner.close()
        channel = mm.json("GET", "/api/v4/channels/" + room["external_channel_id"])
        community_url = browser_origin + "/" + team["name"] + "/channels/" + channel["name"]
        config["showcase"] = {"fictional": True, "browser_origin": browser_origin,
            "community_room": {"id": room["id"], "channel_id": channel["id"],
            "url": community_url, "display_name": "Campus • Lost & Found"}, "people": state["people"],
            "observer": observer, "viewer": state["viewer"], "notice_post_id": state["notice_post_id"],
            "background_items": BACKGROUND_ITEMS, "simulated_people_count": len(state["people"]),
            "setup_verified_at": datetime.now(timezone.utc).isoformat(),
            "observer_boundary": "The two designated observers can read all fictional rooms but have no claimant/staff command authority."}
        normalize_room_urls(config)
        _save(checkpoint, state)
        _save(state_dir / "config.json", config)
        return config
    finally:
        mm.close()
        service_owner.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".findback/showcase"))
    parser.add_argument("--viewer-username", default="krishna")
    args = parser.parse_args()
    config = setup_showcase(args.state_dir, viewer_username=args.viewer_username)
    print(json.dumps({"simulated_people": len(config["showcase"]["people"]),
        "observer_is_separate_nonadmin": True, "viewer": config["showcase"]["viewer"]["username"],
        "community_url": config["showcase"]["community_room"]["url"], "runtime_started": False}, indent=2))


if __name__ == "__main__":
    main()
