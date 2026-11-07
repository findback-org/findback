#!/usr/bin/env python3
"""Encode unchanged browser JPEG captures; compress pauses, never invent frames.

Requires only Python's standard library, ffmpeg and ffprobe. Omissions must be
explicitly named with a human-reviewed reason. No text, UI or graphics are added.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def probe(path: Path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_name,width,height,avg_frame_rate,pix_fmt:format=duration,size",
         "-of", "json", str(path)], check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def jpeg_size(path: Path) -> tuple[int, int]:
    """Read a JPEG's SOF dimensions without decoding or altering its pixels."""
    data = path.read_bytes()
    if data[:2] != b"\xff\xd8":
        raise ValueError(f"Not a JPEG: {path.name}")
    offset = 2
    sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
           0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while offset < len(data):
        if data[offset] != 0xFF:
            raise ValueError(f"Malformed JPEG header: {path.name}")
        while data[offset] == 0xFF:
            offset += 1
        marker = data[offset]
        offset += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        length = int.from_bytes(data[offset:offset + 2], "big")
        if length < 2:
            break
        if marker in sof:
            height = int.from_bytes(data[offset + 3:offset + 5], "big")
            width = int.from_bytes(data[offset + 5:offset + 7], "big")
            return width, height
        offset += length
    raise ValueError(f"No JPEG dimensions: {path.name}")


def encode(args) -> dict:
    source = args.frames.resolve()
    rows = json.loads(source.read_text())
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("Expected a list of actual captures.")
    omissions = {}
    for value in args.omit:
        filename, separator, reason = value.partition("=")
        if not separator or not reason.strip():
            raise ValueError("Each --omit needs filename=human-reviewed reason.")
        omissions[filename] = reason
    captured = []
    previous = None
    dimensions = None
    for row in rows:
        name, at = row["file"], row["at"]
        if Path(name).name != name or not name.lower().endswith((".jpg", ".jpeg")):
            raise ValueError("Capture filenames must be local JPEG basenames.")
        if type(at) is not int or previous is not None and at <= previous:
            raise ValueError("Capture timestamps must increase strictly.")
        previous = at
        path = (source.parent / name).resolve()
        path.relative_to(source.parent)
        size = jpeg_size(path)
        dimensions = dimensions or size
        if size != dimensions:
            raise ValueError("Source capture dimensions changed; no rescaling is permitted.")
        captured.append({"file": name, "at": at, "sha256": digest(path), "path": path})
    unknown = omissions.keys() - {row["file"] for row in captured}
    if unknown:
        raise ValueError("Unknown omitted filenames: " + ", ".join(sorted(unknown)))
    selected = [row for row in captured if row["file"] not in omissions]
    if len(selected) < 2:
        raise ValueError("Need at least two retained actual captures.")
    timeline = []
    cursor = 0.0
    for index, row in enumerate(selected):
        gap = ((selected[index + 1]["at"] - row["at"]) / 1000
               if index + 1 < len(selected) else None)
        duration = (args.last_duration if gap is None else
                    args.gap_duration if gap > args.gap_threshold else gap)
        timeline.append({"file": row["file"], "source_at_ms": row["at"],
                         "output_start_seconds": round(cursor, 6),
                         "duration_seconds": duration, "source_gap_seconds": gap,
                         "gap_compressed": gap is not None and gap > args.gap_threshold})
        cursor += duration
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    width, height = dimensions
    with tempfile.TemporaryDirectory(prefix="findback-capture-") as temporary:
        listing = Path(temporary) / "capture.ffconcat"
        lines = ["ffconcat version 1.0"]
        for row, timing in zip(selected, timeline):
            quoted = str(row["path"]).replace("'", "'\\''")
            lines.extend([f"file '{quoted}'", "option framerate 1000",
                          f"duration {timing['duration_seconds']:.6f}"])
        quoted = str(selected[-1]["path"]).replace("'", "'\\''")
        lines.extend([f"file '{quoted}'", "option framerate 1000"])
        listing.write_text("\n".join(lines) + "\n")
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-safe", "0",
            "-f", "concat", "-i", str(listing), "-an", "-vf",
            f"pad=ceil(iw/2)*2:ceil(ih/2)*2:0:0:black,setsar=1,fps={args.fps}",
            "-r", str(args.fps), "-fps_mode", "cfr", "-t", f"{cursor:.6f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-metadata", "title=FindBack — actual Mattermost screen-captured walkthrough",
            "-metadata", "comment=Edited actual browser captures; time gaps compressed. Fictional people and handovers. No generated UI or added graphics.",
            str(output)], check=True)
    encoded = probe(output)
    stream = encoded["streams"][0]
    if stream["codec_name"] != "h264" or (stream["width"], stream["height"]) != (width + width % 2, height + height % 2):
        raise ValueError("Unexpected output codec or dimensions.")
    if abs(float(encoded["format"]["duration"]) - cursor) > 2 / args.fps:
        raise ValueError("Output duration differs from the recorded edit timeline.")
    # Check the originals again after encoding, and preserve the last screenshot
    # byte-for-byte as a separate inspection artifact.
    if any(digest(row["path"]) != row["sha256"] for row in captured):
        raise ValueError("An original capture changed during encoding.")
    still = output.with_name("mattermost-chat.jpg")
    shutil.copyfile(selected[-1]["path"], still)
    report = {
        "schema": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "edited actual browser screen-captured walkthrough",
        "disclosure": "Scenes navigated during the real scripted run; cuts and waiting gaps compressed. Fictional participants, objects and handovers. Not a continuous capture of every event. No generated UI, added graphics, text slides or narration.",
        "source_manifest_sha256": digest(source), "source_frame_count": len(captured),
        "retained_frame_count": len(selected), "source_dimensions": [width, height],
        "output_dimensions": [stream["width"], stream["height"]],
        "pixel_transform": "One black padding pixel on right/bottom if needed for H.264; original browser pixels are not rescaled or cropped. Lossy H.264 compression only.",
        "timing_rule": {"gap_threshold_seconds": args.gap_threshold,
                        "long_gap_output_seconds": args.gap_duration,
                        "final_frame_seconds": args.last_duration, "fps": args.fps,
                        "other_intervals": "Original timestamp cadence, quantized to output frames."},
        "source_elapsed_seconds": (captured[-1]["at"] - captured[0]["at"]) / 1000,
        "edited_duration_seconds": float(encoded["format"]["duration"]),
        "video_file": output.name, "video_sha256": digest(output), "ffprobe": encoded,
        "still_file": still.name, "still_sha256": digest(still),
        "omitted_frames": [{"file": name, "reason": reason} for name, reason in omissions.items()],
        "source_frames": [{key: value for key, value in row.items() if key != "path"} for row in captured],
        "timeline": timeline,
    }
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--omit", action="append", default=[], metavar="FILENAME=REASON")
    parser.add_argument("--gap-threshold", type=float, default=2)
    parser.add_argument("--gap-duration", type=float, default=.7)
    parser.add_argument("--last-duration", type=float, default=.7)
    parser.add_argument("--fps", type=int, default=24)
    args = parser.parse_args()
    if min(args.gap_threshold, args.gap_duration, args.last_duration, args.fps) <= 0:
        parser.error("Timing values and fps must be positive.")
    result = encode(args)
    print(json.dumps({key: result[key] for key in (
        "source_frame_count", "retained_frame_count", "output_dimensions", "edited_duration_seconds", "video_sha256")}, indent=2))


if __name__ == "__main__":
    main()
