"""Real handler/inbox tests for independent source proof beyond the latest page."""
from __future__ import annotations

import asyncio
import copy
import json
import sys

import httpx
import pytest

from findback import cli, provenance
from test_runtime import human_frame, runtime
from test_showcase import public_frame, showcase


@pytest.fixture(params=["command", "community"])
def source(request, runtime, showcase, scenario):
    if request.param == "command":
        reviewed = scenario.ready()
        frame = human_frame(runtime, "staff", f"findback approve C001 I-001 {reviewed['review_id']} Inspected the independent identifying details.")
        instance = runtime
    else:
        frame = public_frame(showcase, "priya", "I lost my black headphone case near the library.")
        instance = showcase
    post = copy.deepcopy(next(p for p in instance._test_posts.values()
                              if p["message"] == frame["data"]["payload"]["body"]))
    return request.param, instance, frame, post


def filler(pid):
    return {"id": pid, "user_id": "unrelated-user", "channel_id": "irrelevant-channel",
            "message": "Unrelated background activity", "create_at": 1794038400001, "type": "", "props": {}}


def page(posts, *, extra=False):
    order = [p["id"] for p in posts]
    # Dictionary insertion deliberately differs from canonical page order.
    result = {"order": order, "posts": {p["id"]: p for p in reversed(posts)}}
    if extra:
        result["posts"]["extra-thread-root"] = filler("extra-thread-root")
    return result


def busy_page(number=0):
    return page([{**filler(f"busy-{number}-{i:03}"), "create_at": 1794039400000 - number * 1000 - i}
                 for i in range(provenance.PAGE_SIZE)], extra=True)


def install(instance, pages):
    calls = []
    def handler(request):
        index = len(calls)
        calls.append(request)
        assert request.url.params["page"] == "0"
        assert request.url.params["per_page"] == str(provenance.PAGE_SIZE)
        assert "since" not in request.url.params
        if index:
            previous = pages[index - 1]
            oldest = previous["posts"][previous["order"][-1]]["create_at"]
            anchor = next(pid for pid in reversed(previous["order"]) if previous["posts"][pid]["create_at"] > oldest)
            assert request.url.params["before"] == anchor
        else:
            assert "before" not in request.url.params
        result = pages[index]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)
    asyncio.run(instance.http.aclose())
    instance.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return calls


def process(instance, frame):
    instance.journal.capture("test-service", frame, primary=True)
    asyncio.run(instance._process_frame(frame))
    with instance.store.connection() as db:
        return dict(db.execute("SELECT state,error FROM inbox WHERE event_id=?",
                              (frame["data"]["payload"]["message_id"],)).fetchone())


def counts(instance):
    with instance.store.connection() as db:
        return tuple(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("approvals", "reservations", "drafts", "source_messages", "runtime_messages"))


def test_genuine_older_source_is_processed_by_actual_handler_with_cursor_pages(source):
    kind, instance, frame, post = source
    calls = install(instance, [busy_page(), page([post], extra=True)])
    result = process(instance, frame)
    assert result == {"state": "DONE", "error": None}
    assert len(calls) == 2
    with instance.store.connection() as db:
        if kind == "command":
            assert db.execute("SELECT status FROM claims WHERE id='C001'").fetchone()[0] == "APPROVED"
            assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        else:
            assert db.execute("SELECT current_draft_id FROM claims WHERE id='C001'").fetchone()[0]
            assert len(instance.model.drafts) == 1
            assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 0


@pytest.mark.parametrize("tamper", ["author", "body", "channel", "webhook", "bot", "system", "deleted", "delegated"])
def test_known_tampered_older_post_never_authorizes_mutation(source, tamper):
    _, instance, frame, post = source
    if tamper == "author":
        post["user_id"] = "different-account"
    elif tamper == "body":
        post["message"] = "The original content differs from this alleged command."
    elif tamper == "channel":
        post["channel_id"] = "different-channel"
    elif tamper in {"webhook", "bot"}:
        post["props"] = {"from_" + tamper: "true", "override_username": "Mira"}
    elif tamper == "system":
        post["type"] = "system_add_to_channel"
    elif tamper == "deleted":
        post["delete_at"] = 1794038400999
    else:
        frame["data"]["payload"]["on_behalf_of"] = "staff-mira"
    calls = install(instance, [busy_page(), page([post])])
    before = counts(instance)
    result = process(instance, frame)
    assert result == {"state": "DONE", "error": None}  # Known invalid input is rejected, not retried.
    assert counts(instance) == before
    assert len(calls) == (0 if tamper == "delegated" else 2)


