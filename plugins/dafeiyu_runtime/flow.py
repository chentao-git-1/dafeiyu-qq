"""Bounded per-sender debounce and stale-response tracking."""
import asyncio
from collections import OrderedDict
import time

class ChatFlow:
    def __init__(self,quiet=1.8,maximum=4.0):
        self.quiet=quiet; self.maximum=maximum
        self.buckets={}; self.epochs=OrderedDict(); self.seen=OrderedDict()
        self.stats={'merged_messages':0,'duplicate_events':0,'stale_replies':0}

    def observe(self,key,message_id):
        now=time.monotonic()
        if message_id:
            identity=(key,str(message_id))
            if identity in self.seen:
                self.stats['duplicate_events']+=1
                return None
            self.seen[identity]=now
            while self.seen and (len(self.seen)>1024 or now-next(iter(self.seen.values()))>600):self.seen.popitem(last=False)
        epoch=self.epochs.get(key,0)+1
        self.epochs[key]=epoch; self.epochs.move_to_end(key)
        while len(self.epochs)>256:self.epochs.popitem(last=False)
        return epoch

    def fresh(self,key,epoch):return self.epochs.get(key)==epoch

    async def collect(self,key,event,text):
        now=time.monotonic()
        bucket=self.buckets.get(key)
        if bucket is None:
            bucket={'start':now,'last':now,'events':[],'latest':event}
            self.buckets[key]=bucket
        bucket['last']=now; bucket['latest']=event
        bucket['events'].append(text)
        if len(bucket['events'])>16:bucket['events']=bucket['events'][-16:]
        while True:
            remaining=min(bucket['last']+self.quiet,bucket['start']+self.maximum)-time.monotonic()
            if remaining<=0:break
            await asyncio.sleep(remaining)
        if bucket['latest'] is not event:return None
        if self.buckets.get(key) is bucket:self.buckets.pop(key,None)
        self.stats['merged_messages']+=max(0,len(bucket['events'])-1)
        return '\n'.join(bucket['events'])
