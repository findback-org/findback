# Actual Mattermost screen-captured walkthrough

[mattermost-demo.mp4](mattermost-demo.mp4) is a silent, **70.917-second edited capture of the actual local Mattermost browser UI**, not a generated UI or text slideshow. Scenes were navigated during the live scripted campus run. The participants, objects and handovers are fictional; the underlying chat messages, Switch transport, model calls, checks and stored receipts ran through FindBack. This is not a continuous recording of every event or evidence of physical returns.

The original capture contains 108 JPEG screenshots spanning 577.152 seconds. The video retains 103 in their original order. The opening community frame was omitted as requested, and four visually inspected Mattermost loading screens were omitted: frames 1, 2, 80 and 81. Every gap greater than two seconds is compressed to 0.7 seconds; other screenshot intervals preserve their original cadence, quantized to 24 fps. The final frame is held for 0.7 seconds. There are cuts, with no interpolated motion, narration, overlays, added text or reconstructed chat.

The source screenshots are 649 × 769 pixels. The H.264 video is 650 × 770, with one black padding pixel at the right and bottom for even dimensions. Browser pixels are neither cropped nor rescaled; normal lossy video compression applies. All source JPEG hashes were checked again after encoding. [capture-manifest.json](capture-manifest.json) records their timestamps and hashes, explicit omissions, output timing, video hash and ffprobe results. Original captures remain in `.findback/live-campus/chat-capture/`.

[mattermost-chat.jpg](mattermost-chat.jpg) is the last source screenshot copied byte for byte. It shows the actual final summary in the campus room: three simulated recoveries completed, with Alex's claim still open.

Verification: ffprobe reports H.264, 24 fps, 650 × 770, 1,702 frames and 70.916667 seconds. The first and final encoded frames were decoded and visually compared with their original browser screenshots; the text, layout and content match. The original capture contact sheet was also inspected for loading screens.

To reproduce from the preserved captures, run from `app/`:

```sh
python3 scripts/encode_chat_capture.py \
  --frames .findback/live-campus/chat-capture/frames.json \
  --output evidence/live-campus/mattermost-demo.mp4 \
  --manifest evidence/live-campus/capture-manifest.json \
  --omit 'frame-000000.jpg=Opening community frame omitted as requested; subsequent actual room walkthrough retained.' \
  --omit 'frame-000001.jpg=Visually inspected Mattermost loading screen.' \
  --omit 'frame-000002.jpg=Visually inspected Mattermost loading screen.' \
  --omit 'frame-000080.jpg=Visually inspected Mattermost loading screen during navigation.' \
  --omit 'frame-000081.jpg=Visually inspected Mattermost loading screen during navigation.'
```

The encoder uses only Python's standard library, ffmpeg and ffprobe.
