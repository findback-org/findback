"""Generate authorized chapter narration using only ElevenLabs' official API.

Reads the private API-key file into memory; never records credentials, response
headers or account data. Existing chapters matching this manifest are reused.
"""
from __future__ import annotations
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading

import httpx

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'demo/broll'
KEY = ROOT / '.findback/elevenlabs.key'
VOICE_ID = 'EXAVITQu4vr4xnSDxMaL'
VOICE_NAME = 'Sarah — Mature, Reassuring, Confident'
MODEL = 'eleven_v3'
LABELS = ['The lost case', 'The usual group post', 'Where the handoff stalls', 'FindBack connects the next step',
          'Alex gives private evidence', 'Mira makes the decision', 'An approved collection', 'The value of Switch']
DIRECTIONS = ['[warm]', '', '[thoughtful]', '[confident]', '[reassuring]', '', '[relieved]', '[confident]']
SPOKEN_TEXTS = None
VOICE_SETTINGS = {'stability':0.5,'similarity_boost':0.75,'style':0.1,'use_speaker_boost':True}


def duration(path):
    r = subprocess.run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],check=True,capture_output=True,text=True)
    return float(r.stdout.strip())


def write_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix='.narration-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream: json.dump(value,stream,indent=2,ensure_ascii=False);stream.write('\n')
        os.replace(temporary,path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def stamp(seconds):
    millis=round(seconds*1000);hours,millis=divmod(millis,3600000);minutes,millis=divmod(millis,60000);secs,millis=divmod(millis,1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}'


def sentence_cues(alignment):
    if not alignment:return []
    chars=alignment['characters'];starts=alignment['character_start_times_seconds'];ends=alignment['character_end_times_seconds']
    # Strip unspoken direction tags without losing the character/time mapping.
    clean=[];inside=False
    for i,char in enumerate(chars):
        if char=='[':inside=True
        if not inside:clean.append((char,starts[i],ends[i]))
        if char==']':inside=False
    text=''.join(x[0] for x in clean);cues=[]
    for match in re.finditer(r'[^.!?]+[.!?]+|[^.!?]+$',text):
        a,b=match.span()
        while a<b and clean[a][0].isspace():a+=1
        while b>a and clean[b-1][0].isspace():b-=1
        if a<b:cues.append({'start':clean[a][1],'end':clean[b-1][2],'text':''.join(x[0] for x in clean[a:b]).strip()})
    return cues


def main():
    if KEY.stat().st_mode & 0o077:raise SystemExit('Private key file permissions must be 0600.')
    key=KEY.read_text().strip()
    paragraphs=[x.strip() for x in re.split(r'\n\s*\n', (OUT/'narration-script.txt').read_text()) if x.strip()]
    if len(paragraphs)!=8:raise SystemExit('Expected eight canonical narration paragraphs.')
    OUT.mkdir(parents=True,exist_ok=True)
    signature=hashlib.sha256('\n\n'.join(paragraphs).encode()).hexdigest()
    manifest={'provider':'ElevenLabs','model_id':MODEL,'voice_name':VOICE_NAME,'voice_id':VOICE_ID,
              'ai_generated_narration':True,'script_sha256':signature,'sample_rate_hz':48000,'channels':1,
              'official_api':'https://api.elevenlabs.io','voice_settings':VOICE_SETTINGS,
              'native_tempo_factor':1.0,'chapters':[]}
    manifest_path=OUT/'narration-manifest.json';lock=threading.Lock()
    old=json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    same_generation = (old.get('script_sha256') == signature and old.get('model_id') == MODEL
                       and old.get('voice_id') == VOICE_ID)
    completed={x['chapter']:x for x in old.get('chapters',[])} if same_generation else {}
    def generate(i):
        chapter=i+1;base=OUT/f'chapter-{chapter:02d}';mp3=base.with_suffix('.mp3');wav=base.with_suffix('.wav')
        if chapter in completed and mp3.exists() and wav.exists():return completed[chapter]
        spoken=SPOKEN_TEXTS[i] if SPOKEN_TEXTS is not None else (DIRECTIONS[i]+' '+paragraphs[i]).strip()
        body={'text':spoken,'model_id':MODEL,'language_code':'en',
              'voice_settings':VOICE_SETTINGS,
              'seed':1946+i,'apply_text_normalization':'auto'}
        # The live official API explicitly rejects neighboring text for v3.
        # Consistency comes from one stock voice and identical voice settings.
        if MODEL != 'eleven_v3':
            if i:body['previous_text']=paragraphs[i-1]
            if i+1<len(paragraphs):body['next_text']=paragraphs[i+1]
        with httpx.Client(timeout=240,follow_redirects=False) as client:
            response=client.post(f'https://api.elevenlabs.io/v1/text-to-speech/{VOICE_ID}/with-timestamps',
                                 params={'output_format':'mp3_44100_128'},headers={'xi-api-key':key,'Content-Type':'application/json'},json=body)
        if response.status_code!=200:
            # No arbitrary API body or headers are logged.
            error='unknown'
            try:error=response.json().get('detail',{}).get('status','unknown')
            except (ValueError,AttributeError):pass
            raise RuntimeError(f'Chapter {chapter} generation refused: HTTP {response.status_code}, code {error}.')
        result=response.json();mp3.write_bytes(base64.b64decode(result['audio_base64']))
        alignment=result.get('normalized_alignment') or result.get('alignment')
        write_json(base.with_suffix('.alignment.json'),{'alignment':result.get('alignment'),'normalized_alignment':result.get('normalized_alignment')})
        subprocess.run(['ffmpeg','-v','error','-y','-i',str(mp3),'-ar','48000','-ac','1','-c:a','pcm_s16le',str(wav)],check=True)
        item={'chapter':chapter,'label':LABELS[i],'text':paragraphs[i],'direction':DIRECTIONS[i],'spoken_input':spoken,
              'mp3_path':str(mp3),'wav_path':str(wav),'alignment_path':str(base.with_suffix('.alignment.json')),
              'duration_seconds':duration(wav),'sentences':sentence_cues(alignment)}
        with lock:
            completed[chapter]=item;manifest['chapters']=[completed[n] for n in sorted(completed)];write_json(manifest_path,manifest)
            print(json.dumps({'chapter':chapter,'duration_seconds':item['duration_seconds'],'wav_path':str(wav)}),flush=True)
        return item
    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in as_completed([pool.submit(generate,i) for i in range(8)]):future.result()
    manifest['chapters']=[completed[n] for n in sorted(completed)];offset=0;srt=[];cue_id=1
    for item in manifest['chapters']:
        item['start_seconds']=offset;item['end_seconds']=offset+item['duration_seconds']
        for cue in item['sentences']:
            # Provider alignment can exceed decoded MP3 duration by a few ms.
            # Preserve the raw alignment; keep derived captions inside PCM.
            cue['start']=max(0,min(cue['start'],item['duration_seconds']))
            cue['end']=max(cue['start'],min(cue['end'],item['duration_seconds']))
            srt.extend([str(cue_id),stamp(offset+cue['start'])+' --> '+stamp(offset+cue['end']),cue['text'],'']);cue_id+=1
        offset=item['end_seconds']
    manifest['duration_seconds']=offset
    (OUT/'narration.srt').write_text('\n'.join(srt))
    concat=OUT/'narration-concat.txt';concat.write_text(''.join("file '"+Path(item['wav_path']).name+"'\n" for item in manifest['chapters']))
    subprocess.run(['ffmpeg','-v','error','-y','-f','concat','-safe','0','-i',str(concat),'-c:a','pcm_s16le',str(OUT/'narration.wav')],check=True)
    subprocess.run(['ffmpeg','-v','error','-y','-i',str(OUT/'narration.wav'),'-c:a','libmp3lame','-b:a','192k',str(OUT/'narration.mp3')],check=True)
    manifest['wav_path']=str(OUT/'narration.wav');manifest['mp3_path']=str(OUT/'narration.mp3');manifest['srt_path']=str(OUT/'narration.srt')
    write_json(manifest_path,manifest)
    print(json.dumps({'complete':True,'duration_seconds':offset,'manifest':str(manifest_path),'voice':VOICE_NAME,'model':MODEL}),flush=True)


if __name__=='__main__':main()
