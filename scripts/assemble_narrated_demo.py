#!/usr/bin/env python3
"""Align eight actual UI captures to eight authorized synthetic voice chapters.

Uses standard-library PCM WAV handling and ffmpeg/ffprobe. Source images and
the separate native/silent B-roll artifacts are never modified.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
import wave


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-4000:])
    return result


def probe(path):
    return json.loads(run(["ffprobe", "-v", "error", "-show_entries",
        "stream=codec_name,width,height,avg_frame_rate,nb_frames:format=duration,size",
        "-of", "json", str(path)]).stdout)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def concat_videos(paths, output):
    with tempfile.TemporaryDirectory(prefix="findback-narrated-") as temporary:
        listing = Path(temporary) / "video.ffconcat"
        listing.write_text("ffconcat version 1.0\n" + "".join(
            "file '" + str(path.resolve()).replace("'", "'\\''") + "'\n" for path in paths))
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-safe", "0",
             "-f", "concat", "-i", str(listing), "-an", "-c:v", "copy",
             "-movflags", "+faststart", str(output)])


def encode_cue_holds(plan, args, broll_path, source_audio, duration, frames, output):
    """Hold verified actual browser frames and cut on real spoken phrases."""
    alignment_path = source_audio.with_suffix(".alignment.json")
    alignment = json.loads(alignment_path.read_text())["alignment"]
    characters = "".join(alignment["characters"])
    original_duration = float(probe(source_audio)["format"]["duration"])
    scale = duration / original_duration
    cuts = []
    for index, shot in enumerate(plan):
        image = args.capture_root.resolve() / shot["frame"]
        source_rows = json.loads((image.parent / "frames.json").read_text())
        original = next(row for row in source_rows if row["file"] == image.name)
        source_manifest = broll_path.parent / "native" / image.parent.name / "capture-manifest.json"
        recorded = json.loads(source_manifest.read_text())
        expected = next(row for row in recorded["source_frames"] if row["file"] == image.name)
        if sha256(image) != expected["sha256"]:
            raise ValueError("An original browser frame changed after capture verification.")
        cue = shot.get("cue")
        provider_time = 0.0
        if cue:
            char_index = characters.find(cue)
            if char_index < 0:
                raise ValueError("Spoken cue missing from real character alignment: " + cue)
            provider_time = alignment["character_start_times_seconds"][char_index]
        boundary = 0 if index == 0 else round(provider_time * scale * 24)
        if index and not cuts[-1]["output_frame"] < boundary < frames:
            raise ValueError("Narration visual cues must increase within their chapter.")
        cuts.append({"source_frame": shot["frame"], "source_at_ms": original["at"],
                     "source_sha256": expected["sha256"], "cue": cue,
                     "provider_cue_seconds": provider_time,
                     "output_frame": boundary, "output_seconds": boundary / 24,
                     "_path": image})
    with tempfile.TemporaryDirectory(prefix="findback-cue-holds-") as temporary:
        listing = Path(temporary) / "holds.ffconcat"
        lines = ["ffconcat version 1.0"]
        for index, cut in enumerate(cuts):
            end = cuts[index + 1]["output_frame"] if index + 1 < len(cuts) else frames
            cut["held_seconds"] = (end - cut["output_frame"]) / 24
            quoted = str(cut["_path"]).replace("'", "'\\''")
            lines.extend([f"file '{quoted}'", "option framerate 1000", f"duration {cut['held_seconds']:.6f}"])
        quoted = str(cuts[-1]["_path"]).replace("'", "'\\''")
        lines.extend([f"file '{quoted}'", "option framerate 1000"])
        listing.write_text("\n".join(lines) + "\n")
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-safe", "0",
             "-f", "concat", "-i", str(listing), "-an", "-vf",
             "pad=ceil(iw/2)*2:ceil(ih/2)*2:0:0:black,setsar=1,fps=24,tpad=stop_mode=clone:stop_duration=1",
             "-frames:v", str(frames), "-c:v", "libx264", "-preset", "medium",
             "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)])
    for cut in cuts:
        if sha256(cut.pop("_path")) != cut["source_sha256"]:
            raise ValueError("An original source screenshot changed while encoding.")
    return {"mode": "Narration-cued holds of unchanged actual browser screenshots; hard cuts do not depict live typing or event timing.",
            "character_alignment_sha256": sha256(alignment_path), "cuts": cuts}


def assemble(args):
    root = args.output_root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    broll_path = args.broll_manifest.resolve()
    broll = json.loads(broll_path.read_text())
    cue_plan = json.loads(args.cue_plan.read_text()) if args.cue_plan else {}
    audio_paths = {}
    for item in args.audio:
        number, value = item.split("=", 1)
        number = int(number.removeprefix("scene-"))
        if number not in range(1, 9) or number in audio_paths:
            raise ValueError("Supply each of the eight audio chapters exactly once.")
        audio_paths[number] = Path(value).resolve()
    if set(audio_paths) != set(range(1, 9)):
        raise ValueError("Eight chapter audio files are required.")
    audio_dir, video_dir = root / "audio", root / "chapters"
    audio_dir.mkdir(exist_ok=True)
    video_dir.mkdir(exist_ok=True)
    chapters = []
    total_samples = 0
    frame_boundary = 0
    for number in range(1, 9):
        source_audio = audio_paths[number]
        normalized = audio_dir / f"chapter-{number:02d}.wav"
        audio_command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_audio), "-vn"]
        if args.tempo != 1:
            audio_command.extend(["-af", f"atempo={args.tempo}"])
        audio_command.extend(["-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(normalized)])
        run(audio_command)
        with wave.open(str(normalized), "rb") as stream:
            samples = stream.getnframes()
        duration = samples / 48000
        scene = broll["scenes"][f"scene-{number:02d}"]
        source_video = broll_path.parent / scene.get("presentation_source_file", scene["native_file"])
        native_duration = scene.get("presentation_source_seconds", scene["native_seconds"])
        start_sample = total_samples
        total_samples += samples
        next_boundary = math.ceil(total_samples / 48000 * 24)
        frames = next_boundary - frame_boundary
        target_duration = frames / 24
        if str(number) not in cue_plan and target_duration + 1 / 24 < native_duration:
            raise ValueError(f"Chapter {number} audio ({duration:.3f}s) is shorter than its actual UI scene ({native_duration:.3f}s); choose an explicit visual edit before compiling.")
        chapter_video = video_dir / f"chapter-{number:02d}.mp4"
        visual_edit = None
        if str(number) in cue_plan:
            visual_edit = encode_cue_holds(cue_plan[str(number)], args, broll_path, source_audio,
                                          duration, frames, chapter_video)
        else:
            run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_video),
                 "-an", "-vf", f"tpad=stop_mode=clone:stop_duration={target_duration + 1},fps=24",
                 "-frames:v", str(frames), "-c:v", "libx264", "-preset", "medium",
                 "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(chapter_video)])
        chapters.append({"chapter": number, "label": scene["label"],
            "audio_input_file": source_audio.name, "audio_input_sha256": sha256(source_audio),
            "normalized_audio_file": str(normalized.relative_to(root)),
            "audio_start_seconds": start_sample / 48000, "audio_seconds": duration,
            "video_start_seconds": frame_boundary / 24, "video_seconds": target_duration,
            "native_visual_seconds": native_duration,
            "final_actual_frame_hold_seconds": max(0, target_duration - native_duration),
            "source_video_file": str(source_video.relative_to(broll_path.parent)),
            "source_video_sha256": sha256(source_video),
            "chapter_video_file": str(chapter_video.relative_to(root)),
            "chapter_video_sha256": sha256(chapter_video), "narration_cued_visual_edit": visual_edit})
        frame_boundary = next_boundary
        print(f"Chapter {number}: {duration:.3f}s voice, {target_duration:.3f}s actual-UI video", flush=True)
    duration = total_samples / 48000
    if duration > args.max_seconds:
        raise ValueError(f"Narration is {duration:.3f}s, exceeding the {args.max_seconds}s limit.")
    narration = root / "narration.wav"
    with wave.open(str(narration), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        for row in chapters:
            with wave.open(str(root / row["normalized_audio_file"]), "rb") as stream:
                output.writeframes(stream.readframes(stream.getnframes()))
    silent = root / "voice-aligned-silent.mp4"
    concat_videos([root / row["chapter_video_file"] for row in chapters], silent)
    final = root / "findback-narrated-demo.mp4"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(silent),
         "-i", str(narration), "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", "-shortest", "-metadata",
         "title=FindBack — actual Mattermost demo with synthetic English narration",
         "-metadata", "comment=Actual browser screenshots; cuts, compressed waiting gaps and final-frame holds. Fictional people/objects/handovers. Authorized synthetic voice narration via ElevenLabs.", str(final)])
    info = probe(final)
    if abs(float(info["format"]["duration"]) - duration) > .1:
        raise ValueError("Final video and exact PCM narration durations do not align.")
    report = {"schema": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "kind": "actual browser UI walkthrough with authorized synthetic English narration",
        "disclosure": "Fictional participants, objects and handovers. Real app messages, model calls and Switch transport. Edited screenshot captures with compressed waits and holds of actual browser frames, including hard cuts timed to spoken phrases; not a continuous recording of every event. ElevenLabs synthetic narration; no generated UI or text overlays.",
        "source_broll_manifest_sha256": sha256(broll_path), "audio_seconds": duration,
        "video_file": final.name, "video_sha256": sha256(final), "ffprobe": info,
        "narration_file": narration.name, "narration_sha256": sha256(narration),
        "voice_tempo_factor": args.tempo,
        "alignment": "Exact normalized PCM sample durations; cumulative video boundaries quantized to 24fps. No extra interchapter silence. A tempo factor other than 1 uses pitch-preserving ffmpeg atempo; original TTS files are unchanged.",
        "chapters": chapters}
    if args.tts_manifest:
        tts = json.loads(args.tts_manifest.read_text())
        report["tts_manifest_sha256"] = sha256(args.tts_manifest)
        report["tts_voice"] = {key: tts[key] for key in (
            "provider", "model_id", "voice_name", "voice_id", "ai_generated_narration") if key in tts}
    (root / "narrated-manifest.json").write_text(json.dumps(report, indent=2) + "\n")
    lines = ["# Narrated actual UI demo", "",
        "[Play the narrated demo](findback-narrated-demo.mp4). English narration is an authorized ElevenLabs synthetic voice. The visuals are actual Mattermost browser screenshots of the scripted fictional campus demonstration, with real FindBack/Switch messages and AI calls. There are cuts, compressed waiting gaps and holds of selected actual screenshots; this is not continuous footage of every event. Key visual transitions follow real character-level narration alignment. No UI or message content is generated for the video. The featured case ends at approved collection, before handover and receipt.", "",
        f"Duration: **{float(info['format']['duration']):.3f} seconds**. Each chapter follows its exact PCM narration duration, quantized to 24fps for video. Voice tempo factor: **{args.tempo}** (1 is unchanged; other values use pitch-preserving atempo). No extra interchapter silence. The original TTS files remain unchanged.", "",
        "| Chapter | View | Audio start | Audio length | Final actual-frame hold |", "|---|---|---:|---:|---:|"]
    for row in chapters:
        lines.append(f"| {row['chapter']} | {row['label']} | {row['audio_start_seconds']:.3f}s | {row['audio_seconds']:.3f}s | {row['final_actual_frame_hold_seconds']:.3f}s |")
    lines.extend(["", "`narrated-manifest.json` records source hashes, exact chapter durations and ffprobe checks. The separate native clips and silent 180-second scaffold remain in `../broll/`. `narration.wav` preserves the complete normalized voice track."])
    (root / "README.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({"video": str(final), "seconds": info["format"]["duration"], "sha256": report["video_sha256"]}, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broll-manifest", type=Path, default=Path("demo/broll/broll-manifest.json"))
    parser.add_argument("--output-root", type=Path, default=Path("demo/narrated"))
    parser.add_argument("--capture-root", type=Path, default=Path(".findback/focused-capture"))
    parser.add_argument("--cue-plan", type=Path)
    parser.add_argument("--tts-manifest", type=Path)
    parser.add_argument("--audio", action="append", required=True, metavar="CHAPTER=FILE")
    parser.add_argument("--max-seconds", type=float, default=180)
    parser.add_argument("--tempo", type=float, default=1)
    args = parser.parse_args()
    if not .5 <= args.tempo <= 2:
        parser.error("Tempo must be between 0.5 and 2.")
    assemble(args)


if __name__ == "__main__":
    main()
