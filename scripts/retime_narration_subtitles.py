"""Write subtitle sidecars against the exact final chapter PCM timeline.

No API calls or MP4 changes. Original provider sentence boundaries are scaled
by each measured output/input chapter duration, checked against the final voice
tempo, then placed at normalized chapter starts and clipped to its actual end.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import textwrap

DESCRIPTION = (
    'FindBack follows fictional campus participants and objects through a real recorded Mattermost/Switch workflow, with AI-generated English narration. '
    'Alex’s case ends approved and reserved for collection, before physical handover or receipt.\n'
)


def timestamp_ms(value: int) -> str:
    hours, remaining = divmod(value, 3600000)
    minutes, remaining = divmod(remaining, 60000)
    seconds, milliseconds = divmod(remaining, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}'


def retime(source: dict, final: dict) -> list[dict]:
    tempo = float(final['voice_tempo_factor'])
    if not .5 <= tempo <= 2:
        raise ValueError('Invalid final voice tempo factor.')
    original = {int(value['chapter']): value for value in source['chapters']}
    cues = []
    previous_end = 0
    for chapter in final['chapters']:
        index = int(chapter['chapter'])
        start = float(chapter['audio_start_seconds'])
        end = start + float(chapter['audio_seconds'])
        source_duration = float(original[index]['duration_seconds'])
        if source_duration <= 0:
            raise ValueError('Invalid source chapter duration.')
        # FFmpeg atempo has tiny duration/rounding differences from 1/tempo.
        # Use the actual normalized PCM chapter length supplied by the encoder.
        scale = float(chapter['audio_seconds']) / source_duration
        if abs(scale * tempo - 1) > .05:
            raise ValueError('Measured chapter duration disagrees with the stated tempo.')
        for sentence in original[index]['sentences']:
            a = max(start, min(start + float(sentence['start']) * scale, end))
            b = max(a, min(start + float(sentence['end']) * scale, end))
            a_ms, b_ms = round(a * 1000), round(b * 1000)
            if a_ms < previous_end or b_ms <= a_ms:
                raise ValueError('Subtitle cue would overlap or be empty after retiming.')
            cues.append({'chapter': index, 'start_ms': a_ms, 'end_ms': b_ms, 'text': sentence['text']})
            previous_end = b_ms
    if previous_end > round(float(final['audio_seconds']) * 1000):
        raise ValueError('Subtitle exceeds actual final audio duration.')
    return cues


def write_cut(source_path: Path, directory: Path) -> dict:
    source = json.loads(source_path.read_text())
    final_path = directory / 'narrated-manifest.json'
    final = json.loads(final_path.read_text())
    cues = retime(source, final)
    lines = []
    for number, cue in enumerate(cues, 1):
        lines.extend([str(number), timestamp_ms(cue['start_ms']) + ' --> ' + timestamp_ms(cue['end_ms']),
                      '\n'.join(textwrap.wrap(cue['text'], width=55, break_long_words=False, break_on_hyphens=False)), ''])
    (directory / 'narration.srt').write_text('\n'.join(lines), encoding='utf-8')
    (directory / 'demo-description.txt').write_text(DESCRIPTION, encoding='utf-8')
    report = {'subtitle_file': 'narration.srt', 'source_timing_file': str(source_path.resolve()),
              'final_timing_file': final_path.name, 'voice_tempo_factor': final['voice_tempo_factor'],
              'audio_seconds': final['audio_seconds'], 'cue_count': len(cues),
              'first_cue_seconds': cues[0]['start_ms'] / 1000,
              'last_cue_seconds': cues[-1]['end_ms'] / 1000,
              'method': 'Provider sentence cues scaled by measured output/input chapter PCM duration (checked against voice_tempo_factor), shifted to exact normalized chapter starts and clamped to actual chapter audio end. No MP4 changes.',
              'cues': cues}
    (directory / 'subtitle-timing.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
    return {key: value for key, value in report.items() if key != 'cues'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cuts', nargs='*', type=Path)
    parser.add_argument('--source', type=Path, default=Path('demo/broll/narration-manifest.json'))
    args = parser.parse_args()
    for directory in args.cuts or [Path('demo/narrated-paced'), Path('demo/narrated-natural')]:
        print(json.dumps({'directory': str(directory), **write_cut(args.source, directory)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
