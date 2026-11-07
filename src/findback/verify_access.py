"""Opt-in read-only credential checks; no sockets, posts, models or case actions.

Run ``python -m findback.verify_access --state-dir .findback`` while the normal
runtime is active or stopped. The checker never claims/replaces its connections.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

import httpx


def verify_access(state_dir: Path, *, gateway_owners: bool = False) -> dict:
    state_dir = Path(state_dir)
    config = json.loads((state_dir / "config.json").read_text())
    results: list[dict] = []

    def record(name: str, actual, expected):
        results.append({"name": name, "status": actual, "expected": expected,
                        "passed": actual == expected})

    with httpx.Client(timeout=20, follow_redirects=False) as http:
        def room_read(name: str, agent: dict, room: str, expected: int, *, target_id=None):
            try:
                response = http.post(
                    config["switch_url"] + "/agents/" + (target_id or agent["id"]) + "/ops/read_context",
                    headers={"Authorization": "Bearer " + agent["api_key"]},
                    json={"room_id": room, "limit": 1})
                record(name, response.status_code, expected)
            except httpx.HTTPError:
                record(name, "unreachable", expected)

        service, matcher = config["agents"]["service"], config["agents"]["matcher"]
        staff_room = config["staff_room"]["id"]
        room_read("service_reads_staff", service, staff_room, 200)
        room_read("matcher_reads_staff", matcher, staff_room, 200)
        for case_id, case in config["cases"].items():
            room_read(f"service_reads_{case_id}", service, case["room_id"], 200)
            room_read(f"own_intake_reads_{case_id}", case["intake_agent"], case["room_id"], 200)
            room_read(f"intake_{case_id}_denied_staff", case["intake_agent"], staff_room, 403)
            room_read(f"matcher_denied_{case_id}", matcher, case["room_id"], 403)
            for other_id, other in config["cases"].items():
                if other_id != case_id:
                    room_read(f"intake_{case_id}_denied_{other_id}", case["intake_agent"], other["room_id"], 403)
        first = next(iter(config["cases"].values()))
        room_read("changed_url_path_does_not_grant_matcher_scope", first["intake_agent"], staff_room,
                  403, target_id=matcher["id"])

        humans = [("staff", config["staff"]["human"], config["staff_room"]["channel_id"])] + [
            (case_id, case["human"], case["channel_id"]) for case_id, case in config["cases"].items()]
        for label, human, own_channel in humans:
            headers = {"Authorization": "Bearer " + human["token"]}
            try:
                user = http.get(config["mattermost_url"] + "/api/v4/users/me", headers=headers)
                identity_ok = user.status_code == 200 and user.json().get("id") == human["id"]
                record(f"human_{label}_immutable_account", identity_ok, True)
                not_admin = identity_ok and "system_admin" not in user.json().get("roles", "").split()
                record(f"human_{label}_nonadmin", not_admin, True)
            except (httpx.HTTPError, ValueError):
                record(f"human_{label}_immutable_account", "unreachable", True)
            for destination, _, channel in humans:
                expected = 200 if channel == own_channel else 403
                try:
                    response = http.get(config["mattermost_url"] + f"/api/v4/channels/{channel}/posts",
                                        params={"per_page": 1}, headers=headers)
                    record(f"human_{label}_reads_{destination}", response.status_code, expected)
                except httpx.HTTPError:
                    record(f"human_{label}_reads_{destination}", "unreachable", expected)

    if gateway_owners:
        state = json.loads((state_dir / "provisioning.json").read_text())
        checks = [("intake_priya", staff_room, "intake_owner_denied_staff"),
                  ("matcher", first["room_id"], "matcher_owner_denied_claimant"),
                  ("service", first["room_id"], "service_owner_denied_claimant_gateway")]
        for key, room, label in checks:
            owner = state["owners"][key]
            with httpx.Client(timeout=20, follow_redirects=False) as gateway:
                try:
                    login = gateway.post(config["switch_url"] + "/gateway/auth/login",
                                         json={"email": owner["email"], "password": owner["password"]})
                    if login.status_code != 200:
                        record(label, "owner_login_failed", 403)
                        continue
                    response = gateway.get(config["switch_url"] + "/gateway/rooms/" + room)
                    record(label, response.status_code, 403)
                except httpx.HTTPError:
                    record(label, "unreachable", 403)

    return {"verified_at": datetime.now(timezone.utc).isoformat(),
            "behavior": "Live HTTP read-only credential checks; no WebSocket claims, messages, models or case mutations.",
            "gateway_owner_checks_requested": gateway_owners,
            "passed": all(value["passed"] for value in results), "checks": results,
            "limitations": "This measures configured token/room access, not physical returns, user demand, deployment security or addressing behavior."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=Path(".findback"))
    parser.add_argument("--gateway-owners", action="store_true",
                        help="Also check the nonadmin owners saved in private provisioning.json")
    parser.add_argument("--output", type=Path, help="Write the nonsecret result artifact")
    args = parser.parse_args()
    result = verify_access(args.state_dir, gateway_owners=args.gateway_owners)
    encoded = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
