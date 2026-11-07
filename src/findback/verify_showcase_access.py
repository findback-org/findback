"""Read-only identity and room-access checks for the fictional campus.

Uses the existing scoped credentials. It never opens/claims an agent socket,
posts messages, invokes models, or changes cases, roles, or configuration.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

from .verify_access import verify_access


def verify_showcase_access(state_dir: Path) -> dict:
    config = json.loads((state_dir / "config.json").read_text())
    result = verify_access(state_dir, gateway_owners=True)
    checks = result["checks"]
    showcase = config["showcase"]
    community = showcase["community_room"]

    def record(name, status, expected):
        checks.append({"name": name, "status": status, "expected": expected,
                       "passed": status == expected})

    with httpx.Client(timeout=20, follow_redirects=False) as client:
        def request(method, url, token, **kwargs):
            try:
                return client.request(method, url, headers={"Authorization": "Bearer " + token}, **kwargs)
            except httpx.HTTPError:
                return None

        for label, agent, expected in [("service_community", config["agents"]["service"], 200)] + [
            (cid + "_intake_not_community", case["intake_agent"], 403) for cid, case in config["cases"].items()]:
            response = request("POST", config["switch_url"] + "/agents/" + agent["id"] + "/ops/read_context",
                               agent["api_key"], json={"room_id": community["id"], "limit": 1})
            record(label, response.status_code if response is not None else "unreachable", expected)

        actual_human_ids = []
        for name, person in showcase["people"].items():
            identity = request("GET", config["mattermost_url"] + "/api/v4/users/me", person["token"])
            membership = request("GET", config["mattermost_url"] + "/api/v4/channels/" + community["channel_id"] + "/members/" + person["id"], person["token"])
            try:
                account = identity.json() if identity is not None and identity.status_code == 200 else {}
                member = membership.json() if membership is not None and membership.status_code == 200 else {}
                valid = (account.get("id") == person["id"] and member.get("user_id") == person["id"] and
                         "system_admin" not in account.get("roles", "").split() and not account.get("is_bot"))
                if valid:
                    actual_human_ids.append(account["id"])
            except ValueError:
                valid = False
            record(name + "_community_member", 200 if valid else "identity_or_membership_failed", 200)

        observer = showcase["observer"]
        identity = request("GET", config["mattermost_url"] + "/api/v4/users/me", observer["token"])
        try:
            account = identity.json() if identity is not None and identity.status_code == 200 else {}
            nonadmin = account.get("id") == observer["id"] and "system_admin" not in account.get("roles", "").split() and not account.get("is_bot")
        except ValueError:
            nonadmin = False
        checks.append({"name": "observer_nonadmin", "passed": nonadmin})
        rooms = [("community", community["channel_id"]), ("staff", config["staff_room"]["channel_id"])] + [
            (cid, case["channel_id"]) for cid, case in config["cases"].items()]
        for label, channel in rooms:
            response = request("GET", config["mattermost_url"] + "/api/v4/channels/" + channel + "/posts",
                               observer["token"], params={"per_page": 1})
            record("observer_" + label, response.status_code if response is not None else "unreachable", 200)
        checks.append({"name": "twelve_distinct_immutable_humans",
                       "passed": len(actual_human_ids) == len(set(actual_human_ids)) == 12})

    result["passed"] = all(check["passed"] for check in checks)
    result["check_count"] = len(checks)
    result["showcase_observer_disclosure"] = "Separate nonadmin observer and explicitly enrolled existing viewer can read fictional private rooms; ordinary claimants remain scoped."
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = verify_showcase_access(args.state_dir.resolve())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"passed": result["passed"], "checks": result["check_count"], "output": str(args.output)}))
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
