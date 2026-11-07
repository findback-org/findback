"""Authorized American male narration; separate outputs, native pace, expressive arc."""
from pathlib import Path
import json
import generate_narration as base

OUT = Path(__file__).resolve().parents[1] / 'demo/broll-male'
VOICE_ID = 'iP95p4xoKVk53GoZ742B'
VOICE_NAME = 'Chris - Charming, Down-to-Earth'


def performance(paragraphs):
    return [
        '[concerned] ' + paragraphs[0].replace('Somewhere nearby,', '[thoughtful] Somewhere nearby,'),
        '[conversational] ' + paragraphs[1],
        '[frustrated] ' + paragraphs[2].replace('Someone still has to', '[uncertain] Someone still has to'),
        '[confident] ' + paragraphs[3],
        '[patient] ' + paragraphs[4].replace('Then it asks', '[curious] Then it asks'),
        '[deliberate] ' + paragraphs[5].replace('Mira checks the details', '[confident] Mira checks the details'),
        '[relieved] ' + paragraphs[6],
        '[warm] ' + paragraphs[7].replace('Switch connects', '[confident] Switch connects'),
    ]


def configure(model):
    paragraphs=[value.strip() for value in (OUT/'narration-script.txt').read_text().split('\n\n') if value.strip()]
    base.OUT=OUT; base.VOICE_ID=VOICE_ID; base.VOICE_NAME=VOICE_NAME; base.MODEL=model
    base.DIRECTIONS=['concerned → thoughtful','conversational','frustrated → uncertain','confident',
                     'patient → curious','deliberate → confident','relieved','warm → confident']
    base.SPOKEN_TEXTS=performance(paragraphs)
    base.VOICE_SETTINGS={'stability':0.5,'similarity_boost':0.75,'use_speaker_boost':True}
    return paragraphs


if __name__=='__main__':
    selection=json.loads((OUT/'voice-selection.json').read_text())
    configure(selection['model_id'])
    base.main()
    manifest=json.loads((OUT/'narration-manifest.json').read_text())
    manifest['voice_metadata']={key:selection[key] for key in ['accent','gender','use_case']}
    manifest['voice_selection_file']='voice-selection.json'
    manifest['unavailable_requested_voice']=selection.get('unavailable_requested_voice')
    base.write_json(OUT/'narration-manifest.json',manifest)