@pytest.mark.parametrize("failure,error", [
    ("http", "ProvenanceUnavailable"), ("transport", "ProvenanceUnavailable"),
    ("json", "ProvenanceIncomplete"), ("missing_order", "ProvenanceIncomplete"),
    ("missing_post", "ProvenanceIncomplete"), ("duplicate_order", "ProvenanceIncomplete"),
    ("absent", "ProvenanceNotFound"), ("repeated", "ProvenanceNonProgressing"),
    ("overlap", "ProvenanceNonProgressing"), ("exhausted", "ProvenanceSearchExhausted"),
])
def test_missing_or_incomplete_proof_is_retained_failed_without_unauthorized_mutation(source, failure, error, capsys):
    _, instance, frame, post = source
    first = busy_page()
    if failure == "http":
        next_page = httpx.Response(403, text="PRIVATE provider response token")
    elif failure == "transport":
        next_page = httpx.ReadTimeout("PRIVATE credentials and claimant text")
    elif failure == "json":
        next_page = httpx.Response(200, content=b"PRIVATE invalid json")
    elif failure == "missing_order":
        next_page = {"posts": {post["id"]: post}}
    elif failure == "missing_post":
        next_page = {"order": [post["id"]], "posts": {}}
    elif failure == "duplicate_order":
        next_page = {"order": [post["id"], post["id"]], "posts": {post["id"]: post}}
    elif failure == "absent":
        next_page = {"order": [], "posts": {}}
    elif failure == "repeated":
        next_page = copy.deepcopy(first)
    elif failure == "overlap":
        next_page = page([filler(first["order"][-1]), *[filler(f"new-{i}") for i in range(199)]])
    else:
        next_page = None
    pages = [busy_page(i) for i in range(provenance.MAX_PAGES)] if failure == "exhausted" else [first, next_page]
    calls = install(instance, pages)
    before = counts(instance)
    result = process(instance, frame)
    assert result == {"state": "FAILED", "error": error}
    assert counts(instance) == before
    assert len(calls) == (provenance.MAX_PAGES if failure == "exhausted" else 2)
    assert "PRIVATE" not in capsys.readouterr().out


def test_operator_can_restore_proof_and_requeue_the_original_failed_event(source, monkeypatch, capsys):
    kind, instance, frame, post = source
    install(instance, [httpx.Response(503, text="PRIVATE temporary upstream problem")])
    assert process(instance, frame)["state"] == "FAILED"
    event_id = frame["data"]["payload"]["message_id"]
    monkeypatch.setattr(sys, "argv", ["findback", "--state-dir", str(instance.state_dir), "retry-failed"])
    cli.main()
    pending = instance.journal.pending()
    assert pending == [frame]
    install(instance, [busy_page(), page([post])])
    assert process(instance, pending[0]) == {"state": "DONE", "error": None}
    with instance.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM inbox WHERE event_id=?", (event_id,)).fetchone()[0] == 1
        if kind == "command":
            assert db.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] == 1
        else:
            assert db.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    assert "PRIVATE" not in capsys.readouterr().out


def test_same_millisecond_boundary_is_revisited_so_older_genuine_source_is_not_skipped(source):
    _, instance, frame, post = source
    newest = [{**filler(f"recent-{i}"), "create_at": 1794039400000 - i} for i in range(199)]
    # Source is hidden beyond page 1, beside another post sharing its timestamp.
    boundary = {**filler("same-time-other"), "create_at": post["create_at"]}
    history = [*newest, boundary, post]
    calls = []
    def strict_mattermost(request):
        calls.append(request)
        before = request.url.params.get("before")
        candidates = history
        if before:
            anchor_time = next(p["create_at"] for p in history if p["id"] == before)
            candidates = [p for p in history if p["create_at"] < anchor_time]
        assert request.url.params["page"] == "0"
        return httpx.Response(200, json=page(candidates[:200]))
    asyncio.run(instance.http.aclose())
    instance.http = httpx.AsyncClient(transport=httpx.MockTransport(strict_mattermost))
    assert process(instance, frame) == {"state": "DONE", "error": None}
    assert len(calls) == 2
    assert calls[1].url.params["before"] == newest[-1]["id"]


def test_full_single_timestamp_page_fails_visibly_instead_of_skipping_hidden_ties(source):
    _, instance, frame, post = source
    same_time = [{**filler(f"same-time-{i}"), "create_at": post["create_at"]} for i in range(200)]
    calls = install(instance, [page(same_time)])
    before = counts(instance)
    assert process(instance, frame) == {"state": "FAILED", "error": "ProvenanceNonProgressing"}
    assert len(calls) == 1 and counts(instance) == before
