"""Download an existing, exact Miles story from official ElevenLabs history.

This helper only sends GET requests. It never generates speech or purchases
anything. Both the voice ID and the full approved script must match, and a
required generation-start timestamp excludes earlier previews.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any

import httpx

API = "https://api.elevenlabs.io"
VOICE_ID = "vSjOBQp24DUB2COr2xI9"
VOICE_NAME = "Miles - Confident, Upbeat and Smooth"
QUOTE_MAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "\u00a0": " "})


class HistoryError(RuntimeError):
    """A safe error that does not expose API credentials or unrelated history."""


def normalize_text(text: str) -> str:
    return " ".join(text.translate(QUOTE_MAP).split())


def is_match(item: dict[str, Any], script: str, not_before: int, not_after: int | None) -> bool:
    date = item.get("date_unix")
    return (item.get("voice_id") == VOICE_ID
            and isinstance(date, (int, float))
            and date >= not_before
            and (not_after is None or date < not_after)
            and normalize_text(item.get("text") or "") == normalize_text(script))


def select_latest(items: list[dict[str, Any]], script: str, not_before: int,
                  not_after: int | None) -> dict[str, Any]:
    matches = {x["history_item_id"]: x for x in items
               if x.get("history_item_id") and is_match(x, script, not_before, not_after)}
    if not matches:
        raise HistoryError("No exact new Miles story is available in the specified time window.")
    ordered = sorted(matches.values(), key=lambda x: x["date_unix"], reverse=True)
    if len(ordered) > 1 and ordered[0]["date_unix"] == ordered[1]["date_unix"]:
        raise HistoryError("Several exact stories have the same timestamp; specify the history item ID.")
    return ordered[0]


def standardize_alignment(provider: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate saved timings, detecting history's duration-in-end-field format.

    For that format every listed duration must independently equal the next
    global start minus this start. Only then can end=start+duration be derived.
    The unmodified provider object is saved separately by the caller.
    """
    converted = {}
    evidence = {}
    for kind in ("alignment", "normalized_alignment"):
        if kind not in provider:
            continue
        a = provider[kind]
        arrays = [a.get(k) for k in ("characters", "character_start_times_seconds", "character_end_times_seconds")]
        if not all(isinstance(x, list) for x in arrays) or not arrays[0] or len(set(map(len, arrays))) != 1:
            raise HistoryError("Provider character alignment is incomplete.")
        chars, starts, ends = arrays
        if not all(isinstance(x, (int, float)) and math.isfinite(x) and x >= 0 for x in starts + ends):
            raise HistoryError("Provider alignment contains invalid timing values.")
        if any(y < x for x, y in zip(starts, starts[1:])):
            raise HistoryError("Provider alignment starts are not globally ordered.")
        if all(e >= s for s, e in zip(starts, ends)):
            actual_ends = ends
            conversion = {"method": "Provider absolute end times retained unchanged."}
        else:
            if len(starts) < 2:
                raise HistoryError("Insufficient timings to verify the provider duration format.")
            error = max(abs((starts[i + 1] - starts[i]) - ends[i]) for i in range(len(starts) - 1))
            if error > 0.002:
                raise HistoryError("Provider end fields are neither valid absolute ends nor verifiable character durations.")
            actual_ends = [s + d for s, d in zip(starts, ends)]
            conversion = {"method": "History end field contains per-character durations; derived absolute_end=global_start+duration.",
                          "independent_next_start_deltas_checked": len(starts) - 1,
                          "maximum_delta_error_seconds": error}
        converted[kind] = {"characters": chars, "character_start_times_seconds": starts,
                           "character_end_times_seconds": actual_ends}
        evidence[kind] = {**conversion, "characters": len(chars), "last_spoken_end_seconds": actual_ends[-1]}
    if not converted:
        raise HistoryError("Provider alignment is unavailable.")
    return converted, evidence


