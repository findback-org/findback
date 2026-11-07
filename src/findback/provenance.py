"""Bounded independent Mattermost source proof for an enrolled human event.

GetPostsForChannel documents page/per_page and a post-ID `before` cursor:
https://docs.mattermost.com/api/reference/get-posts-for-channel
Cursor pagination avoids offset drift as new posts arrive. `order`, not the
posts dictionary (which can include thread material), defines page boundaries.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from urllib.parse import quote

import httpx

from .domain import ActorContext


PAGE_SIZE = 200
MAX_PAGES = 20


class ProvenanceError(RuntimeError):
    """A fixed safe diagnostic; never include provider prose or source content."""
    def __init__(self):
        super().__init__("Mattermost source proof requires operator attention.")


class ProvenanceUnavailable(ProvenanceError):
    pass


class ProvenanceIncomplete(ProvenanceError):
    pass


class ProvenanceNonProgressing(ProvenanceError):
    pass


class ProvenanceNotFound(ProvenanceError):
    pass


class ProvenanceSearchExhausted(ProvenanceError):
    pass


async def verify_human_post(http: httpx.AsyncClient, *, base_url: str, bridge_id: str,
                            channel_id: str, human: dict, actor_id: str,
                            room_id: str, payload: dict) -> ActorContext | None:
    """Reject known invalid posts; raise when independent proof is unavailable.

    The caller's inbox worker persists these exceptions as FAILED, allowing an
    operator to restore history/access and explicitly retry the original event.
    Returning None is reserved for demonstrated invalid/tampered human input.
    """
    if payload.get("on_behalf_of") or payload.get("sender_kind") in {"agent", "platform"}:
        return None
    if not isinstance(payload.get("message_id"), str) or not isinstance(payload.get("body"), str):
        return None
    before = None
    before_time = None
    seen: dict[str, int] = {}
    for _ in range(MAX_PAGES):
        params = {"page": 0, "per_page": PAGE_SIZE}
        if before is not None:
            params["before"] = before
        try:
            response = await http.get(base_url + f"/api/v4/channels/{quote(channel_id, safe='')}/posts",
                params=params, headers={"Authorization": "Bearer " + human["token"]})
        except httpx.HTTPError:
            raise ProvenanceUnavailable() from None
        if response.status_code != 200:
            raise ProvenanceUnavailable()
        try:
            page = response.json()
        except (ValueError, TypeError):
            raise ProvenanceIncomplete() from None
        if not isinstance(page, dict) or not isinstance(page.get("posts"), dict) or not isinstance(page.get("order"), list):
            raise ProvenanceIncomplete()
        order, posts = page["order"], page["posts"]
        if (len(order) > PAGE_SIZE or any(not isinstance(pid, str) or not pid for pid in order)
                or len(set(order)) != len(order)):
            raise ProvenanceIncomplete()
        times = []
        for pid in order:
            post = posts.get(pid)
            if not isinstance(post, dict) or post.get("id") != pid:
                raise ProvenanceIncomplete()
            created = post.get("create_at")
            if type(created) is not int or created < 0:
                raise ProvenanceIncomplete()
            times.append(created)
        if any(newer < older for newer, older in zip(times, times[1:])):
            raise ProvenanceIncomplete()
        if ((before_time is not None and times and times[0] >= before_time)
                or (order and not any(pid not in seen for pid in order))
                or any(pid in seen and seen[pid] != posts[pid]["create_at"] for pid in order)):
            raise ProvenanceNonProgressing()
        seen.update(zip(order, times))
        for pid in order:
            post = posts[pid]
            material = "\x1f".join([human["client_id"], bridge_id, channel_id, pid, "0"])
            expected = "sw_" + hashlib.sha256(material.encode()).hexdigest()[:32]
            if expected != payload["message_id"]:
                continue
            if not all(isinstance(post.get(key), str) for key in ("user_id", "channel_id", "message")):
                raise ProvenanceIncomplete()
            if post["user_id"] != human["id"] or post["channel_id"] != channel_id or post["message"] != payload["body"]:
                return None
            props = post.get("props", {})
            if not isinstance(props, dict):
                raise ProvenanceIncomplete()
            if post.get("type") or post.get("delete_at") or props.get("from_webhook") or props.get("from_bot"):
                return None
            created = post.get("create_at")
            if type(created) is not int or created < 0:
                raise ProvenanceIncomplete()
            try:
                occurred_at = datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
            except (OverflowError, OSError, ValueError):
                raise ProvenanceIncomplete() from None
            # Core 0.31 translates MM root IDs into Switch thread IDs. Preserve
            # that trusted transport context; comparing raw IDs would be wrong.
            return ActorContext(actor_id=actor_id, actor_kind="human", room_id=room_id,
                source_event_id=expected, source_text=post["message"], occurred_at=occurred_at,
                origin="switch", thread_id=payload.get("thread_id"))
        if len(order) < PAGE_SIZE:
            raise ProvenanceNotFound()
        # Mattermost v11.9.0's before cursor filters CreateAt STRICTLY LESS
        # THAN the anchor timestamp, not (timestamp, ID). Anchoring on the last
        # post can skip unseen same-millisecond posts. Re-read the oldest-time
        # boundary using its nearest strictly newer anchor instead. Repeated
        # boundary IDs are expected, but each page must add new IDs. A full
        # single-timestamp page cannot be traversed safely with this cursor.
        # https://github.com/mattermost/mattermost/blob/v11.9.0/server/channels/store/sqlstore/post_store.go
        anchor = next((pid for pid in reversed(order) if posts[pid]["create_at"] > times[-1]), None)
        if anchor is None:
            raise ProvenanceNonProgressing()
        before, before_time = anchor, posts[anchor]["create_at"]
    raise ProvenanceSearchExhausted()
