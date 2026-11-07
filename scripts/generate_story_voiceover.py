"""Generate one root-approved continuous story take through official ElevenLabs.

Existing matching output is reused without an API call. Mismatched existing
output is preserved and refused: choose a new directory for a different take.
The private key is read into memory and never included in logs or artifacts.
"""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import quote
import httpx


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',type=Path,default=Path('demo/story-voiceover'))
    parser.add_argument('--key-file',type=Path,default=Path('.findback/elevenlabs.key'))
    args=parser.parse_args();out=args.output_dir.resolve();plan=json.loads((out/'story-plan.json').read_text())
    if plan.get('status')!='ROOT_SCRIPT_APPROVED' or not plan.get('tts_generation_permitted'):
        raise SystemExit('An internally approved canonical script is required before generation.')
    canonical=(out/'narration-script.txt').read_text();sha=hashlib.sha256(canonical.encode()).hexdigest()
    if sha!=plan['script_sha256']:raise SystemExit('Canonical text changed after approval; no API call made.')
    text=(out/'performance-input.txt').read_text().strip()
    normalize=lambda value:re.sub(r'\s+',' ',value).strip()
    if normalize(re.sub(r'\[[^\]]*\]','',text))!=normalize(canonical):
        raise SystemExit('Performance input changes spoken words; no API call made.')
    if len(text)>=5000:raise SystemExit('The approved performance exceeds the v3 text limit.')
    existing=out/'generation-metadata.json'
    if (out/'narration-original.mp3').exists():
        if existing.exists() and json.loads(existing.read_text()).get('script_sha256')==sha:
            print('Existing continuous take reused; no API call made.');return
        raise SystemExit('Existing audio preserved. Use a separate directory for a different approved take.')
    if args.key_file.stat().st_mode&0o077:raise SystemExit('Private key file must be mode0600.')
    key=args.key_file.read_text().strip();headers={'xi-api-key':key}
    with httpx.Client(timeout=240,follow_redirects=False) as client:
        quota=client.get('https://api.elevenlabs.io/v1/user/subscription',headers=headers)
        if quota.status_code!=200:raise SystemExit('Could not verify allowance; no generation or purchase attempted.')
        q=quota.json();remaining=q['character_limit']-q['character_count']
        if remaining<len(text):raise SystemExit('Insufficient allowance; no generation or purchase attempted.')
        started=time.time()
        response=client.post('https://api.elevenlabs.io/v1/text-to-speech/'+quote(plan['voice_id'],safe='')+'/with-timestamps',
            params={'output_format':'mp3_44100_128'},headers={**headers,'Content-Type':'application/json'},
            json={'text':text,'model_id':'eleven_v3','language_code':'en',
                  'voice_settings':{'stability':.5,'similarity_boost':.75,'use_speaker_boost':True},
                  'seed':8794,'apply_text_normalization':'auto'})
        if response.status_code!=200:
            code='unknown'
            try:code=response.json().get('detail',{}).get('status','unknown')
            except (ValueError,AttributeError):pass
            raise SystemExit(f'Generation refused: HTTP{response.status_code}, code{code}. No alternate voice or purchase attempted.')
        result=response.json()
    (out/'narration-original.mp3').write_bytes(base64.b64decode(result['audio_base64']))
    (out/'global-alignment.json').write_text(json.dumps({'alignment':result.get('alignment'),
        'normalized_alignment':result.get('normalized_alignment')},ensure_ascii=False)+'\n')
    metadata={'provider':'ElevenLabs','official_api':'https://api.elevenlabs.io','voice_id':plan['voice_id'],
              'voice_name':plan['voice_name'],'model_id':'eleven_v3','native_tempo_factor':1.0,
              'single_continuous_request':True,'input_characters':len(text),'generation_seconds':round(time.time()-started,2),
              'script_sha256':sha,'ai_generated_narration':True}
    existing.write_text(json.dumps(metadata,indent=2)+'\n')
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(out/'narration-original.mp3'),'-ar','48000','-ac','1',
                    '-c:a','pcm_s16le',str(out/'narration-raw.wav')],check=True)
    print('Continuous take saved; run prepare_story_audio.py for local edit references and mastering.')


if __name__=='__main__':main()
