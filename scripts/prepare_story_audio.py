"""Derive edit beats from one continuous, approved native-pace narration.

No network calls. The final AAC/WAV stay continuous; local beat WAVs are edit
references cut from exact PCM boundaries, not separate TTS generations.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import textwrap
import wave


def run(args):
    p=subprocess.run(args,capture_output=True,text=True)
    if p.returncode:raise RuntimeError(p.stderr[-1500:])
    return p


def stamp(value):
    n=round(value*1000);h,n=divmod(n,3600000);m,n=divmod(n,60000);s,n=divmod(n,1000)
    return f'{h:02d}:{m:02d}:{s:02d},{n:03d}'


def clean_alignment(data):
    a=data.get('normalized_alignment') or data['alignment'];result={'characters':[],'character_start_times_seconds':[],'character_end_times_seconds':[]};inside=False
    for c,s,e in zip(a['characters'],a['character_start_times_seconds'],a['character_end_times_seconds']):
        if c=='[':inside=True
        if not inside:
            result['characters'].append(c);result['character_start_times_seconds'].append(s);result['character_end_times_seconds'].append(e)
        if c==']':inside=False
    return result


def signature(text):return ''.join(c.casefold() for c in text if c.isalnum())


def prepare(out):
    plan=json.loads((out/'story-plan.json').read_text());assert plan['status']=='ROOT_SCRIPT_APPROVED'
    text=(out/'narration-script.txt').read_text();assert hashlib.sha256(text.encode()).hexdigest()==plan['script_sha256']
    raw=out/'narration-raw.wav'
    with wave.open(str(raw)) as w:
        rate=w.getframerate();count=w.getnframes();assert rate==48000 and w.getnchannels()==1 and w.getsampwidth()==2
    duration=count/rate
    a=clean_alignment(json.loads((out/'global-alignment.json').read_text()));chars=a['characters'];starts=a['character_start_times_seconds'];ends=a['character_end_times_seconds']
    full=''.join(chars);sig=[];mapping=[]
    for i,c in enumerate(chars):
        if c.isalnum():
            folded=c.casefold();sig.extend(folded);mapping.extend([i]*len(folded))
    sig=''.join(sig);positions=[]
    for beat in plan['beats']:
        anchor=signature(beat['unique_anchor']);index=sig.find(anchor)
        if index<0 or sig.find(anchor,index+1)>=0:raise ValueError('Missing/ambiguous global anchor: '+beat['unique_anchor'])
        positions.append(mapping[index])
    assert positions==sorted(positions)
    boundaries=[0]
    for index in positions[1:]:
        left=index-1
        while left>=0 and chars[left].isspace():left-=1
        preceding=ends[left] if left>=0 else 0
        onset=starts[index]
        at=(preceding+onset)/2 if preceding<=onset else onset
        boundaries.append(round(max(0,min(at,duration))*rate))
    boundaries.append(count)
    assert all(x<y for x,y in zip(boundaries,boundaries[1:]))
    # Whole-track loudness mastering only. No atempo/pitch/cadence changes.
    measured=run(['ffmpeg','-hide_banner','-i',str(raw),'-af','loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json','-f','null','-'])
    match=re.search(r'\{\s*"input_i"[\s\S]*?\}',measured.stderr)
    if not match:raise ValueError('Could not measure narration loudness.')
    loud=json.loads(match.group())
    filt=('loudnorm=I=-16:TP=-1.5:LRA=11:measured_I='+loud['input_i']+':measured_TP='+loud['input_tp']+
          ':measured_LRA='+loud['input_lra']+':measured_thresh='+loud['input_thresh']+':offset='+loud['target_offset']+
          ':linear=true,aresample=48000,apad,atrim=end_sample='+str(count))
    master=out/'narration.wav'
    run(['ffmpeg','-v','error','-y','-i',str(raw),'-af',filt,'-ar','48000','-ac','1','-c:a','pcm_s16le',str(master)])
    with wave.open(str(master)) as w:assert w.getnframes()==count;pcm=w.readframes(count)
    run(['ffmpeg','-v','error','-y','-i',str(master),'-c:a','aac','-b:a','192k','-movflags','+faststart',str(out/'narration.m4a')])
    chapters=[]
    for n,beat in enumerate(plan['beats']):
        begin,end=boundaries[n:n+2];offset=begin/rate;length=(end-begin)/rate
        start_index=positions[n];end_index=positions[n+1] if n+1<len(positions) else len(chars)
        local={'characters':chars[start_index:end_index],
               'character_start_times_seconds':[max(0,min(s-offset,length)) for s in starts[start_index:end_index]],
               'character_end_times_seconds':[max(0,min(e-offset,length)) for e in ends[start_index:end_index]]}
        local_file=out/f'chapter-{n+1:02d}.alignment.json';local_file.write_text(json.dumps({'alignment':local,'normalized_alignment':local},ensure_ascii=False)+'\n')
        wav=out/f'chapter-{n+1:02d}.wav'
        with wave.open(str(wav),'wb') as w:
            w.setnchannels(1);w.setsampwidth(2);w.setframerate(rate);w.writeframes(pcm[begin*2:end*2])
        chapters.append({'chapter':n+1,'label':beat['label'],'text':beat['text'],'unique_anchor':beat['unique_anchor'],
                         'audio_start_seconds':offset,'audio_seconds':length,'audio_start_sample':begin,'audio_end_sample':end,
                         'wav_file':wav.name,'alignment_file':local_file.name,'first_spoken_word_at_global_seconds':starts[start_index]})
    # Global captions use provider sentence timing with only control tags removed.
    cues=[]
    for match in re.finditer(r'[^.!?]+[.!?]+[”’\"\']?|[^.!?]+$',full):
        b,e=match.span()
        while b<e and chars[b].isspace():b+=1
        while e>b and chars[e-1].isspace():e-=1
        if b==e:continue
        start=max(0,min(starts[b],duration));end=max(start,min(ends[e-1],duration));caption=''.join(chars[b:e]).strip()
        if end>start:cues.append({'start':start,'end':end,'text':caption})
    lines=[]
    for n,c in enumerate(cues,1):lines.extend([str(n),stamp(c['start'])+' --> '+stamp(c['end']),'\n'.join(textwrap.wrap(c['text'],55,break_long_words=False,break_on_hyphens=False)),''])
    (out/'narration.srt').write_text('\n'.join(lines),encoding='utf-8')
    metadata=json.loads((out/'generation-metadata.json').read_text())
    manifest={**metadata,'kind':'One continuous approved story narration; beat files are local PCM/alignment edit references.',
              'audio_seconds':duration,'narration_file':'narration.wav','aac_file':'narration.m4a','subtitle_file':'narration.srt',
              'global_alignment_file':'global-alignment.json','voice_tempo_factor':1.0,'single_continuous_audio':True,
              'audio_sample_count':count,'sample_rate_hz':rate,'continuous_aac_sha256':hashlib.sha256((out/'narration.m4a').read_bytes()).hexdigest(),
              'mastering':{'whole_track_only':True,'loudness_target_lufs':-16,'true_peak_target_dbtp':-1.5,'cadence_or_pitch_changed':False,'sample_count_preserved':True,'source_measurement':loud},
              'alignment_method':'Unique canonical anchors matched in actual global character alignment; boundaries at inter-paragraph pause midpoints. Beat1 starts0; finalbeat includes full trailing audio. FinalAAC is never reassembled from beat files.',
              'chapters':chapters,'global_sentence_cues':cues}
    (out/'narration-manifest.json').write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({'audio_seconds':duration,'chapters':len(chapters),'aac':str(out/'narration.m4a'),'manifest':str(out/'narration-manifest.json'),'caption_cues':len(cues)},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output-dir',type=Path,default=Path('demo/story-voiceover'));args=p.parse_args();prepare(args.output_dir.resolve())
