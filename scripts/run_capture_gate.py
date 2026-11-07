"""Configurable filesystem gate: capture first, then explicitly cue each stage.

Each cue is handled once. Existing cues are ignored at worker startup. A full
JSON cue with a unique nonce is preferred; plain stage names remain supported.
The worker never starts an earlier stage automatically and never posts for a
completed stage again. No story runs merely because the process starts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from findback.live_take import LiveTake, STAGES
from findback.showcase_story import StoryError, _save


class IncompleteCue(ValueError):
    pass


def parse_cue(raw: str) -> tuple[str, str | None]:
    raw = raw.strip()
    if not raw:
        raise IncompleteCue('Empty cue.')
    if raw.startswith('{'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise IncompleteCue('JSON cue is not complete.') from exc
        if not isinstance(value, dict) or not isinstance(value.get('stage'), str):
            raise ValueError('Cue must name a stage.')
        stage, nonce = value['stage'], value.get('nonce')
        if nonce is not None and (isinstance(nonce, bool) or not isinstance(nonce, (str, int))):
            raise ValueError('Nonce must be a string or integer.')
        nonce = str(nonce) if nonce is not None else None
    else:
        stage, nonce = raw.split()[0], None
    if stage not in STAGES:
        raise ValueError('Unsupported recording stage.')
    return stage, nonce


class CaptureGate:
    def __init__(self, state_dir: Path, *, delay: float = 1.0, poll: float = .05,
                 pace: float = 1.0, take_factory=LiveTake):
        self.state = state_dir.resolve()
        self.cue = self.state / 'capture-cue.txt'
        self.status = self.state / 'capture-status.json'
        self.delay, self.poll, self.pace = delay, poll, pace
        self.take_factory = take_factory
        self.stopping = False
        self.active_take = None
        self.seen_nonces = set()
        self.last_signature = None
        self.last_incomplete = None
        current = self.read_current()
        if current:
            self.last_signature, raw = current
            try:
                _, nonce = parse_cue(raw)
                if nonce is not None:
                    self.seen_nonces.add(nonce)
            except (IncompleteCue, ValueError):
                pass
        self.write_status('READY', ready_at_ms=round(time.time() * 1000),
                          existing_cue_ignored=current is not None,
                          allowed=list(STAGES), gate_path=str(self.cue),
                          startup_posts_sent=0, trigger_delay_seconds=self.delay)

    def write_status(self, status: str, **fields):
        _save(self.status, {'status': status, **fields})

    def read_current(self):
        try:
            before = self.cue.stat()
            raw = self.cue.read_text()
            after = self.cue.stat()
        except (FileNotFoundError, UnicodeDecodeError):
            return None
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            return None
        signature = (after.st_mtime_ns, after.st_size, hashlib.sha256(raw.encode()).hexdigest())
        return signature, raw

    def stop(self, *_):
        self.stopping = True
        if self.active_take is not None:
            self.active_take.stop()

    def discard_queued_cue(self, running_signature):
        current = self.read_current()
        if not current or current[0] == running_signature:
            return None
        self.last_signature, raw = current
        try:
            stage, nonce = parse_cue(raw)
            if nonce is not None:
                self.seen_nonces.add(nonce)
        except (IncompleteCue, ValueError):
            stage, nonce = None, None
        return {'stage': stage, 'nonce': nonce,
                'reason': 'Cue arrived while another stage was running. Write a fresh cue after COMPLETE and after capture begins.'}

    def tick(self):
        current = self.read_current()
        if not current or current[0] == self.last_signature:
            return False
        signature, raw = current
        try:
            stage, nonce = parse_cue(raw)
        except IncompleteCue:
            # A truncate/write or partial JSON must not consume the cue or
            # kill the worker. Retry when the writer finishes.
            if signature != self.last_incomplete:
                self.write_status('WAITING_FOR_COMPLETE_CUE', at_ms=round(time.time() * 1000))
                self.last_incomplete = signature
            return False
        except ValueError:
            self.last_signature = signature
            self.write_status('REFUSED', reason='Unsupported or invalid cue.', at_ms=round(time.time() * 1000))
            return False
        self.last_signature = signature
        if nonce is not None and nonce in self.seen_nonces:
            return False
        if nonce is not None:
            self.seen_nonces.add(nonce)
        received = round(time.time() * 1000)
        self.write_status('CUE_RECEIVED', stage=stage, nonce=nonce, cue_received_at_ms=received)
        until = time.monotonic() + self.delay
        while not self.stopping and time.monotonic() < until:
            time.sleep(min(self.poll, max(0, until - time.monotonic())))
        if self.stopping:
            return False
        take = None
        try:
            take = self.take_factory(self.state, pace=self.pace)
            self.active_take = take
            completed = take.journal.get('stages', {}).get(stage)
            if completed:
                self.write_status('NOOP', stage=stage, nonce=nonce, original_result=completed,
                                  at_ms=round(time.time() * 1000), posts_sent=0)
                return True
            expected = next((name for name in STAGES if name not in take.journal.get('stages', {})), None)
            if stage != expected:
                self.write_status('REFUSED', stage=stage, expected_stage=expected,
                                  reason='Stages must be explicitly cued in order; nothing was posted.',
                                  at_ms=round(time.time() * 1000), posts_sent=0)
                return False
            started = round(time.time() * 1000)
            self.write_status('RUNNING', stage=stage, nonce=nonce,
                              cue_received_at_ms=received, started_at_ms=started)
            result = getattr(take, stage)()
            queued = self.discard_queued_cue(signature)
            self.write_status('COMPLETE', stage=stage, nonce=nonce,
                              cue_received_at_ms=received, started_at_ms=started,
                              completed_at_ms=round(time.time() * 1000), result=result,
                              queued_cue_refused=queued)
            print('COMPLETE ' + stage, flush=True)
            return True
        except Exception as exc:
            self.write_status('FAILED', stage=stage, nonce=nonce, error_class=type(exc).__name__,
                              safe_reason=str(exc) if isinstance(exc, StoryError) else 'Inspect the local runtime log.',
                              at_ms=round(time.time() * 1000))
            print('FAILED ' + stage + ' ' + type(exc).__name__, flush=True)
            return False
        finally:
            if take is not None:
                take.close()
            self.active_take = None

    def run(self):
        print('GATE READY ' + str(self.cue), flush=True)
        while not self.stopping:
            self.tick()
            time.sleep(self.poll)
        print('Gate stopped; existing state preserved.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path('.findback/interactive-take'))
    parser.add_argument('--delay', type=float, default=1.0)
    parser.add_argument('--poll', type=float, default=.05)
    parser.add_argument('--pace', type=float, default=1.0)
    args = parser.parse_args()
    if args.delay < 0 or args.poll <= 0 or args.pace < 0:
        parser.error('Delay/pace must be nonnegative and poll must be positive.')
    gate = CaptureGate(args.state_dir, delay=args.delay, poll=args.poll, pace=args.pace)
    signal.signal(signal.SIGTERM, gate.stop)
    signal.signal(signal.SIGINT, gate.stop)
    gate.run()


if __name__ == '__main__':
    main()
