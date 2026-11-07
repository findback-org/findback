"""Explicit fictional campus fixtures. Loading never clears existing workflow state."""
from __future__ import annotations
from .storage import SQLiteStore, dumps, utcnow

FIXTURE_ITEMS = [
    {"id": "I-001", "coarse": {"category": "headphone_case", "color": "black", "brand_model": None,
      "found_place": "Library Desk A", "found_at": "2026-11-07T14:00:00+05:30"},
     "private_facts": ["Green triangle sticker under lid", "Short blue charging cable inside"]},
    {"id": "I-002", "coarse": {"category": "headphone_case", "color": "black", "brand_model": None,
      "found_place": "Library Study Room 2", "found_at": "2026-11-07T14:15:00+05:30"},
     "private_facts": ["Red thread on zipper", "Initials RK on inner label"]},
    {"id": "I-003", "coarse": {"category": "bottle", "color": "blue", "brand_model": None,
      "found_place": "Library Desk B", "found_at": "2026-11-06T14:00:00+05:30"},
     "private_facts": ["Small star engraving beneath base"]},
]


def load_fixtures(store: SQLiteStore, *, staff_room_id="staff", staff_actor_id="mira",
                  claimant_ids=None, case_room_ids=None, intake_agent_ids=None,
                  matcher_agent_ids=None) -> dict:
    """Configure fictional actors/rooms and INSERT OR IGNORE the three exact items.

    Mappings use C001/C002 keys, or priya/alex for convenience. Live setup must
    supply the actual enrolled IDs/room IDs and still describe these items as fictional.
    """
    claimant_ids = claimant_ids if claimant_ids is not None else {"C001": "priya", "C002": "alex"}
    case_room_ids = case_room_ids if case_room_ids is not None else {"C001": "case-priya", "C002": "case-alex"}
    intake_agent_ids = intake_agent_ids if intake_agent_ids is not None else {"C001": ["intake-priya"], "C002": ["intake-alex"]}
    matcher_agent_ids = matcher_agent_ids if matcher_agent_ids is not None else ["matcher"]
    cases = []
    for cid, name in (("C001", "priya"), ("C002", "alex")):
        agents = intake_agent_ids.get(cid, intake_agent_ids.get(name, []))
        if isinstance(agents, str):
            agents = [agents]
        cases.append({"claim_id": cid, "claimant_actor_id": claimant_ids.get(cid, claimant_ids.get(name, name)),
                      "case_room_id": case_room_ids.get(cid, case_room_ids.get(name, "case-" + name)),
                      "intake_agent_ids": agents})
    store.configure_enrollment(staff_room_id=staff_room_id, staff_actor_ids=[staff_actor_id],
                               matcher_agent_ids=matcher_agent_ids, cases=cases)
    with store.transaction() as db:
        for item in FIXTURE_ITEMS:
            now = utcnow()
            db.execute("INSERT OR IGNORE INTO items(id,coarse_json,private_facts_json,status,created_at,updated_at) VALUES (?,?,?,'AVAILABLE',?,?)",
                       (item["id"], dumps(item["coarse"]), dumps(item["private_facts"]), now, now))
        db.execute("INSERT INTO settings VALUES ('fixture_inventory',?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", (dumps(True),))
    return {"cases": cases, "item_ids": [i["id"] for i in FIXTURE_ITEMS],
            "staff_room_id": staff_room_id, "staff_actor_id": staff_actor_id,
            "fictional": True}


seed_fixtures = load_fixtures
