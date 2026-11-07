"""Run a fresh, explicitly fictional campus showcase with one local command.

Creates twelve fictional accounts and four private cases in the installed local
Switch/Mattermost tenant. Scripted human messages use their own accounts; all
agent inference, transport, and domain transitions run live. No physical item
is handed over. Existing showcase directories are reconciled, never reset.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from uuid import uuid4

from .bootstrap import BootstrapError
from .showcase_runtime import ShowcaseRuntime
from .showcase_setup import setup_showcase
from .showcase_story import CampusStory, StoryError


async def serve_showcase(config: dict, state_dir: Path, evidence_dir: Path, *,
                         pace: float = .7, max_seconds: float = 600):
    """Keep Switch heartbeats running while the synchronous story uses HTTP."""
    story = CampusStory(state_dir, evidence_dir, pace=pace, max_seconds=max_seconds)
    try:
        async with ShowcaseRuntime(config, state_dir) as runtime:
            live = asyncio.create_task(runtime.run_forever(), name="findback-showcase-runtime")
            driver = asyncio.create_task(asyncio.to_thread(story.run), name="findback-showcase-story")
            try:
                completed, _ = await asyncio.wait({live, driver}, return_when=asyncio.FIRST_COMPLETED)
                if live in completed:
                    live.result()
                    raise StoryError("The live runtime ended before the showcase completed.")
                result = driver.result()
                print("\nShowcase complete. The real agents remain online; press Ctrl+C to stop.", flush=True)
                print("Community: " + config["showcase"]["community_room"]["url"], flush=True)
                print("Evidence: " + str(evidence_dir / "showcase-run.json"), flush=True)
                print("State: " + str(state_dir), flush=True)
                await live
                return result
            finally:
                # asyncio cancellation does not terminate a to_thread worker.
                # Stop it cooperatively before closing its HTTP client/runtime.
                story.stop()
                try:
                    if not driver.done():
                        try:
                            await asyncio.wait_for(asyncio.shield(driver), timeout=35)
                        except (Exception, asyncio.CancelledError):
                            # A worker's secondary failure during cooperative
                            # shutdown must not replace the failure/cancellation
                            # that brought us here. Its task result is retrieved.
                            pass
                finally:
                    live.cancel()
                    await asyncio.gather(live, return_exceptions=True)
    finally:
        story.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path,
                        help="Reuse this explicitly chosen showcase; default creates .findback/campus-<unique-id>")
    parser.add_argument("--viewer-username", default="krishna",
                        help="Existing local Mattermost user permitted to observe the fictional showcase")
    parser.add_argument("--evidence-dir", type=Path,
                        help="Credential-free actual run record; default is <state-dir>/evidence")
    parser.add_argument("--pace", type=float, default=.7,
                        help="Typing/message pacing; actual inference and domain waits are never skipped")
    parser.add_argument("--max-seconds", type=float, default=600)
    args = parser.parse_args()
    if args.pace < 0 or args.max_seconds <= 0:
        parser.error("Pace must be nonnegative and max-seconds must be positive.")
    state = (args.state_dir or Path(".findback") / ("campus-" + uuid4().hex[:10])).resolve()
    evidence = (args.evidence_dir or state / "evidence").resolve()
    print("Preparing a fictional twelve-person campus at " + str(state), flush=True)
    try:
        config = setup_showcase(state, viewer_username=args.viewer_username)
        print("Open the real Mattermost room: " + config["showcase"]["community_room"]["url"], flush=True)
        asyncio.run(serve_showcase(config, state, evidence, pace=args.pace, max_seconds=args.max_seconds))
    except KeyboardInterrupt:
        print("Showcase stopped. Its accounts, messages, cases, and receipts remain available for inspection.")
    except (BootstrapError, StoryError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
