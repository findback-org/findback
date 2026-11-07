"""Root-cued fresh recording stages; only authenticated human posts are scripted.

The separately running ShowcaseRuntime produces every agent response and calls
real models. No stage implies another, and this helper cannot record handover
or receipt. Prior campuses and their journals are untouched.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from .showcase_story import CampusStory, StoryError, _save

STAGES = ('context','found','claim','confirm','evidence','approval','collection')


class LiveTake(CampusStory):
    def __init__(self, state_dir: Path, *, pace: float = 1.0):
        super().__init__(state_dir,state_dir/'evidence',pace=pace,max_seconds=240)
        self.journal_path=self.state_dir/'live-take.json'
        self.journal=json.loads(self.journal_path.read_text()) if self.journal_path.exists() else {
            'schema':1,'community_channel_id':self.community,'started_at':datetime.now(timezone.utc).isoformat(),
            'steps':{},'stages':{},'disclosure':'Fresh scripted fictional humans; real live Mattermost/Switch/model reactions. Stops before physical handover or receipt.'}
        if self.journal['community_channel_id']!=self.community:raise StoryError('Live take journal belongs to another campus.')

    def complete(self,name,data):
        self.journal['stages'][name]={'completed_at':datetime.now(timezone.utc).isoformat(),**data}
        _save(self.journal_path,self.journal)
        self.event_times()
        return self.journal['stages'][name]

    def event_times(self):
        """Actual remote creation times for frame-window selection; no posts."""
        people={p['id']:name for name,p in self.people.items()}
        labels={p['post_id']:label for label,p in self.journal['steps'].items()}
        since=datetime.fromisoformat(self.journal['started_at']).timestamp()*1000
        events=[]
        for stream,person,channel in [('community','sam',self.community),('alex','alex',self.private_channel('C002')),('staff','mira',self.staff)]:
            batch=self.api(person,'GET',f'/api/v4/channels/{channel}/posts?per_page=200')
            for post in batch.get('posts',{}).values():
                if post['create_at']<since:continue
                text=post.get('message','')
                events.append({'stream':stream,'post_id':post['id'],'created_at_ms':post['create_at'],
                    'speaker':people.get(post['user_id'],'actual-agent-or-platform'),
                    'stage_step':labels.get(post['id']),'root_id':post.get('root_id') or None,
                    'excerpt':text[:220]})
        events.sort(key=lambda x:x['created_at_ms'])
        report={'checked_at':datetime.now(timezone.utc).isoformat(),'actual_remote_post_times':True,'events':events}
        _save(self.state_dir/'live-event-times.json',report)
        return report

    def prior(self,name):
        if name not in self.journal['stages']:raise StoryError('Root must cue '+name+' first; no earlier stage is posted automatically.')
        return self.journal['stages'][name]

    def context(self):
        notice=self.post('live-disclosure','mira',self.community,'Fictional campus simulation: demo participants and objects, real FindBack agents. This recovery will stop at approved collection, before handover.')
        for label,text,item in [
            ('live-log-a','Log case A: black headphone case. Found at Library Desk A. Private details: green triangle sticker under lid; short blue charging cable inside.','I-001'),
            ('live-log-b','Log case B: black headphone case. Found at Library Study Room 2. Private details: red thread on zipper; initials RK inside.','I-002')]:
            p=self.post(label,'mira',self.staff,text);self.inbox(p)
            self.wait('actual desk item '+item,lambda:self.row('SELECT id FROM items WHERE id=?',(item,)))
        p=self.post('live-alex-context','alex',self.community,'I’ve checked the library twice. My headphone case is missing, and my bus leaves soon.')
        self.post('live-elena-context','elena',self.community,'I’m still near the library. Let’s see if someone has handed it in.')
        return self.complete('context',{'alex_post_id':p['post_id']})

    def found(self):
        self.prior('context')
        p=self.post('live-ava-found','ava',self.community,'I found a black headphone case in Library Study Room 2. I’ve brought it to Mira at the library desk. Does it belong to anyone?')
        self.inbox(p)
        self.wait('actual found acknowledgment delivery',lambda:self.row(
            "SELECT remote_id FROM runtime_messages WHERE id=? AND state='SENT'",(p['switch_event_id']+':found-ack',)))
        return self.complete('found',{'found_post_id':p['post_id']})

    def claim(self):
        found=self.prior('found')
        self.post('live-alex-possible','alex',self.community,'That might be mine — I was studying there this afternoon. It’s a plain black case.',root_id=found['found_post_id'])
        self.post('live-noah-ambiguity','noah',self.community,'Mine is a plain black case too. We should check more than the color.',root_id=found['found_post_id'])
        p=self.post('live-alex-report','alex',self.community,'I lost a black headphone case in the library this afternoon. Could you help me check whether the one at the desk is mine?',root_id=found['found_post_id'])
        self.inbox(p)
        draft=self.wait('real private Intake draft',lambda:(self.case('C002') or {}).get('current_draft_id'))
        card=self.card(draft,self.private_channel('C002'),'alex')
        return self.complete('claim',{'report_source_event_id':p['switch_event_id'],'draft_id':draft,'draft_post_id':card['mattermost_post_id']})

    def confirm(self):
        claim=self.prior('claim')
        p=self.post('live-alex-confirm','alex',self.private_channel('C002'),"Yes, that's right.",root_id=claim['draft_post_id'])
        self.inbox(p)
        review=self.review_for('C002',p,'live-alex:initial-review')
        hurry=self.post('live-alex-hurry','alex',self.private_channel('C002'),'Can you just release it? I’m worried I’ll miss my bus, and I can’t remember the marks yet.')
        self.inbox(hurry)
        return self.complete('confirm',{'initial_review_id':review['record_id'],'initial_review_post_id':review['mattermost_post_id']})

    def evidence(self):
        self.prior('confirm')
        p=self.post('live-alex-evidence','alex',self.private_channel('C002'),'I remember now. Mine has a red thread on the zipper and the initials RK on the inside label.')
        self.inbox(p)
        card=self.review_for('C002',p,'live-alex:evidence-review')
        return self.complete('evidence',{'source_event_id':p['switch_event_id'],'review_id':card['record_id'],'review_post_id':card['mattermost_post_id']})

    def approval(self):
        evidence=self.prior('evidence')
        p=self.post('live-mira-approval','mira',self.staff,'Approve case B for Alex. Alex independently described the red thread on the zipper and the initials RK inside. I checked both against the desk observations.',root_id=evidence['review_post_id'])
        self.inbox(p)
        row=self.wait('actual atomic reservation',lambda:self.row("SELECT id,status,item_id FROM reservations WHERE claim_id='C002' AND status='ACTIVE' ORDER BY rowid DESC LIMIT 1"))
        if row['item_id']!='I-002':raise StoryError('The actual reservation is not case B.')
        return self.complete('approval',{'reservation_id':row['id'],'review_post_id':evidence['review_post_id']})

    def collection(self):
        approval=self.prior('approval')
        p=self.post('live-mira-collection','mira',self.staff,'Collection: Your headphone case is reserved at the library desk. Please ask for Mira and show this private recovery chat.',root_id=approval['review_post_id'])
        self.inbox(p)
        self.wait('actual collection notification',lambda:self.row("SELECT id FROM outbox WHERE source_event_id=? AND topic='collection-arranged' AND delivery_state='SENT'",(p['switch_event_id'],)))
        claim=self.case('C002');item=self.row("SELECT status FROM items WHERE id='I-002'");returned=self.row('SELECT COUNT(*) AS count FROM returns')
        if claim['status']!='APPROVED' or item['status']!='RESERVED' or returned['count']!=0:raise StoryError('Actual final state is not reserved for collection before handover.')
        self.post('live-alex-thanks','alex',self.community,'Thanks, I have the collection details now. I’m heading to the library desk.',root_id=self.prior('found')['found_post_id'])
        return self.complete('collection',{'claim_status':claim['status'],'item_status':item['status'],'handover_or_receipt_count':returned['count'],'public_boundary':self.verify_public_boundary()})


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--state-dir',type=Path,default=Path('.findback/live-recording'));p.add_argument('--stage',choices=STAGES,required=True);args=p.parse_args()
    take=LiveTake(args.state_dir)
    try:print(json.dumps({'stage':args.stage,**getattr(take,args.stage)()},indent=2))
    finally:take.close()


if __name__=='__main__':main()
