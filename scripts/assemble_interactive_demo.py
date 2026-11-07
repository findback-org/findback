#!/usr/bin/env python3
"""Edit the fresh interactive recording with cue-centered crossdissolves.

Every image comes from the new actual browser capture. Arrival/progress windows
retain their timestamp cadence. Ten-frame dissolves use five-frame real-image
handles and never synthesize typing, messages or interface elements.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import hashlib
import math
from pathlib import Path
import shutil
import statistics
import tempfile

from assemble_live_landscape import run, digest, probe, audio_digest
from encode_chat_capture import jpeg_size


FPS = 24
FADE_FRAMES = 10
HANDLE_FRAMES = FADE_FRAMES // 2


def load_capture(root: Path, stream: str):
    if Path(stream).name != stream:
        raise ValueError("Use a local capture stream name.")
    manifest = root / stream / "frames.json"
    rows = json.loads(manifest.read_text())
    if len(rows) < 2 or any(b["at"] <= a["at"] for a, b in zip(rows, rows[1:])):
        raise ValueError("Capture timestamps must increase.")
    intervals = [(b["at"] - a["at"]) / 1000 for a, b in zip(rows, rows[1:]) if b["at"] - a["at"] < 1000]
    return rows, statistics.median(intervals), digest(manifest)


def prepare(args):
    plan = json.loads(args.plan.read_text())
    narration = json.loads(args.narration_manifest.read_text())
    capture_root = args.capture_root.resolve()
    streams = {shot["stream"] for chapter in plan["chapters"] for shot in chapter["shots"]}
    captures = {name: load_capture(capture_root, name) for name in streams}
    sources, clips = {}, []
    expected_frame = 0
    for entry in plan["chapters"]:
        number = entry["chapter"]
        chapter = narration["chapters"][number - 1]
        alignment = json.loads((args.alignment_root / f"chapter-{number:02d}.alignment.json").read_text())["alignment"]
        characters = "".join(alignment["characters"])
        starts = [0.0]
        for shot in entry["shots"][1:]:
            index = characters.find(shot["cue"])
            if index < 0:
                raise ValueError("Missing actual spoken cue: " + shot["cue"])
            starts.append(alignment["character_start_times_seconds"][index])
        if entry["shots"][0].get("cue"):
            raise ValueError("First chapter shot must start at zero.")
        for shot_index, shot in enumerate(entry["shots"]):
            start = chapter["audio_start_seconds"] + starts[shot_index]
            end = chapter["audio_start_seconds"] + (starts[shot_index + 1] if shot_index + 1 < len(starts) else chapter["audio_seconds"])
            start_frame, end_frame = round(start * FPS), round(end * FPS)
            if start_frame != expected_frame or end_frame <= start_frame:
                raise ValueError("The plan must cover narration continuously and in order.")
            rows, cadence, _ = captures[shot["stream"]]
            if shot.get("windows"):
                chunks = [[row for row in rows if first <= row["at"] <= last] for first, last in shot["windows"]]
            else:
                chunks = [rows[first:last + 1] for first, last in shot["ranges"]]
            native_chunks = []
            for chunk in chunks:
                if not chunk:
                    raise ValueError(f"Empty actual capture window in chapter {number}.")
                frames = []
                for index, row in enumerate(chunk):
                    seconds = ((chunk[index + 1]["at"] - row["at"]) / 1000 if index + 1 < len(chunk) else cadence)
                    if seconds > 2 and not shot.get("allow_native_gaps"):
                        raise ValueError("A native gap exceeds2s; review it, then explicitly set allow_native_gaps to preserve an actual arrival across it.")
                    image = (capture_root / shot["stream"] / row["file"]).resolve()
                    image.relative_to(capture_root)
                    key = shot["stream"] + "/" + row["file"]
                    if key not in sources:
                        size = jpeg_size(image)
                        if size != (1600, 900):
                            raise ValueError(f"Expected verified full 1600x900 viewport, got {size}: {key}")
                        sources[key] = {"at": row["at"], "sha256": digest(image), "dimensions": size}
                    frames.append({"path": str(image), "source": key, "at": row["at"], "seconds": seconds})
                native_chunks.append((frames, sum(frame["seconds"] for frame in frames)))
            remaining = end_frame - start_frame
            for index, (frames, native_seconds) in enumerate(native_chunks):
                leading_hold = float(shot.get("leading_hold_seconds", 0)) if index == 0 else 0.0
                native_frames = math.ceil((native_seconds + leading_hold) * FPS - 1e-9)
                allocation = native_frames if index + 1 < len(native_chunks) else remaining
                if allocation < native_frames:
                    raise ValueError(f"Chapter {number}: native arrival windows need {native_seconds:.3f}s but only {allocation/FPS:.3f}s remains. Trim reviewed waits, never retime arrivals.")
                clips.append({"chapter": number, "stream": shot["stream"], "cue": shot.get("cue"),
                    "purpose": shot.get("purpose"), "nominal_start_frame": start_frame,
                    "nominal_frames": allocation, "native_seconds": native_seconds,
                    "reading_hold_seconds": allocation / FPS - native_seconds - leading_hold,
                    "leading_hold_seconds": leading_hold,
                    "source_first_at_ms": frames[0]["at"], "source_last_at_ms": frames[-1]["at"],
                    "arrival_at_ms": shot.get("arrival_at_ms", []),
                    "allow_native_gaps": bool(shot.get("allow_native_gaps")), "frames": frames})
                start_frame += allocation
                remaining -= allocation
            expected_frame = end_frame
    if not args.cache_only and expected_frame != round(narration["audio_seconds"] * FPS):
        raise ValueError("All narration beats must be covered exactly.")
    for index, clip in enumerate(clips):
        clip["head_handle_frames"] = HANDLE_FRAMES if index else 0
        clip["tail_handle_frames"] = HANDLE_FRAMES if index + 1 < len(clips) else 0
        clip["render_frames"] = clip["nominal_frames"] + clip["head_handle_frames"] + clip["tail_handle_frames"]
        # These timestamps mean OBSERVED first-visible frame times, not server
        # creation times: network/render/capture latency can differ by seconds.
        arrivals = clip["arrival_at_ms"]
        if isinstance(arrivals, (int, float)):
            arrivals = [arrivals]
        opacity = []
        for arrival in arrivals:
            if clip["source_first_at_ms"] <= arrival <= clip["source_last_at_ms"]:
                local = clip["leading_hold_seconds"] + (arrival - clip["source_first_at_ms"]) / 1000
                before = local - clip["head_handle_frames"] / FPS
                after = clip["nominal_frames"] / FPS - clip["tail_handle_frames"] / FPS - local
                if index and before < .5:
                    raise ValueError("An observed arrival is too close to the completed dissolve; select more pre-roll.")
                if after < .5:
                    raise ValueError("An observed arrival needs more opaque reading time before the next dissolve/end.")
                opacity.append({"first_visible_at_ms": arrival,
                    "estimated_output_seconds": clip["nominal_start_frame"] / FPS + local,
                    "opaque_seconds_before_arrival": before, "opaque_seconds_after_arrival": after})
        clip["arrival_opacity_checks"] = opacity
    first_story = plan.get("first_story_post_at_ms")
    if first_story and clips[0]["source_first_at_ms"] > first_story - 1000:
        raise ValueError("Opening must begin at least 1s before the first actual story post.")
    return plan, narration, captures, sources, clips


def render_clip(clip, index, directory):
    listing = directory / f"window-{index:03d}.ffconcat"
    lines = ["ffconcat version 1.0"]
    for frame in clip["frames"]:
        quoted = frame["path"].replace("'", "'\\''")
        lines.extend([f"file '{quoted}'", "option framerate 1000", f"duration {frame['seconds']:.6f}"])
    quoted = clip["frames"][-1]["path"].replace("'", "'\\''")
    lines.extend([f"file '{quoted}'", "option framerate 1000", "duration 1"])
    listing.write_text("\n".join(lines) + "\n")
    output = directory / f"window-{index:03d}.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-safe", "0", "-f", "concat", "-i", str(listing),
         "-an", "-vf", f"scale=1920:1080:flags=lanczos,setsar=1,fps=24,tpad=start_mode=clone:start_duration={clip['head_handle_frames']/FPS+clip['leading_hold_seconds']:.6f}:stop_mode=clone:stop_duration={clip['render_frames']/FPS+1:.6f}",
         "-frames:v", str(clip["render_frames"]), "-c:v", "libx264", "-threads", "2", "-preset", "veryfast", "-crf", "0",
         "-pix_fmt", "yuv420p", "-video_track_timescale", "24000", str(output)])
    info = probe(output)
    if abs(float(info["format"]["duration"]) - clip["render_frames"] / FPS) > .0001:
        raise ValueError("An intermediate window did not render to its exact transition length.")
    return output


def cached_window(clip, index, args, sources):
    identity = {"version": "lossless-window-v2", "clip": clip,
                "source_hashes": {frame["source"]: sources[frame["source"]]["sha256"] for frame in clip["frames"]}}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    output = args.cache_root / (key + ".mp4")
    if output.exists():
        info = probe(output)
        if abs(float(info["format"]["duration"]) - clip["render_frames"] / FPS) < .0001:
            return output
    args.cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="window-", dir=args.cache_root) as temporary:
        rendered = render_clip(clip, index, Path(temporary))
        rendered.replace(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-root", type=Path, default=Path(".findback/interactive-capture"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("demo/interactive"))
    parser.add_argument("--output-name", default="FindBack-interactive-demo.mp4")
    parser.add_argument("--audio-video", type=Path, default=Path("demo/FindBack-submission-demo-male.mp4"))
    parser.add_argument("--narration-manifest", type=Path, default=Path("demo/narrated-male/narrated-manifest.json"))
    parser.add_argument("--alignment-root", type=Path, default=Path("demo/broll-male"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--cache-only", action="store_true", help="Prepare contiguous early chapters without assembling a final video")
    parser.add_argument("--cache-root", type=Path, default=Path(".findback/interactive-edit-cache"))
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    if Path(args.output_name).name != args.output_name or not args.output_name.endswith(".mp4"):
        parser.error("Output name must be a local MP4 filename.")
    args.output_root.mkdir(parents=True, exist_ok=True)
    plan, narration, captures, sources, clips = prepare(args)
    summary = {"selected_windows": len(clips), "source_frames": len(sources),
               "nominal_seconds": sum(clip["nominal_frames"] for clip in clips) / FPS,
               "native_capture_seconds": sum(clip["native_seconds"] for clip in clips),
               "crossdissolve_seconds": FADE_FRAMES / FPS}
    print(json.dumps(summary), flush=True)
    if args.dry_run:
        return
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        paths = list(pool.map(lambda pair: cached_window(pair[1], pair[0], args, sources), enumerate(clips)))
    if args.cache_only:
        print(json.dumps({"cached_windows": len(paths), "cache_root": str(args.cache_root.resolve())}), flush=True)
        return
    audio_before = audio_digest(args.audio_video)
    voice_name = narration.get("voice_name", "Narrator")
    with tempfile.TemporaryDirectory(prefix="findback-interactive-") as temporary:
        directory = Path(temporary)
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-filter_complex_threads", "1"]
        for path in paths:
            command.extend(["-i", str(path)])
        command.extend(["-i", str(args.audio_video)])
        filters = [f"[{index}:v]settb=AVTB,setpts=PTS-STARTPTS[v{index}]" for index in range(len(paths))]
        current, total_frames = "v0", clips[0]["render_frames"]
        transitions = []
        for index in range(1, len(paths)):
            offset = (total_frames - FADE_FRAMES) / FPS
            label = f"fade{index}"
            filters.append(f"[{current}][v{index}]xfade=transition=fade:duration={FADE_FRAMES/FPS:.6f}:offset={offset:.6f}[{label}]")
            transitions.append({"start_seconds": offset, "center_seconds": offset + HANDLE_FRAMES/FPS,
                                "end_seconds": offset + FADE_FRAMES/FPS, "next_window": index})
            current = label
            total_frames += clips[index]["render_frames"] - FADE_FRAMES
        graph = directory / "crossdissolves.ffgraph"
        graph.write_text(";\n".join(filters) + "\n")
        output = args.output_root / args.output_name
        command.extend(["-filter_complex", graph.read_text(), "-map", f"[{current}]", "-map", f"{len(paths)}:a:0",
            "-r", "24", "-t", f"{narration['audio_seconds']:.6f}", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", "-metadata",
            "title=FindBack — fresh interactive chat with smooth room transitions", "-metadata",
            f"comment=Full1600x900 interactive capture begins before first story post, uniformly scaled1.2x to1920x1080 without crop/padding. Real arrivals/progress retained; reviewed waits removed with crossdissolves. Fictional people/items; real Switch and AI. Exact input narration by {voice_name} at native pace.", str(output)])
        run(command)
    info = probe(output)
    if audio_digest(output) != audio_before:
        raise ValueError("The input narration AAC stream changed.")
    if abs(float(info["format"]["duration"]) - narration["audio_seconds"]) > .1:
        raise ValueError("Crossdissolves changed narration alignment or final duration.")
    for source, record in sources.items():
        if digest(args.capture_root / source) != record["sha256"]:
            raise ValueError("An original fresh capture changed during encoding.")
    run(["ffmpeg", "-hide_banner", "-v", "error", "-i", str(output), "-f", "null", "-"])
    manifest = {"created_at": datetime.now(timezone.utc).isoformat(), "kind": "fresh full landscape interactive recording with smooth crossdissolves",
        "fictional_participants_and_items": True, "source_dimensions": [1600,900], "output_dimensions": [1920,1080],
        "pixel_transform": "Verified complete1600x900 viewport uniformly scaled1.2x to1920x1080 with Lanczos; no crop or padding.",
        "source_root": str(args.capture_root), "previous_take_frames_used": False,
        "transitions": transitions, "transition_frames": FADE_FRAMES, "transition_seconds": FADE_FRAMES/FPS,
        "intermediate_encoding": "Lossless H.264 windows; only final output uses CRF 18, avoiding repeated lossy text compression.",
        "transition_handles": "Five cloned real frames per side; actual arrival windows keep original cadence and full opacity.",
        "audio_stream_copied_bit_for_bit": True, "aac_adts_sha256": audio_before, "voice_tempo": 1.0,
        "narration_beat_count": len(narration["chapters"]), "narration_duration_seconds": narration["audio_seconds"],
        "narration_manifest_sha256": digest(args.narration_manifest), "narration_audio_file": args.audio_video.name,
        "voice_name": voice_name, "voice_id": narration.get("voice_id"), "script_sha256": narration.get("script_sha256"),
        "video_file": output.name, "video_sha256": digest(output), "ffprobe": info, "plan_sha256": digest(args.plan),
        "capture_manifest_sha256": {name: value[2] for name,value in captures.items()},
        "sources": sources, "windows": [{key:value for key,value in clip.items() if key!='frames'} for clip in clips],
        "summary": summary, "first_story_post_at_ms": plan.get("first_story_post_at_ms")}
    (args.output_root / "interactive-manifest.json").write_text(json.dumps(manifest,indent=2)+"\n")
    subtitles = args.audio_video.with_suffix(".srt")
    if subtitles.exists():
        shutil.copyfile(subtitles,output.with_suffix(".srt"))
    (args.output_root / "README.md").write_text(
        f"# Captured FindBack story\n\n[Play the narrated demo]({output.name}). "
        "This edit uses the full 1600×900 interactive capture recorded before the first story post, uniformly scaled 1.2× to 1920×1080. It shows actual message arrivals, real agent responses, "
        "and any typing/progress displayed by the app. The people and objects are fictional.\n\n"
        "Arrival windows keep their captured timestamp cadence. The edit removes reviewed static waits, adds reading time by holding real frames, "
        "and uses 10-frame (0.417s) crossdissolves with explicit 5-frame handles. It does not invent messages, typing, progress or interface elements. "
        "No old take, crop or padding is used. The 1.2× upscale preserves the full source viewport. This narrated cut is edited rather than a continuous record of every elapsed second.\n\n"
        f"The {narration['audio_seconds']:.3f}-second normal-pace master AAC narration by {voice_name} is copied without editing. The audio stream hash is verified bit-for-bit. "
        "The case ends at approved collection, before handover and receipt. The source hashes, native capture windows, real-frame holds and dissolve times "
        "are recorded in interactive-manifest.json.\n")
    print(json.dumps({"video":str(output.resolve()),"duration":info['format']['duration'],"sha256":manifest['video_sha256']},indent=2))


if __name__ == "__main__":
    main()
