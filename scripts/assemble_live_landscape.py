#!/usr/bin/env python3
"""Cut fresh full-desktop capture streams to the unchanged Chris voice track.

The plan selects actual timestamp windows, never reconstructed UI. Native frame
cadence is retained inside each window; reviewed waits are cut between windows,
and the last real frame is held when a narration slot needs more time.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import shutil
import tempfile

from encode_chat_capture import jpeg_size


def run(command):
    result = subprocess.run(command, capture_output=True)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace")[-4000:])
    return result


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,sample_rate,channels:format=duration,size",
        "-of", "json", str(path)]).stdout)


def audio_digest(path):
    return hashlib.sha256(run(["ffmpeg", "-v", "error", "-i", str(path),
        "-map", "0:a:0", "-c:a", "copy", "-f", "adts", "-"]).stdout).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-root", type=Path, default=Path(".findback/landscape-capture"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, default=Path("demo/landscape"))
    parser.add_argument("--audio-video", type=Path, default=Path("demo/FindBack-submission-demo-male.mp4"))
    parser.add_argument("--narration-manifest", type=Path, default=Path("demo/narrated-male/narrated-manifest.json"))
    parser.add_argument("--alignment-root", type=Path, default=Path("demo/broll-male"))
    parser.add_argument("--output-width", type=int, default=1920)
    parser.add_argument("--output-height", type=int, default=1080)
    args = parser.parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True)
    plan = json.loads(args.plan.read_text())
    narration = json.loads(args.narration_manifest.read_text())
    duration = narration["audio_seconds"]
    captures, source_manifests = {}, {}
    for stream in {shot["stream"] for chapter in plan["chapters"] for shot in chapter["shots"]}:
        if Path(stream).name != stream:
            raise ValueError("Capture stream must be a local directory name.")
        source = args.capture_root / stream / "frames.json"
        rows = json.loads(source.read_text())
        if len(rows) < 2 or any(b["at"] <= a["at"] for a, b in zip(rows, rows[1:])):
            raise ValueError("Fresh capture timestamps must increase.")
        captures[stream] = rows
        source_manifests[stream] = {"manifest_sha256": digest(source), "frame_count": len(rows),
            "first_at_ms": rows[0]["at"], "last_at_ms": rows[-1]["at"]}
    edited, report, selected_sources = [], [], {}
    capture_dimensions = None
    total = 0.0
    for entry in plan["chapters"]:
        number = entry["chapter"]
        chapter = narration["chapters"][number - 1]
        alignment_path = args.alignment_root / f"chapter-{number:02d}.alignment.json"
        alignment = json.loads(alignment_path.read_text())["alignment"]
        characters = "".join(alignment["characters"])
        starts = []
        for index, shot in enumerate(entry["shots"]):
            cue = shot.get("cue")
            if index == 0:
                if cue:
                    raise ValueError("First shot must begin at the chapter start.")
                starts.append(0.0)
            else:
                char_index = characters.find(cue)
                if char_index < 0:
                    raise ValueError("Missing actual spoken cue: " + cue)
                starts.append(alignment["character_start_times_seconds"][char_index])
        for index, shot in enumerate(entry["shots"]):
            end = starts[index + 1] if index + 1 < len(starts) else chapter["audio_seconds"]
            available = end - starts[index]
            if available <= 0:
                raise ValueError("Narration shot cues must increase.")
            rows = captures[shot["stream"]]
            intervals = [(b["at"] - a["at"]) / 1000 for a, b in zip(rows, rows[1:])]
            cadence = min(.3, statistics.median(intervals))
            windows = shot.get("windows")
            ranges = shot.get("ranges")
            chunks = ([[row for row in rows if start <= row["at"] <= stop] for start, stop in windows]
                      if windows else [rows[start:stop + 1] for start, stop in ranges])
            actual = []
            for chunk in chunks:
                if not chunk:
                    raise ValueError(f"Empty capture window in chapter {number}.")
                for position, row in enumerate(chunk):
                    seconds = ((chunk[position + 1]["at"] - row["at"]) / 1000
                               if position + 1 < len(chunk) else cadence)
                    if seconds > 2:
                        raise ValueError("A long static wait remains inside a window; explicitly select narrower reviewed windows.")
                    image = (args.capture_root / shot["stream"] / row["file"]).resolve()
                    image.relative_to(args.capture_root.resolve())
                    key = shot["stream"] + "/" + row["file"]
                    if key not in selected_sources:
                        size = jpeg_size(image)
                        if size[0] < 1280 or abs(size[0] / size[1] - 16 / 9) > .005:
                            raise ValueError("A selected frame is not a fresh full landscape viewport: " + key)
                        capture_dimensions = capture_dimensions or size
                        selected_sources[key] = {"at": row["at"], "sha256": digest(image), "dimensions": size}
                    actual.append({"file": key, "path": image, "source_at_ms": row["at"], "seconds": seconds})
            native = sum(row["seconds"] for row in actual)
            if native > available + .001:
                raise ValueError(f"Chapter {number} shot has {native:.3f}s actual footage but only {available:.3f}s voice; trim reviewed waits explicitly.")
            hold = available - native
            actual[-1]["seconds"] += hold
            edited.extend(actual)
            report.append({"chapter": number, "stream": shot["stream"], "cue": shot.get("cue"),
                "purpose": shot.get("purpose"), "output_start_seconds": total,
                "output_seconds": available, "actual_capture_seconds": native,
                "final_real_frame_hold_seconds": hold, "selected_frame_count": len(actual),
                "source_windows": windows, "source_ranges": ranges})
            total += available
    if abs(total - duration) > .005:
        raise ValueError("The edit plan must cover all eight narration chapters exactly.")
    first = probe(edited[0]["path"])["streams"][0]
    source_width, source_height = first["width"], first["height"]
    if source_width < 1280 or abs(source_width / source_height - 16 / 9) > .005:
        raise ValueError("Only fresh full landscape captures are accepted; portrait sources are refused.")
    output = args.output_root / "FindBack-landscape-demo.mp4"
    before_audio = audio_digest(args.audio_video)
    with tempfile.TemporaryDirectory(prefix="findback-live-landscape-") as temporary:
        listing = Path(temporary) / "actual-frames.ffconcat"
        lines = ["ffconcat version 1.0"]
        for row in edited:
            quoted = str(row["path"]).replace("'", "'\\''")
            lines.extend([f"file '{quoted}'", "option framerate 1000", f"duration {row['seconds']:.6f}"])
        quoted = str(edited[-1]["path"]).replace("'", "'\\''")
        lines.extend([f"file '{quoted}'", "option framerate 1000", "duration 1"])
        listing.write_text("\n".join(lines) + "\n")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-safe", "0", "-f", "concat",
             "-i", str(listing), "-i", str(args.audio_video), "-map", "0:v:0", "-map", "1:a:0",
             "-vf", f"scale={args.output_width}:{args.output_height}:flags=lanczos,setsar=1,fps=24,tpad=stop_mode=clone:stop_duration=1",
             "-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "fast", "-crf", "18",
             "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart",
             "-metadata", "title=FindBack — fresh live chat, full landscape desktop capture",
             "-metadata", "comment=Actual fresh browser message arrivals and scrolling, edited with reviewed static waits removed. Fictional participants and items; real Switch/model responses. Chris narration at native pace, unchanged AAC audio. Full viewport upscale only; no cropped or reconstructed UI.", str(output)])
    info = probe(output)
    if audio_digest(output) != before_audio:
        raise ValueError("The existing narration AAC stream changed.")
    for row in edited:
        if digest(row["path"]) != selected_sources[row["file"]]["sha256"]:
            raise ValueError("A source capture changed while encoding.")
    run(["ffmpeg", "-hide_banner", "-v", "error", "-i", str(output), "-f", "null", "-"])
    result = {"kind": "fresh actual landscape browser capture with real message arrivals",
        "fictional_demo": True, "continuous_event_capture_claimed": False,
        "source_dimensions": [source_width, source_height], "output_dimensions": [args.output_width, args.output_height],
        "source_dimensions_seen": sorted({tuple(row["dimensions"]) for row in selected_sources.values()}),
        "transform": "Full 16:9 viewport scaled uniformly with Lanczos; no crop or padding, no generated UI.",
        "voice_tempo": 1.0, "audio_stream_copied_bit_for_bit": True, "aac_adts_sha256": before_audio,
        "video_file": output.name, "video_sha256": digest(output), "ffprobe": info,
        "source_manifests": source_manifests, "selected_sources": selected_sources,
        "shots": report, "plan_sha256": digest(args.plan)}
    (args.output_root / "landscape-manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    subtitles = args.audio_video.with_suffix(".srt")
    if subtitles.exists():
        shutil.copyfile(subtitles, output.with_suffix(".srt"))
    (args.output_root / "README.md").write_text(
        "# Fresh full-desktop FindBack demonstration\n\n"
        f"[Play the landscape video]({output.name}). Actual source viewport: **{source_width} × {source_height}**; "
        f"output: **{args.output_width} × {args.output_height}**, H.264 at 24 fps. The entire 16:9 viewport is scaled uniformly; "
        "there is no crop, padding, reconstructed interface or portrait footage in this cut.\n\n"
        "The edit combines freshly captured recent public-thread context with actual message-arrival and scrolling windows. "
        "The fictional participants post through distinct real accounts, and agent reactions run through the app and Switch. "
        "Captured cadence is retained within selected windows; reviewed static waits are cut, and real frames are held where needed. "
        "This is an edited demonstration, not continuous footage of every event. It ends at approved collection before handover or receipt.\n\n"
        "Chris's existing normal-pace 118.800-second narration is copied as an identical AAC stream. "
        "No new TTS call, voice speed change or audio processing is applied. The existing subtitles are unchanged. "
        "Source timestamps, selected image hashes, narration cues and holds are recorded in `landscape-manifest.json`.\n")
    print(json.dumps({"video": str(output.resolve()), "sha256": result["video_sha256"], "duration": info["format"]["duration"]}, indent=2))


if __name__ == "__main__":
    main()
