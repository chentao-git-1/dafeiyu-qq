"""Grounded, model-led private initiative for the owner."""
import asyncio
import datetime as dt
from decimal import Decimal,InvalidOperation
import hashlib
import json
import time

from astrbot.api.event import MessageChain
from astrbot.core.message.components import Plain
from dafeiyu.initiative.spontaneous_store import InitiativeStore,TZ

OWNER='7000000001';SESSION='dafeiyu-qq:FriendMessage:'+OWNER

class InitiativeEngine:
    def __init__(self,plugin,store,clock=time.time):
        self.plugin=plugin;self.context=plugin.context;self.store=store;self.clock=clock
        self.lock=asyncio.Lock();self.current=None
    def active(self,name):
        meta=self.context.get_registered_star(name)
        return getattr(meta,'star_cls',None) if meta and meta.activated else None

    def semantic_controller(self):
        controller=self.active('dafeiyu_dialogue')
        return controller if controller and controller.owner_policy_enabled('initiative_policy_v2') else None
    def allowed(self):
        social=getattr(self.plugin,'social_engine',None)
        if social and (social.lock.locked() or social.store.peer(OWNER)['pending_since'] or social.store.active_for(OWNER)):return False
        cfg=self.context.get_config(SESSION)['platform_settings']
        if SESSION not in {str(x) for x in cfg.get('id_whitelist',[])}:return False
        from data.plugins.dafeiyu_social.access import can_notify
        if not can_notify(OWNER):return False
        balance=self.active('astrbot_plugin_dafeiyu_balance')
        if balance is None:return False
        status=balance.watch.status()
        try:
            value=Decimal(str(status.get('last_balance_cny')))
            return bool(value.is_finite() and value>=Decimal('0.70') and status.get('last_available') is True
                and 0<=self.clock()-float(status.get('last_checked_at') or 0)<=1800)
        except (InvalidOperation,ValueError,TypeError):return False
    async def care_allowed(self,topic):
        now=dt.datetime.fromtimestamp(self.clock(),TZ);state=self.plugin.state
        if OWNER in state.get('care_disabled',[]):return False
        if now.date().isoformat() in state.get('reserved_days',{}).get(OWNER,[]):return False
        for job in await self.context.cron_manager.list_jobs():
            p=job.payload or {}
            if p.get('dafeiyu_kind')=='followup' and p.get('dafeiyu_target')==OWNER and p.get('dafeiyu_topic')==topic:return False
        return True
    async def candidates(self):
        now=self.clock();today=dt.datetime.fromtimestamp(now,TZ).date()
        life=self.active('dafeiyu_life')
        if life:
            direction=await asyncio.to_thread(getattr(getattr(life,'engine',None),'direction_profile',lambda:None))
            if direction and direction['status']=='intent':
                source={'id':direction['id'],'kind':'intention','day':dt.datetime.fromtimestamp(direction['updated'],TZ).date().isoformat(),
                    'note':direction['question']+'\n'+direction['next_step'],'version':direction['version'],
                    'status':'intent','activity':direction['kind']}
                if not self.store.used(source['id']):yield source
            record=life.public_context()
            if record and 0<=now-record.get('completed_at',0)<=48*3600:
                source={'id':str(record['id']),'kind':'life','day':record['day'],
                    'activity':record['kind'],'note':record['note'],'thought':record['thought'],
                    'completed_at':record['completed_at'],'source':record.get('source',{})}
                if not self.store.used(source['id']):yield source
        natural=self.active('dafeiyu_natural_chat')
        if not natural:return
        for source in await asyncio.to_thread(self.conversation_candidates):
            if not self.store.used(source['id']):yield source
        controller=self.semantic_controller()
        if controller and controller.feature('learning_v2'):
            for row in controller.learning_store().current(OWNER,SESSION):
                if row['kind']!='event' or row['status'] not in ('planned','ongoing') or not row['event_day']:continue
                try:day=dt.date.fromisoformat(row['event_day'])
                except ValueError:continue
                if day>=today or not 0<=now-row['updated']<=48*3600:continue
                source={'id':row['id'],'kind':'care','learned_id':row['id'],'version':row['version'],
                    'topic':row['summary'],'day':row['event_day'],'evidence':row['evidence']}
                if not self.store.used(source['id']) and await self.care_allowed(source['topic']):yield source
        for row in natural.store.search(owner=OWNER,limit=20):
            if row['kind']!='event' or row['phase'] not in ('planned','ongoing') or not row['event_day']:continue
            try:day=dt.date.fromisoformat(row['event_day'])
            except ValueError:continue
            if day>=today or not 0<=now-row['updated']<=48*3600:continue
            source={'id':'episode:'+str(row['id']),'kind':'care','episode_id':row['id'],
                'updated':row['updated'],'topic':row['topic'],'day':row['event_day'],'evidence':row['evidence'][:500]}
            if not self.store.used(source['id']) and await self.care_allowed(source['topic']):yield source
    def conversation_candidates(self):
        natural=self.active('dafeiyu_natural_chat')
        if not natural or not callable(getattr(natural,'exchange_store',None)):return []
        from dafeiyu.dialogue.catalog import MemoryCatalog
        from dafeiyu.memory.interaction import privacy_time
        path=natural.exchange_store().path
        rows=MemoryCatalog._read(path,'SELECT id,human,at FROM exchanges WHERE qq=? AND origin=? AND at>=? ORDER BY at DESC,id DESC LIMIT 6',
            (OWNER,SESSION,max(self.clock()-7*86400,privacy_time(path,SESSION))))
        result=[]
        for row in rows:
            if not row['human'] or not 0<=self.clock()-row['at']<=7*86400:continue
            key=hashlib.sha256(json.dumps([OWNER,SESSION,row['id'],row['human'],row['at']],ensure_ascii=False).encode()).hexdigest()[:24]
            result.append(dict(id='conversation:'+key,kind='conversation',day=dt.datetime.fromtimestamp(row['at'],TZ).date().isoformat(),
                evidence=row['human'],recorded_at=row['at'],exchange_id=row['id']))
        return result
    async def source_valid(self,source):
        if source['kind']=='conversation':
            return source in await asyncio.to_thread(self.conversation_candidates)
        if source['kind']=='intention':
            life=self.active('dafeiyu_life')
            current=await asyncio.to_thread(getattr(getattr(life,'engine',None),'direction_profile',lambda:None))
            return bool(current and current['status']=='intent' and current['id']==source['id']
                and current['version']==source['version'] and current['question']+'\n'+current['next_step']==source['note'])
        if source['kind']=='care':
            if source.get('learned_id'):
                controller=self.semantic_controller()
                rows=controller.learning_store().current(OWNER,SESSION) if controller else []
                row=next((r for r in rows if r['id']==source['learned_id']),None)
                return bool(row and row['version']==source['version'] and row['status'] in ('planned','ongoing')
                    and await self.care_allowed(source['topic']))
            natural=self.active('dafeiyu_natural_chat')
            row=natural.store.get(source['episode_id']) if natural else None
            return bool(row and row['owner']==OWNER and row['phase'] in ('planned','ongoing') and row['updated']==source['updated']
                and await self.care_allowed(source['topic']))
        life=self.active('dafeiyu_life');current=life.public_context() if life else None
        return bool(current and str(current['id'])==source['id'] and 0<=self.clock()-current['completed_at']<=48*3600)
    async def tick(self):
        if self.semantic_controller():return await self.tick_semantic()
        await asyncio.to_thread(self.store.diagnose,'skip','semantic_controller_unavailable')

    async def review(self,provider,source,body,recent,previous):
        from dafeiyu.initiative.spontaneous_policy import review_initiative
        return await review_initiative(provider,source,body,recent,previous)

    async def tick_semantic(self):
        if self.lock.locked():return
        async with self.lock:
            await asyncio.to_thread(self.store.diagnose,'check')
            due=await asyncio.to_thread(self.store.due)
            if not due or not await asyncio.to_thread(self.store.eligible) or not self.allowed():return
            day,slot=due;epoch=await asyncio.to_thread(self.store.epoch);controller=self.semantic_controller()
            if OWNER in getattr(controller,'learning_state_inflight',()):return
            candidates=[]
            async for candidate in self.candidates():
                candidates.append(candidate)
                if len(candidates)>=12:break
            if not candidates:return
            try:
                from dafeiyu.initiative.spontaneous_policy import choose_initiative
                from dafeiyu.dialogue.contracts import TurnContext
                turn=TurnContext(OWNER,SESSION,'dafeiyu-qq','initiative:'+day+':'+str(slot),self.clock())
                snapshot=await asyncio.to_thread(controller.catalog().read,turn,self.clock())
                recent=[{'id':r.id,'text':r.text,'recorded_at':r.recorded_at} for r in snapshot.items
                    if r.owner==OWNER and r.origin==SESSION and r.kind=='exchange'][:6]
                provider=await self.context.get_using_provider_async(SESSION)
                if provider is None:raise RuntimeError('Text provider unavailable')
                provider=controller.metered_provider(provider,'initiative')
                previous=await asyncio.to_thread(self.store.recent_sent)
                growth=await controller.growth_profile(turn) if callable(getattr(controller,'growth_profile',None)) else {}
                relationships=await controller.relationship_profile(turn) if callable(getattr(controller,'relationship_profile',None)) else []
                context_key=hashlib.sha256(json.dumps({'candidates':candidates,'recent':recent,'previous':previous,
                    'growth':growth,'relationship_memory':relationships,'epoch':epoch},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                if not await asyncio.to_thread(self.store.claim_evaluation,day,slot,epoch,context_key):return
                self.current=(day,slot)
                timing=await asyncio.to_thread(self.store.timing_context)
                source,body,minutes=await choose_initiative(provider,candidates,recent,previous,day,growth=growth,timing=timing,
                    relationships=relationships,include_schedule=True)
                await asyncio.to_thread(self.store.finish_evaluation,day,slot,'selected' if source else 'no_action',minutes)
                if not source:return
                if epoch!=await asyncio.to_thread(self.store.epoch) or not await self.source_valid(source):return
                if not await self.review(provider,source,body,recent,previous):
                    await asyncio.to_thread(self.store.finish_evaluation,day,slot,'filtered',minutes)
                    await asyncio.to_thread(self.store.diagnose,'skip','semantic_review_declined');return
                async with self.plugin.lock:
                    if (not self.allowed() or not await self.source_valid(source)
                            or not await asyncio.to_thread(self.store.reserve,day,slot,source['id'],source['kind'],epoch)):return
                    if not await asyncio.to_thread(self.store.begin_send,day,slot,epoch,body):
                        await asyncio.to_thread(self.store.finish,day,slot,'cancelled','eligibility_changed');return
                    if source['kind']=='care':
                        self.plugin.state.setdefault('care_attempt_days',{})[OWNER]=day;self.plugin.save()
                try:sent=await asyncio.wait_for(self.context.send_message(SESSION,MessageChain(chain=[Plain(body)])),timeout=40)
                except Exception as error:
                    await asyncio.to_thread(self.store.finish,day,slot,'unknown',type(error).__name__);return
                await asyncio.to_thread(self.store.finish,day,slot,'sent' if sent else 'unknown','' if sent else 'send_not_confirmed')
            except asyncio.CancelledError:
                await asyncio.to_thread(self.store.finish_evaluation,day,slot,'unknown')
                await asyncio.to_thread(self.store.finish,day,slot,'unknown','interrupted');raise
            except Exception as error:
                await asyncio.to_thread(self.store.finish_evaluation,day,slot,'failed')
                await asyncio.to_thread(self.store.finish,day,slot,'failed',type(error).__name__)
                await asyncio.to_thread(self.store.diagnose,'error',type(error).__name__)
            finally:self.current=None