def get(client: httpx.Client, path: str, *, params=None) -> httpx.Response:
    if not re.fullmatch(r"/v1/history(?:/[A-Za-z0-9_-]+(?:/audio)?)?", path):
        raise HistoryError("Refused a non-history endpoint.")
    try:
        response = client.get(API + path, params=params)
    except httpx.HTTPError as exc:
        raise HistoryError("Official history GET failed (" + type(exc).__name__ + ").") from None
    if response.status_code != 200:
        raise HistoryError(f"Official history GET returned HTTP {response.status_code}; no generation was attempted.")
    return response


def download(client: httpx.Client, *, source: Path, out: Path, not_before: int,
             not_after: int | None = None, history_id: str | None = None,
             expected_model: str = "eleven_multilingual_v2") -> dict[str, Any]:
    script = (source / "narration-script.txt").read_text(encoding="utf-8")
    plan = json.loads((source / "story-plan.json").read_text())
    digest = hashlib.sha256(script.encode()).hexdigest()
    if plan.get("status") != "ROOT_SCRIPT_APPROVED" or digest != plan.get("script_sha256"):
        raise HistoryError("The source script does not match the approved plan.")
    if not_before <= 0 or (not_after is not None and not_after <= not_before):
        raise HistoryError("A valid explicit generation timestamp window is required.")
    if history_id:
        detail = get(client, "/v1/history/" + history_id).json()
    else:
        params: dict[str, Any] = {"voice_id": VOICE_ID, "date_after_unix": not_before,
                                  "page_size": 100, "sort_direction": "desc"}
        if not_after is not None:
            params["date_before_unix"] = not_after
        items = []
        for _ in range(10):
            page = get(client, "/v1/history", params=params).json()
            items.extend(page.get("history") or [])
            if not page.get("has_more"):
                break
            cursor = page.get("last_history_item_id")
            if not cursor or cursor == params.get("start_after_history_item_id"):
                raise HistoryError("History pagination did not advance safely.")
            params["start_after_history_item_id"] = cursor
        else:
            raise HistoryError("The bounded history scan needs a narrower time window.")
        chosen = select_latest(items, script, not_before, not_after)
        detail = get(client, "/v1/history/" + chosen["history_item_id"]).json()
    if not is_match(detail, script, not_before, not_after):
        raise HistoryError("The selected history metadata fails the exact voice/text/time checks.")
    if detail.get("model_id") != expected_model:
        raise HistoryError("The exact story uses a different model from the confirmed browser settings.")
    selected_id = detail.get("history_item_id")
    if not isinstance(selected_id, str) or (history_id and selected_id != history_id):
        raise HistoryError("History identity changed between selection and detail verification.")
    alignment = detail.get("alignments")
    if not isinstance(alignment, dict) or not (alignment.get("alignment") or alignment.get("normalized_alignment")):
        raise HistoryError("The exact story exists but provider alignment is unavailable; no audio was downloaded.")
    global_alignment, alignment_evidence = standardize_alignment(alignment)
    outputs = ("narration-original.mp3", "narration-script.txt", "global-alignment.json", "story-plan.json",
               "generation-metadata.json", "history-metadata.json", "narration-raw.wav", "narration.wav",
               "narration.m4a", "narration-manifest.json", "provider-alignment.json")
    if any((out / name).exists() for name in outputs):
        raise HistoryError("Output already contains narration files; refusing to overwrite prior audio.")
    # This is the only audio request, after every identity/content/time check.
    audio_response = get(client, "/v1/history/" + selected_id + "/audio")
    content_type = audio_response.headers.get("content-type", "").split(";", 1)[0]
    audio = audio_response.content
    if len(audio) < 1000 or content_type not in ("audio/mpeg", "audio/mp3", "application/octet-stream"):
        raise HistoryError("Official history did not return the expected MP3 audio.")
    out.mkdir(parents=True, exist_ok=True)
    (out / "narration-original.mp3").write_bytes(audio)
    (out / "narration-script.txt").write_text(script, encoding="utf-8")
    (out / "provider-alignment.json").write_text(json.dumps(alignment, ensure_ascii=False) + "\n")
    (out / "global-alignment.json").write_text(json.dumps(global_alignment, ensure_ascii=False) + "\n")
    plan.update(voice_id=VOICE_ID, voice_name=detail.get("voice_name") or VOICE_NAME,
                model_id=detail.get("model_id"), tts_generation_permitted=False,
                generation_source="Existing official browser-generated speech; GET-only history download.")
    (out / "story-plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    metadata = {"provider": "ElevenLabs", "official_api": API, "voice_id": VOICE_ID,
                "voice_name": detail.get("voice_name") or VOICE_NAME, "model_id": detail.get("model_id"),
                "history_item_id": selected_id, "history_date_unix": detail["date_unix"],
                "generation_source": "ElevenLabs browser UI; existing audio downloaded with official history GET API.",
                "downloader_generated_speech": False, "download_method": "GET", "native_tempo_factor": 1.0,
                "single_continuous_generation": True, "input_characters": len(detail["text"]),
                "script_sha256": digest, "ai_generated_narration": True,
                "downloaded_at_unix": int(time.time()), "downloaded_bytes": len(audio),
                "original_audio_sha256": hashlib.sha256(audio).hexdigest(),
                "matching": {"exact_voice": True, "exact_script_after_quotes_and_spacing_normalization": True,
                             "date_after_unix_inclusive": not_before, "date_before_unix_exclusive": not_after},
                "provider_alignment_preserved": True, "provider_alignment_file": "provider-alignment.json",
                "alignment_conversion": alignment_evidence}
    metadata["voice_settings"] = {k: v for k, v in (detail.get("settings") or {}).items()
                                  if k in ("speed", "stability", "similarity_boost", "style", "use_speaker_boost")}
    (out / "generation-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    # Allowlist only relevant provider metadata; no account data or credentials.
    saved = {k: detail.get(k) for k in ("history_item_id", "voice_id", "voice_name", "model_id", "text",
             "date_unix", "content_type", "state", "source", "output_format", "alignments")}
    saved["settings"] = metadata["voice_settings"]
    (out / "history-metadata.json").write_text(json.dumps(saved, indent=2, ensure_ascii=False) + "\n")
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--not-before", type=int, required=True)
    parser.add_argument("--not-after", type=int)
    parser.add_argument("--history-id")
    parser.add_argument("--expected-model", default="eleven_multilingual_v2")
    parser.add_argument("--source-dir", type=Path, default=Path("demo/story-voiceover"))
    parser.add_argument("--output-dir", type=Path, default=Path("demo/miles-story"))
    parser.add_argument("--key-file", type=Path, default=Path(".findback/elevenlabs.key"))
    parser.add_argument("--prepare", action="store_true", help="Decode and derive continuous master/14 edit beats locally.")
    args = parser.parse_args()
    try:
        key = args.key_file.read_text().strip()
        if not key:
            raise HistoryError("The private API credential file is empty.")
        with httpx.Client(headers={"xi-api-key": key}, timeout=120, follow_redirects=False) as client:
            metadata = download(client, source=args.source_dir.resolve(), out=args.output_dir.resolve(),
                                not_before=args.not_before, not_after=args.not_after, history_id=args.history_id,
                                expected_model=args.expected_model)
        if args.prepare:
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(args.output_dir / "narration-original.mp3"),
                            "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(args.output_dir / "narration-raw.wav")], check=True)
            subprocess.run([sys.executable, str(Path(__file__).with_name("prepare_story_audio.py")),
                            "--output-dir", str(args.output_dir)], check=True)
        print(json.dumps({k: metadata[k] for k in ("voice_id", "voice_name", "history_item_id", "history_date_unix", "downloaded_bytes")}, indent=2))
    except (HistoryError, OSError, ValueError) as exc:
        # Deliberately do not print arbitrary HTTP bodies, headers, or request objects.
        print(str(exc) if isinstance(exc, HistoryError) else "Local input/output validation failed (" + type(exc).__name__ + ").", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
