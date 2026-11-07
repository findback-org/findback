"""Coarse candidate lookup only. Similarity never decides ownership."""
from __future__ import annotations
import re
from typing import Any

VERIFICATION_QUESTION = "What marks, contents, or accessories would help staff identify yours?"


def normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def category(value: Any) -> str:
    text = normalize(value)
    if "headphone" in text or "earbud" in text:
        return "headphone case"
    if "bottle" in text:
        return "bottle"
    if text in {"book", "paperback", "paperback book", "hardback", "hardback book",
                "hardcover", "hardcover book", "novel", "textbook"}:
        return "book"
    return text


def coarse_candidates(report: dict, items: list[dict]) -> list[dict]:
    """Unknown/missing facts never become confident matches or exclusions."""
    found = []
    for item in items:
        if item.get("status") != "AVAILABLE":
            continue
        coarse = item.get("coarse", {})
        agreements, missing = [], []
        incompatible = False
        for field, normalizer in (("category", category), ("color", normalize)):
            a, b = normalizer(report.get(field)), normalizer(coarse.get(field))
            if a and b:
                if a != b:
                    incompatible = True
                    break
                agreements.append(field)
            else:
                missing.append(field)
        if incompatible:
            continue
        # Place is descriptive; exact room/time disagreement is a staff question, not a hard exclusion.
        a = normalize(report.get("lost_place"))
        b = normalize(coarse.get("found_place"))
        if a and b and (a in b or b in a or "library" in a and "library" in b):
            agreements.append("general place")
        found.append({"id": item["id"], "item_id": item["id"], "coarse": coarse, "revision": item["revision"],
                      "agreements": agreements, "missing_information": missing,
                      "assessment": "Possible match; staff review and identifying evidence needed."})
    return sorted(found, key=lambda x: (-len(x["agreements"]), x["item_id"]))
