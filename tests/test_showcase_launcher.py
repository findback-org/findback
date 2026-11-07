from __future__ import annotations

import asyncio

import pytest

from findback import showcase


def test_primary_runtime_failure_survives_secondary_story_cleanup_error(tmp_path, monkeypatch):
    """A worker's shutdown error must not replace the actual transport failure."""
    state = {"story_closed": False, "runtime_exited": False}

    class RuntimeFailure(RuntimeError):
        pass

    class Story:
        def __init__(self, *_args, **_kwargs):
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()
            state["story"] = self

        def run(self):
            raise AssertionError("The controlled fake worker must be used.")

        def stop(self):
            self.stopped.set()

        def close(self):
            state["story_closed"] = True

    class Runtime:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            state["runtime_exited"] = True

        async def run_forever(self):
            await state["story"].started.wait()
            raise RuntimeFailure("Primary live transport failure")

    async def worker(_function):
        story = state["story"]
        story.started.set()
        await story.stopped.wait()
        # This models e.g. malformed JSON or a failed SQLite read while the
        # synchronous driver is winding down after the runtime has failed.
        raise ValueError("Secondary worker shutdown failure")

    monkeypatch.setattr(showcase, "CampusStory", Story)
    monkeypatch.setattr(showcase, "ShowcaseRuntime", Runtime)
    monkeypatch.setattr(showcase.asyncio, "to_thread", worker)

    with pytest.raises(RuntimeFailure, match="Primary live transport failure"):
        asyncio.run(showcase.serve_showcase({}, tmp_path, tmp_path / "evidence"))

    assert state["story_closed"] and state["runtime_exited"]
