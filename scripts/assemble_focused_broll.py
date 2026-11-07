#!/usr/bin/env python3
"""Encode actual browser captures into eight silent clips and a 180s assembly.

Original JPEGs are never changed. Short clips hold their final actual frame;
long clips are cut at the requested duration. There is no generated interface,
interpolated motion, added text, music or narration.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile


DURATIONS = (18, 20, 22, 17, 28, 30, 25, 20)


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,nb_frames:format=duration,size",
        "-of", "json", str(path)]).stdout)


def scene_name(value):
    number = int(value.removeprefix("scene-"))
    if number not in range(1, 9):
        raise ValueError("Scenes must be 1 through 8.")
    return f"scene-{number:02d}"


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def encode_scene(scene, args, records, labels, omissions):
    number = int(scene[-2:])
    source = args.capture_root.resolve() / scene / "frames.json"
    native_dir = args.output_root.resolve() / "native" / scene
    native_dir.mkdir(parents=True, exist_ok=True)
    native = native_dir / "capture.mp4"
    native_manifest = native_dir / "capture-manifest.json"
    command = [sys.executable, str(Path(__file__).with_name("encode_chat_capture.py")),
               "--frames", str(source), "--output", str(native), "--manifest", str(native_manifest),
               "--gap-threshold", "1000000000", "--last-duration", "0.7"]
    run(command)
    capture = json.loads(native_manifest.read_text())
    presentation_source = native
    presentation_manifest = native_manifest
    source_info = capture
    if omissions.get(scene) or any((row.get("source_gap_seconds") or 0) > 2 for row in capture["timeline"]):
        edited_dir = args.output_root.resolve() / "edited" / scene
        edited_dir.mkdir(parents=True, exist_ok=True)
        presentation_source = edited_dir / "capture.mp4"
        presentation_manifest = edited_dir / "capture-manifest.json"
        edited_command = [sys.executable, str(Path(__file__).with_name("encode_chat_capture.py")),
                          "--frames", str(source), "--output", str(presentation_source),
                          "--manifest", str(presentation_manifest), "--gap-threshold", "2",
                          "--gap-duration", "0.7", "--last-duration", "0.7"]
        for item in omissions.get(scene, []):
            edited_command.extend(["--omit", item])
        run(edited_command)
        source_info = json.loads(presentation_manifest.read_text())
    target = DURATIONS[number - 1]
    clip = args.output_root.resolve() / "clips" / f"{scene}.mp4"
    clip.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(presentation_source),
         "-an", "-vf", f"tpad=stop_mode=clone:stop_duration={target}",
         "-t", str(target), "-r", "24", "-fps_mode", "cfr", "-c:v", "libx264",
         "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
         "-movflags", "+faststart", "-metadata",
         "comment=Actual Mattermost browser captures. Final actual frame held to target length. Fictional demo; no generated UI.",
         str(clip)])
    info = probe(clip)
    stream = info["streams"][0]
    if stream["codec_name"] != "h264" or stream["avg_frame_rate"] != "24/1":
        raise ValueError("Unexpected clip encoding.")
    if abs(float(info["format"]["duration"]) - target) > 1 / 24:
        raise ValueError("Unexpected clip duration.")
    native_duration = capture["edited_duration_seconds"]
    presentation_duration = source_info["edited_duration_seconds"]
    records[scene] = {
        "scene": number, "label": labels.get(scene, records.get(scene, {}).get("label", f"Scene {number}")),
        "target_seconds": target, "native_seconds": native_duration,
        "presentation_source_seconds": presentation_duration,
        "final_actual_frame_hold_seconds": round(max(0, target - presentation_duration), 6),
        "tail_cut_seconds": round(max(0, presentation_duration - target), 6),
        "native_file": str(native.relative_to(args.output_root.resolve())),
        "native_manifest": str(native_manifest.relative_to(args.output_root.resolve())),
        "presentation_source_file": str(presentation_source.relative_to(args.output_root.resolve())),
        "presentation_source_manifest": str(presentation_manifest.relative_to(args.output_root.resolve())),
        "long_gaps_compressed": sum(row["gap_compressed"] for row in source_info["timeline"]),
        "clip_file": str(clip.relative_to(args.output_root.resolve())),
        "source_frame_count": capture["source_frame_count"],
        "retained_frame_count": source_info["retained_frame_count"],
        "source_manifest_sha256": capture["source_manifest_sha256"],
        "omissions": source_info["omitted_frames"], "clip_sha256": sha256(clip), "ffprobe": info,
    }
    print(f"{scene}: {native_duration:.3f}s actual capture → {target}s timed clip", flush=True)


def assemble(args, records):
    ordered = [f"scene-{number:02d}" for number in range(1, 9)]
    if any(scene not in records for scene in ordered):
        raise ValueError("All eight finalized scenes are required before assembly.")
    output = args.output_root.resolve() / "findback-silent-180s.mp4"
    with tempfile.TemporaryDirectory(prefix="findback-broll-") as temporary:
        listing = Path(temporary) / "clips.ffconcat"
        listing.write_text("ffconcat version 1.0\n" + "".join(
            "file '" + str((args.output_root / records[scene]["clip_file"]).resolve()).replace("'", "'\\''") + "'\n"
            for scene in ordered))
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-safe", "0", "-f", "concat",
             "-i", str(listing), "-an", "-c:v", "copy", "-movflags", "+faststart",
             "-metadata", "title=FindBack — actual chat B-roll, silent 3-minute assembly",
             "-metadata", "comment=Actual browser screenshots; scene cuts and final-frame holds. Fictional people and handovers. Not a continuous recording of every event.",
             str(output)])
    info = probe(output)
    if abs(float(info["format"]["duration"]) - 180) > 1 / 24:
        raise ValueError("The silent assembly must be exactly 180 seconds.")
    return {"file": output.name, "sha256": sha256(output), "ffprobe": info}


def write_readme(output, records, assembly):
    lines = ["# Actual Mattermost B-roll", "",
        "These clips show the actual local Mattermost browser UI. The demonstration uses fictional people, objects and handovers; FindBack's messages, Switch transport, model calls and checks run through the app. The intended final state is approved collection, before handover or receipt.", "",
        "The native clips encode timestamped browser screenshots at their captured cadence, with a final 0.7-second hold. They are not a continuous recording of every event. Timed clips compress gaps longer than two seconds to 0.7 seconds, hold the final actual frame when short, and cut the tail when long. No UI, chat, motion, narration, text overlay or graphics are generated. Source pixels are padded to even dimensions where necessary and are not rescaled or cropped.", "",
        "The silent assembly is a timing scaffold for a future voice recording. Scene boundaries can be moved to match the actual narration; do not treat these fixed durations as measured speech timing.", "",
        "| Scene | View | Assembly start | Target length | Native capture | After gap cuts | Extra final-frame hold |", "|---|---|---:|---:|---:|---:|---:|"]
    cursor = 0
    for number in range(1, 9):
        scene = f"scene-{number:02d}"
        if scene not in records:
            continue
        row = records[scene]
        presented = row.get("presentation_source_seconds", row["native_seconds"])
        lines.append(f"| [{number}]({row['clip_file']}) | {row['label']} | {cursor // 60}:{cursor % 60:02d} | {row['target_seconds']}s | {row['native_seconds']:.3f}s | {presented:.3f}s | {row['final_actual_frame_hold_seconds']:.3f}s |")
        cursor += row["target_seconds"]
    lines.extend(["", "Native captures and byte-for-byte final screenshots are in `native/scene-XX/`. Each native manifest records original JPEG hashes, timestamps, exact omissions and encoding details. `broll-manifest.json` records the timed clips, holds, cuts and hashes.", ""])
    if assembly:
        lines.append("[Play the 180-second silent assembly](findback-silent-180s.mp4).")
    (output / "README.md").write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-root", type=Path, default=Path(".findback/focused-capture"))
    parser.add_argument("--output-root", type=Path, default=Path("demo/broll"))
    parser.add_argument("--scene", action="append", default=[])
    parser.add_argument("--label", action="append", default=[], metavar="SCENE=DESCRIPTION")
    parser.add_argument("--omit", action="append", default=[], metavar="SCENE/FILENAME=REASON")
    parser.add_argument("--assemble", action="store_true")
    args = parser.parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = args.output_root / "broll-manifest.json"
    prior = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    records = prior.get("scenes", {})
    labels = {scene_name(key): value for key, value in (item.split("=", 1) for item in args.label)}
    omissions = {}
    for item in args.omit:
        scene, value = item.split("/", 1)
        omissions.setdefault(scene_name(scene), []).append(value)
    selected = args.scene or ([] if args.assemble else [str(number) for number in range(1, 9)])
    for value in selected:
        encode_scene(scene_name(value), args, records, labels, omissions)
    for scene, label in labels.items():
        if scene in records:
            records[scene]["label"] = label
    assembly = assemble(args, records) if args.assemble else prior.get("assembly")
    report = {"schema": 1, "updated_at": datetime.now(timezone.utc).isoformat(),
              "kind": "actual browser B-roll with transparent scene cuts and final-frame holds",
              "voice_track": None, "scenes": records, "assembly": assembly}
    save(manifest_path, report)
    write_readme(args.output_root, records, assembly)


if __name__ == "__main__":
    main()
