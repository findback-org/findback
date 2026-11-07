#!/usr/bin/env python3
"""Render an honest text playback from captured FindBack evidence, entirely offline.

Python standard library orchestrates ffmpeg/ffprobe and optional macOS `say`.
If ffmpeg lacks drawtext, the macOS system AppKit renderer is used through Swift;
no Python image package, web assets, models or network calls are required.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap

ROOT = Path(__file__).resolve().parents[1]
DISCLOSURE = "RECORDED PLAYBACK of captured live Switch run — scripted fictional inputs"
FOOTER = "No physical handover • Text reconstruction, not a screen recording"


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, capture_output=True, text=True, **kwargs)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot_database(source: Path, destination: Path):
    """SQLite backup sees a consistent committed database, including a live WAL."""
    source = source.resolve()
    try:
        incoming = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        incoming.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except sqlite3.OperationalError:
        if "incoming" in locals():
            incoming.close()
        # Some restricted macOS environments cannot create WAL shared-memory files.
        # Immutable is safe only when there are no uncheckpointed WAL contents.
        wal = Path(str(source) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise RuntimeError("Read-only SQLite backup was denied while a WAL exists. Run with read access to SQLite's shared-memory files; do not use an immutable stale copy.")
        incoming = sqlite3.connect(source.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        incoming.execute("PRAGMA query_only=ON")
        with sqlite3.connect(destination) as copied:
            incoming.backup(copied)
    finally:
        incoming.close()


def read_sources(database: Path, live_path: Path):
    live = json.loads(live_path.read_text())
    if not live.get("passed") or live.get("behavior", {}).get("human_input") != "scripted authenticated fictional roleplay":
        raise RuntimeError("Expected the successful, explicitly scripted fictional live-run record.")
    db = sqlite3.connect(database)
    db.row_factory = sqlite3.Row
    proposals = {r["job_id"]: json.loads(r["result_json"]) for r in db.execute("SELECT * FROM model_proposals")}
    outcomes = {r["source_event_id"]: json.loads(r["result_json"]) for r in db.execute("SELECT * FROM processed_events")}
    messages = {r["id"]: r["body"] for r in db.execute("SELECT id,body FROM runtime_messages")}
    items = {r["id"]: {"coarse": json.loads(r["coarse_json"]), "private": json.loads(r["private_facts_json"])} for r in db.execute("SELECT * FROM items")}
    receipt = db.execute("SELECT * FROM returns WHERE receipt_id=?", (live["receipt_id"],)).fetchone()
    db.close()
    if receipt is None or not receipt["claimant_receipt_event_id"]:
        raise RuntimeError("The captured final receipt is missing from the database.")
    for event in live["events"]:
        if event["result_code"] != "PROPOSAL_OR_READ":
            actual = outcomes.get(event["switch_event_id"])
            if not actual or actual["code"] != event["result_code"]:
                raise RuntimeError("Live-run result does not match the durable domain record: " + event["label"])
    return live, proposals, outcomes, messages, items, dict(receipt)


def make_scenes(live, proposals, outcomes, messages, items, receipt):
    events = {e["label"]: e for e in live["events"]}
    def event(label):
        if label not in events:
            raise RuntimeError("Captured event is missing: " + label)
        return events[label]
    def proposal(label, suffix):
        e = event(label)
        return proposals[e["switch_event_id"] + suffix]
    def outcome(label):
        return outcomes[event(label)["switch_event_id"]]
    def source(label):
        return "Source: " + event(label)["switch_event_id"]
    def command_text(label, verb):
        text = event(label)["command"]
        prefix = "findback " + verb + " "
        if not text.startswith(prefix):
            raise RuntimeError("Unexpected captured command shape")
        return text[len(prefix):]
    def sentence(text):
        return text.split(". ", 1)[0].rstrip(".") + "."
    initial = proposal("Priya report / live Intake model", ":draft")
    broad = proposal("Priya confirms exact draft / live Matcher model", ":matcher")
    alex = proposal("Alex confirms / insufficient evidence comparison", ":matcher")
    evidence_label = "Priya independent assertion / refreshed live Matcher model"
    detailed = proposal(evidence_label, ":matcher")
    question_source = messages[event("Priya report / live Intake model")["switch_event_id"] + ":draft-display"]
    question_match = re.search(r"What marks, contents, or accessories[^\n]+", question_source)
    if question_match is None:
        raise RuntimeError("The actual non-leading question is missing")
    question = question_match.group(0)
    evidence = command_text(evidence_label, "evidence")
    approved = event("Mira approves exact evidence and reserves")
    approval = outcome("Mira approves exact evidence and reserves")
    # Exact text from the authenticated fictional staff command, not an invented quote.
    reason = approved["command"].split(" ", 5)[5]
    forbidden = outcome("Claimant authorization attempt")
    stale = outcome("Old review is refused")
    early = outcome("Premature receipt refused")
    handed = outcome("Enrolled custodian records fictional handover")
    returned = outcome("Enrolled claimant confirms fictional receipt")
    capture = datetime.fromisoformat(live["recorded_at"]).strftime("%d %b %Y · %H:%M UTC")
    scenes = [
      dict(duration=10, stage="CAPTURED EVIDENCE", title="FindBack", body="A lost possession.\nA possible match.\nA custodian's decision.\nA confirmed return record.\n\n" + DISCLOSURE + ".\nNo physical object moved.", source="Captured full run: " + capture, narration="Recorded playback of a captured live Switch run. Scripted fictional inputs. No physical handover."),
      dict(duration=12, stage="01 / FICTIONAL INVENTORY", title="Two similar cases", body="I-001  •  " + items["I-001"]["coarse"]["color"].capitalize() + " headphone case\n" + items["I-001"]["coarse"]["found_place"] + "\n\nI-002  •  " + items["I-002"]["coarse"]["color"].capitalize() + " headphone case\n" + items["I-002"]["coarse"]["found_place"] + "\n\nThe broad description fits both.", source="Source: captured database item records I-001 and I-002", narration="Fictional inventory. Two black headphone cases, found at different library locations."),
      dict(duration=16, stage="02 / PRIYA'S CASE ROOM", title="A report, then a question", body=initial["description"] + "\n\nLive Intake output:\ncategory: " + initial["category"] + "  •  color: " + initial["color"] + "\nexact date/time: unknown\n\n" + question, source=source("Priya report / live Intake model"), narration=initial["description"] + " " + question),
      dict(duration=14, stage="03 / STAFF ROOM · LIVE MATCHER", title="Ambiguity stays visible", body=broad["summary"] + "\n\nRecorded recommendation:\n" + broad["recommendation"], source=source("Priya confirms exact draft / live Matcher model"), narration=sentence(broad["summary"])),
      dict(duration=12, stage="04 / ALEX'S SEPARATE CASE", title="More information needed", body=alex["summary"] + "\n\nAlex's captured final status: " + live["alex_status"] + "\nNo reservation for Alex was recorded.", source=source("Alex confirms / insufficient evidence comparison"), narration=sentence(alex["summary"]) + " More information is needed."),
      dict(duration=17, stage="05 / CLAIMANT ASSERTION → STAFF REVIEW", title="Claimant details enter the record", body=evidence + "\n\nStaff-only observations for I-001:\n" + "\n".join("• " + fact for fact in items["I-001"]["private"]) + "\n\nThe claimant assertion and staff observations remain separate sources.", source=source(evidence_label), narration=evidence),
      dict(duration=15, stage="06 / STAFF ROOM · REFRESHED MATCHER", title="A possible match, not ownership", body=detailed["summary"], source=source(evidence_label), narration=sentence(detailed["summary"])),
      dict(duration=14, stage="07 / AUTHORITY AND REVISION CHECKS", title="Two requests refused", body=forbidden["code"] + "\n" + forbidden["message"] + "\n\n" + stale["code"] + "\n" + stale["message"], source="Sources: captured unauthorized-approval and old-review outcomes", narration=forbidden["message"] + " " + stale["message"]),
      dict(duration=16, stage="08 / ENROLLED CUSTODIAN DECISION", title="Reserved is not returned", body="Recorded staff reason:\n" + reason + "\n\n" + approval["code"] + "  •  I-001\n" + approval["data"]["reservation_id"] + "\nCollection was arranged privately; closure was still pending.", source=source("Mira approves exact evidence and reserves"), narration=reason),
      dict(duration=18, stage="09 / TWO CONFIRMATIONS", title="Handover, then receipt", body="Premature receipt: " + early["code"] + "\nStaff records: " + handed["code"] + "\nClaimant confirms: " + returned["code"] + "\n\nReceipt: " + live["receipt_id"] + "\n\nThese were authenticated fictional roleplay messages.\nNo physical handover occurred.", source=source("Enrolled claimant confirms fictional receipt"), narration=early["message"] + " The captured fictional staff handover was followed by claimant receipt confirmation."),
      dict(duration=8, stage="CAPTURED FULL-RUN OUTCOME", title="One return record", body=str(live["totals"]["claims"]) + " claims  •  " + str(live["totals"]["model_proposals"]) + " live model proposals\n" + str(live["totals"]["returns"]) + " recorded return\nPriya: " + live["priya_status"] + "  •  Alex: " + live["alex_status"] + "\n\nInspect evidence/live-run.json and the source generator.\nLocal Switch + Mattermost + signed-in Codex.", source="Recorded full-run snapshot: " + capture, narration="Two claims. Five live model proposals. One recorded return. Alex still needs information."),
    ]
    for index, scene in enumerate(scenes, 1):
        scene.update(index=index, total=len(scenes), disclosure=DISCLOSURE, footer=FOOTER)
    return scenes


SWIFT_RENDERER = r'''
import AppKit
import Foundation
let manifestURL = URL(fileURLWithPath: CommandLine.arguments[1])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[2], isDirectory: true)
let scenes = try JSONSerialization.jsonObject(with: Data(contentsOf: manifestURL)) as! [[String: Any]]
let W: CGFloat = 1920, H: CGFloat = 1080
func color(_ r: CGFloat, _ g: CGFloat, _ b: CGFloat) -> NSColor { NSColor(srgbRed:r, green:g, blue:b, alpha:1) }
let ink = color(0.08,0.13,0.14), teal = color(0.10,0.39,0.34), muted = color(0.34,0.39,0.39)
func text(_ value: String, _ x: CGFloat, _ top: CGFloat, _ width: CGFloat, _ height: CGFloat, _ size: CGFloat, _ bold: Bool, _ fg: NSColor) {
    let style = NSMutableParagraphStyle()
    style.lineBreakMode = .byWordWrapping
    style.lineSpacing = size * 0.12
    let font = NSFont(name: bold ? "Arial-BoldMT" : "ArialMT", size:size) ?? NSFont.systemFont(ofSize:size, weight:bold ? .bold : .regular)
    let string = NSAttributedString(string:value, attributes:[.font:font,.foregroundColor:fg,.paragraphStyle:style])
    string.draw(with:NSRect(x:x,y:H-top-height,width:width,height:height),options:[.usesLineFragmentOrigin,.usesFontLeading])
}
for scene in scenes {
    let bitmap = NSBitmapImageRep(bitmapDataPlanes:nil,pixelsWide:Int(W),pixelsHigh:Int(H),bitsPerSample:8,samplesPerPixel:4,hasAlpha:true,isPlanar:false,colorSpaceName:.deviceRGB,bytesPerRow:0,bitsPerPixel:0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep:bitmap)
    color(0.965,0.957,0.933).setFill(); NSBezierPath(rect:NSRect(x:0,y:0,width:W,height:H)).fill()
    text("FINDBACK",96,62,800,50,30,true,ink)
    text("RECORDED PLAYBACK",1310,67,510,40,24,true,teal)
    text(scene["stage"] as! String,96,162,1700,48,28,true,teal)
    text(scene["title"] as! String,92,220,1730,100,68,true,ink)
    text(scene["body"] as! String,96,340,1710,530,44,false,ink)
    text(scene["source"] as! String,96,910,1728,36,22,false,muted)
    color(0.83,0.85,0.81).setFill(); NSBezierPath(rect:NSRect(x:96,y:H-970,width:1728,height:7)).fill()
    teal.setFill(); let fraction = CGFloat(scene["index"] as! Int) / CGFloat(scene["total"] as! Int)
    NSBezierPath(rect:NSRect(x:96,y:H-970,width:1728*fraction,height:7)).fill()
    text(scene["disclosure"] as! String,96,994,1728,36,24,true,ink)
    text(scene["footer"] as! String,96,1031,1728,35,23,false,muted)
    NSGraphicsContext.restoreGraphicsState()
    let png = bitmap.representation(using:.png,properties:[:])!
    try png.write(to:outputURL.appendingPathComponent(String(format:"scene-%02d.png",scene["index"] as! Int)))
}
'''


def filter_path(path):
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def render_slides(scenes, work, ffmpeg):
    manifest = work / "scenes.json"
    manifest.write_text(json.dumps(scenes, ensure_ascii=False, indent=2))
    filters = run([ffmpeg, "-hide_banner", "-filters"]).stdout
    font = next((p for p in (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ) if p.exists()), None)
    if " drawtext " in filters and font:
        for scene in scenes:
            parts = []
            fields = [
                ("FINDBACK",96,62,30,"0x182325",70),
                (scene["stage"],96,162,28,"0x196456",80),
                (scene["title"],96,220,68,"0x182325",42),
                (scene["body"],96,340,44,"0x182325",68),
                (scene["source"],96,910,22,"0x586362",118),
                (DISCLOSURE,96,994,24,"0x182325",120),
                (FOOTER,96,1031,23,"0x586362",120),
            ]
            for n, (text,x,y,size,fg,width) in enumerate(fields):
                path = work / f"text-{scene['index']:02d}-{n}.txt"
                path.write_text("\n".join(textwrap.fill(line,width,break_long_words=False,break_on_hyphens=False) for line in text.split("\n")))
                parts.append(f"drawtext=fontfile='{filter_path(font)}':textfile='{filter_path(path)}':expansion=none:fontsize={size}:fontcolor={fg}:x={x}:y={y}:line_spacing=5")
            parts += ["drawbox=x=96:y=963:w=1728:h=7:color=0xd3d9cf:t=fill",f"drawbox=x=96:y=963:w={1728*scene['index']/len(scenes)}:h=7:color=0x196456:t=fill"]
            run([ffmpeg,"-hide_banner","-loglevel","error","-f","lavfi","-i","color=c=0xf6f4ee:s=1920x1080", "-vf", ",".join(parts),"-frames:v","1","-y",work/f"scene-{scene['index']:02d}.png"])
        return "ffmpeg drawtext", str(font)
    swift = shutil.which("swiftc")
    if sys.platform != "darwin" or not swift:
        raise RuntimeError("ffmpeg drawtext is unavailable; macOS Swift/AppKit fallback is unavailable too.")
    helper = work / "render.swift"
    helper.write_text(SWIFT_RENDERER)
    compiler_cache = work / "swift-cache"
    compiler_cache.mkdir()
    executable = work / "render-slides"
    run([swift,"-module-cache-path",compiler_cache,helper,"-o",executable])
    run([executable,manifest,work])
    return "macOS system AppKit via Swift (ffmpeg drawtext absent)", str(font or "macOS system Arial")


def probe(path, ffprobe):
    return json.loads(run([ffprobe,"-v","error","-show_format","-show_streams","-of","json",path]).stdout)


def encode(scenes, work, output, ffmpeg, ffprobe, *, silent=False, voice="Samantha"):
    say = None if silent else shutil.which("say")
    clips = []
    narration_used = bool(say)
    for scene in scenes:
        index, duration = scene["index"], scene["duration"]
        png, audio = work/f"scene-{index:02d}.png", work/f"voice-{index:02d}.aiff"
        if say:
            words = work/f"voice-{index:02d}.txt"
            words.write_text(scene["narration"])
            rate = 180
            while True:
                run([say,"-v",voice,"-r",rate,"-f",words,"-o",audio])
                audio_info = probe(audio,ffprobe)
                length = float(audio_info["format"].get("duration") or 0)
                if length <= 0:
                    if index > 1:
                        raise RuntimeError("Offline speech failed partway through rendering; regenerate with --silent.")
                    print("Offline speech returned no samples; producing a silent text film.", flush=True)
                    say = None
                    narration_used = False
                    break
                if length <= duration - 0.7:
                    break
                rate = round(rate * length / (duration - 1)) + 8
                if rate > 280:
                    raise RuntimeError("Narration cannot fit without becoming too fast; regenerate with --silent.")
        clip = work/f"clip-{index:02d}.mp4"
        args = [ffmpeg,"-hide_banner","-loglevel","error","-loop","1","-framerate","24","-i",png]
        args += ["-i",audio] if say else ["-f","lavfi","-i","anullsrc=r=48000:cl=stereo"]
        args += ["-t",str(duration),"-vf","format=yuv420p","-af","apad,aresample=48000", "-c:v","libx264","-preset","veryfast","-crf","20","-r","24","-c:a","aac","-b:a","128k","-ac","2","-movflags","+faststart","-y",clip]
        run(args)
        clips.append(clip)
        print(f"Rendered scene {index}/{len(scenes)}", flush=True)
    concat = work/"concat.txt"
    concat.write_text("\n".join("file '" + str(p).replace("'", "'\\''") + "'" for p in clips))
    run([ffmpeg,"-hide_banner","-loglevel","error","-f","concat","-safe","0","-i",concat,"-c","copy","-movflags","+faststart","-y",output])
    return narration_used


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db",type=Path,default=ROOT/".findback/findback.db")
    parser.add_argument("--live-run",type=Path,default=ROOT/"evidence/live-run.json")
    parser.add_argument("--output",type=Path,default=ROOT/"evidence/findback-demo.mp4")
    parser.add_argument("--silent",action="store_true",help="Use readable text with a silent audio track, without macOS speech")
    parser.add_argument("--voice",default="Samantha")
    parser.add_argument("--keep-work",action="store_true",help="Retain private intermediate slides for visual review")
    args = parser.parse_args()
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SystemExit("ffmpeg and ffprobe must already be installed; this script installs/downloads nothing.")
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".findback-video-",dir=args.output.parent))
    work.chmod(0o700)
    try:
        snapshot = work/"source-snapshot.db"
        snapshot_database(args.db,snapshot)
        live, proposals, outcomes, messages, items, receipt = read_sources(snapshot,args.live_run)
        scenes = make_scenes(live,proposals,outcomes,messages,items,receipt)
        backend,font = render_slides(scenes,work,ffmpeg)
        audio = encode(scenes,work,args.output,ffmpeg,ffprobe,silent=args.silent,voice=args.voice)
        info = probe(args.output,ffprobe)
        seconds = float(info["format"]["duration"])
        if not 140 <= seconds <= 160 or seconds >= 180:
            raise RuntimeError(f"Unexpected duration: {seconds:.3f} seconds")
        manifest = {
            "artifact":"recorded text playback, not a UI/screen recording",
            "disclosure":DISCLOSURE,"physical_handover":"none; scripted fictional roleplay only",
            "source_capture_time":live["recorded_at"],"duration_seconds":seconds,
            "width":1920,"height":1080,"fps":24,"render_backend":backend,"font":font,
            "audio":"offline macOS say ("+args.voice+")" if audio else "silent text film",
            "source_live_run_sha256":digest(args.live_run),"consistent_database_snapshot_sha256":digest(snapshot),
            "video_sha256":digest(args.output),"source_generator_sha256":digest(__file__),
            "verified_streams":[{k:stream[k] for k in ("codec_name","codec_type","width","height","r_frame_rate") if k in stream} for stream in info["streams"]],
            "scenes":scenes,
            "totals_scope":"Only the captured full-run totals from evidence/live-run.json; later diagnostics are excluded.",
            "runtime_mutations":False,"network_calls":False,"new_model_calls":False,
            "official_video_limit":"Not verified; under three minutes is this project's conservative target.",
        }
        args.output.with_name("findback-demo-manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
        print(json.dumps({"output":str(args.output),"duration_seconds":seconds,"backend":backend,"narrated":audio,"bytes":args.output.stat().st_size,"private_work":str(work) if args.keep_work else None},indent=2))
    finally:
        if not args.keep_work:
            shutil.rmtree(work,ignore_errors=True)


if __name__ == "__main__":
    main()
