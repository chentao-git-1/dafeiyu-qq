"""Model-led contact with the verified comment author, using existing private delivery."""
import asyncio
import datetime as dt
from decimal import Decimal,InvalidOperation
import hashlib
import json
import time

from astrbot.api import logger
from astrbot.api.event import MessageChain
from astrbot.core.message.components import Plain
from dafeiyu.dialogue.catalog import MemoryCatalog
from dafeiyu.dialogue.contracts import TurnContext
from dafeiyu.dialogue.self_context import default_character
from dafeiyu.initiative.social_intents import SocialIntentStore,SocialReply
from dafeiyu.initiative.social_policy import choose,TZ
from dafeiyu.social.store import DEFAULT,can_notify_profile

BOT='7000000002'
PLATFORM='dafeiyu-qq'


class SocialEngine:
    def __init__(self,plugin,clock=time.time):
        self.plugin,self.context,self.clock=plugin,plugin.context,clock
        self.root=plugin.path.parent.parent.parent
        self.store=SocialIntentStore(plugin.path.with_name('social.db'),clock)
        self.lock=asyncio.Lock();self.closed=False

    def active(self,name):
        meta=self.context.get_registered_star(name)
        return getattr(meta,'star_cls',None) if meta and meta.activated else None

    @staticmethod
    def origin(peer):return PLATFORM+':FriendMessage:'+str(peer)

    def permission(self,peer):
        peer=str(peer)
        if not peer.isdigit() or peer==BOT:return None
        cfg=self.context.get_config(self.origin(peer)).get('platform_settings',{})
        allowed={str(x) for x in cfg.get('id_whitelist',[])}
        if peer not in allowed and self.origin(peer) not in allowed:return None
        path=self.root/'plugin_data/dafeiyu_social/preferences.db'
        if not path.is_file():return None
        if MemoryCatalog._read(path,'SELECT 1 FROM bots WHERE qq=?',(peer,)):return None
        rows=MemoryCatalog._read(path,'SELECT data FROM profiles WHERE user_id=?',(peer,))
        profile={**DEFAULT,**(json.loads(rows[0]['data']) if rows else {})}
        return profile if profile['proactive'] else None

    def resources(self):
        balance=self.active('astrbot_plugin_dafeiyu_balance')
        if balance is None:return False
        status=balance.watch.status()
        try:
            value=Decimal(str(status.get('last_balance_cny')))
            return bool(value.is_finite() and value>=Decimal('0.70') and status.get('last_available') is True
                and 0<=self.clock()-float(status.get('last_checked_at') or 0)<=1800)
        except (InvalidOperation,ValueError,TypeError):return False

    def trusted_peer(self,event):
        raw=getattr(event.message_obj,'raw_message',{})
        peer=str(event.get_sender_id())
        if (event.get_platform_id()!=PLATFORM or event.get_platform_name()!='aiocqhttp' or not event.is_private_chat()
                or event.unified_msg_origin!=self.origin(peer) or not isinstance(raw,dict)
                or raw.get('post_type')!='message' or str(raw.get('self_id'))!=BOT or str(raw.get('user_id'))!=peer
                or event.get_extra('cron_job',None) or event.get_extra('cron_payload',None)):
            return None
        return peer if self.permission(peer) is not None else None

    def qzone(self):return self.active('astrbot_plugin_qzone')

    def source_valid(self,row):
        qzone=self.qzone();path=getattr(getattr(qzone,'cfg',None),'db_path',None)
        if path is None:return False
        return bool(MemoryCatalog._read(path,"SELECT 1 FROM posts WHERE tid=? AND uin=? AND status='approved'",(row['source']['post_tid'],BOT)))

    async def packet(self,row,allow_public):
        peer=row['peer'];now=self.clock();state=await asyncio.to_thread(self.store.peer,peer)
        controller=self.active('dafeiyu_dialogue');natural=self.active('dafeiyu_natural_chat')
        if controller is None or natural is None:raise RuntimeError('Dialogue context unavailable')
        turn=TurnContext(peer,self.origin(peer),PLATFORM,'social:'+row['id'],now)
        snapshot=await asyncio.to_thread(controller.catalog().read,turn,now)
        # Only this person's own private evidence is offered, never other contacts' records.
        private=[dict(r.data(),time_beijing=dt.datetime.fromtimestamp(r.recorded_at,TZ).isoformat()) for r in sorted(snapshot.items,key=lambda r:r.recorded_at,reverse=True)
            if r.owner==peer and r.origin==turn.origin and r.kind in ('exchange','preference','event','fact')][:8]
        outgoing=MemoryCatalog._read(self.root/'plugin_data/dafeiyu_runtime/outgoing.db',
            'SELECT message_id,sent_at,body FROM outgoing WHERE qq=? ORDER BY sent_at DESC LIMIT 4',(peer,))
        outgoing=[dict(r,time_beijing=dt.datetime.fromtimestamp(r['sent_at'],TZ).isoformat()) for r in outgoing]
        label=next((name for name,uid in self.plugin.family.items() if str(uid)==peer),'朋友')
        character=default_character(natural.dialogue_character())
        relationships=await controller.relationship_profile(turn) if callable(getattr(controller,'relationship_profile',None)) else []
        timing=dict(beijing_now=dt.datetime.fromtimestamp(now,TZ).isoformat(),peer_state=state,
            contact_settings=self.permission(peer))
        fingerprint=hashlib.sha256(json.dumps(dict(private=private,outgoing=outgoing,epoch=state['epoch'],
            relationship_memory=relationships),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        public=dict(row['source'],comment_time_beijing=dt.datetime.fromtimestamp(row['source']['comment_at'],TZ).isoformat(),
            post_time_beijing=dt.datetime.fromtimestamp(row['source']['post_at'],TZ).isoformat())
        previous={k:row['decision'].get(k) for k in ('intention','reason','when')} if row['decision'].get('_context_key')==fingerprint else {}
        return dict(target=dict(relationship=label,verified_author=True),public_material=public,
            private_context=dict(records=private,confirmed_outgoing=outgoing,relationship_memory=relationships),character=character,timing=timing,
            owner_requested=bool(row['requested']),owner_requested_at=dt.datetime.fromtimestamp(row['created'],TZ).isoformat() if row['requested'] else None,
            allow_public_reply=bool(allow_public and not row['requested']),previous_intention=previous),state['epoch'],fingerprint

    async def evaluate(self,row,allow_public=False,provider=None):
        packet,epoch,fingerprint=await self.packet(row,allow_public)
        if not await asyncio.to_thread(self.store.claim,row['id'],epoch):return None
        try:
            if provider is None:provider=await self.context.get_using_provider_async(self.origin(row['peer']))
            if provider is None:raise RuntimeError('Social provider unavailable')
            controller=self.active('dafeiyu_dialogue')
            provider=controller.metered_provider(provider,'social_followthrough',structured=False)
            decision=await asyncio.wait_for(choose(provider,packet,self.clock()),timeout=40)
            if self.permission(row['peer']) is None or not self.source_valid(row):
                await asyncio.to_thread(self.store.cancel,row['id'],'source_or_permission_withdrawn');return None
            _,current_epoch,current_fingerprint=await self.packet(row,False)
            if current_fingerprint!=fingerprint or current_epoch!=epoch:
                await asyncio.to_thread(self.store.changed,row['id']);return None
            decision['_context_key']=fingerprint
            saved=await asyncio.to_thread(self.store.decide,row['id'],decision,epoch)
            return decision if saved else None
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.failed,row['id'],'interrupted');raise
        except Exception as error:
            await asyncio.to_thread(self.store.failed,row['id'],type(error).__name__);raise

    async def handle_comment(self,post,comment,provider):
        peer=str(comment.uin)
        if self.closed or int(post.uin)!=int(BOT) or not post.tid or self.permission(peer) is None:return None
        source=dict(peer=peer,post_tid=str(post.tid),comment_id=int(comment.tid),parent_id=comment.parent_tid or 0,
            post_at=post.create_time,comment_at=comment.create_time,post_text=post.text[:1500],comment=comment.content[:1500])
        async with self.lock:
            key=await asyncio.to_thread(self.store.observe,source)
            if key is None:return None
            row=await asyncio.to_thread(self.store.get,key)
            if row['status']!='pending':return SocialReply('')
            if not self.resources():
                await asyncio.to_thread(self.store.postpone,key,self.clock()+300,'resources_unavailable')
                return SocialReply('')
            decision=await self.evaluate(row,True,provider)
            if decision is None:raise RuntimeError('Social context changed')
            return SocialReply(decision['public_reply'])

    def receipt(self,peer,body,since):
        rows=MemoryCatalog._read(self.root/'plugin_data/dafeiyu_runtime/outgoing.db',
            'SELECT message_id,body FROM outgoing WHERE qq=? AND sent_at>=? ORDER BY sent_at DESC LIMIT 8',(peer,int(since)))
        return next((str(r['message_id']) for r in rows if r['body']==body.strip()[:1500]),'')

    async def send(self,row,epoch):
        peer=row['peer'];profile=self.permission(peer)
        if profile is None:
            await asyncio.to_thread(self.store.cancel,row['id'],'contact_disabled');return
        if not can_notify_profile(profile,self.clock()) or not self.resources():
            await asyncio.to_thread(self.store.postpone,row['id'],self.clock()+300,'waiting_for_permission_or_resources');return
        state=await asyncio.to_thread(self.store.peer,peer)
        if state['pending_since'] or state['last_in']>state['last_out'] and self.clock()-state['last_in']<180:
            await asyncio.to_thread(self.store.postpone,row['id'],self.clock()+60,'waiting_for_current_conversation');return
        if not self.source_valid(row):
            await asyncio.to_thread(self.store.cancel,row['id'],'source_unavailable');return
        _,current_epoch,current_key=await self.packet(row,False)
        if current_epoch!=epoch or current_key!=row['decision'].get('_context_key'):
            await asyncio.to_thread(self.store.postpone,row['id'],self.clock()+60,'conversation_changed');return
        since=self.clock();body=row['decision']['private_message']
        if not body or not await asyncio.to_thread(self.store.begin_send,row['id'],epoch):return
        try:
            await asyncio.wait_for(self.context.send_message(self.origin(peer),MessageChain(chain=[Plain(body)])),timeout=40)
            mid=await asyncio.to_thread(self.receipt,peer,body,since)
            await asyncio.to_thread(self.store.finish_send,row['id'],mid,'' if mid else 'unconfirmed_delivery')
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.finish_send,row['id'],'','interrupted');raise
        except Exception as error:
            await asyncio.to_thread(self.store.finish_send,row['id'],'',type(error).__name__)

    async def tick(self):
        if self.closed or self.lock.locked():return
        async with self.lock:
            for row in await asyncio.to_thread(self.store.due):
                profile=self.permission(row['peer'])
                if profile is None or not self.source_valid(row):
                    await asyncio.to_thread(self.store.cancel,row['id'],'source_or_permission_withdrawn');continue
                if not self.resources() or not can_notify_profile(profile,self.clock()):
                    await asyncio.to_thread(self.store.postpone,row['id'],self.clock()+300,'waiting_for_permission_or_resources');continue
                peer=await asyncio.to_thread(self.store.peer,row['peer'])
                if peer['pending_since'] or peer['last_in']>peer['last_out'] and self.clock()-peer['last_in']<180:
                    await asyncio.to_thread(self.store.postpone,row['id'],self.clock()+60,'waiting_for_current_conversation');continue
                try:
                    # Immediate replies reuse the same generation. Delayed or changed contexts are generated once when due.
                    _,_,context_key=await self.packet(row,False)
                    if row['status']!='ready' or row['epoch']!=peer['epoch'] or self.clock()-row['updated']>120 or context_key!=row['decision'].get('_context_key'):
                        if await self.evaluate(row) is None:return
                        row=await asyncio.to_thread(self.store.get,row['id'])
                    if row['status']=='ready':await self.send(row,row['epoch'])
                except asyncio.CancelledError:raise
                except Exception as error:logger.warning('Social follow-through unavailable: %s',type(error).__name__)
                return

    async def status(self):
        return dict(enabled=True,source='verified_own_space_comments',target='verified_allowed_comment_author',
            timing='model_decides',wording='model_generated',extra_review_calls=0,
            initial_decision_reuses_comment_generation=True,loop_poll_seconds=60,**await asyncio.to_thread(self.store.status))
