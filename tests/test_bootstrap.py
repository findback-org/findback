"""Addressing allowlists use external identity IDs and explicit room scope."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from findback import bootstrap


class AddressingEnrollmentTests(unittest.TestCase):
    def test_humans_cannot_inherit_another_room_or_agent_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            cases = {}
            for cid, name in (("C001", "priya"), ("C002", "alex")):
                cases[cid] = {"room_id": "room-" + name,
                              "human": {"id": "mm-" + name, "client_id": "client-" + name},
                              "intake_agent": {"id": "agent-intake-" + name}}
            config = {"switch_url": "http://localhost:1234", "bridge_id": "bridge",
                      "staff_room": {"id": "room-staff"}, "cases": cases,
                      "staff": {"human": {"id": "mm-staff", "client_id": "client-staff"}},
                      "agents": {"service": {"id": "agent-service"}, "matcher": {"id": "agent-matcher"}}}
            keys = ["service", "matcher", "intake_priya", "intake_alex"]
            provision = {"owners": {key: {"email": key, "password": "private"} for key in keys},
                         "agents": {key: {"id": "agent-" + key.replace("_", "-")} for key in keys},
                         "rooms": {key: {"external_channel_url": "http://localhost/" + key}
                                   for key in ["C001", "C002", "staff"]}}
            (state / "provisioning.json").write_text(json.dumps(provision))
            calls = {}

            class FakeAPI:
                def __init__(self, _):
                    pass

                def gateway_login(self, email, password):
                    self.owner = email

                def json(self, method, path, body):
                    calls[self.owner] = body["policy"]
                    return {"addressing_policy": body["policy"]}

                def close(self):
                    pass

            bindings = {"humans": [{"external_user_id": "mm-" + name,
                                     "external_identity_id": "identity-" + name}
                                    for name in ["priya", "alex", "staff"]]}
            with patch.object(bootstrap, "_API", FakeAPI), patch.object(bootstrap, "_metadata", return_value=bindings):
                bootstrap.configure_local_addressing(config, state)
            self.assertEqual(set(keys), set(calls))
            for owner, name, room in [("intake_priya", "priya", "room-priya"),
                                      ("intake_alex", "alex", "room-alex"),
                                      ("matcher", "staff", "room-staff")]:
                rule = calls[owner]["rules"][0]
                # All three ID namespaces differ deliberately.
                self.assertEqual(["identity-" + name], rule["users"])
                self.assertEqual([room], rule["rooms"])
                self.assertEqual(["agent-service"], rule["agents"])
                self.assertFalse(rule["owner"])
                self.assertFalse(rule["owner_agents"])
                self.assertFalse(rule["platform"])
            service = calls["service"]["rules"]
            self.assertEqual({("room-priya", "identity-priya"), ("room-alex", "identity-alex"),
                              ("room-staff", "identity-staff")},
                             {(rule["rooms"][0], rule["users"][0]) for rule in service})
            self.assertTrue(all(rule["agents"] == [] for rule in service))
            self.assertEqual(0o600, (state / "config.json").stat().st_mode & 0o777)
            self.assertEqual(0o700, state.stat().st_mode & 0o777)
            self.assertNotIn('"password"', (state / "config.json").read_text())


if __name__ == "__main__":
    unittest.main()
