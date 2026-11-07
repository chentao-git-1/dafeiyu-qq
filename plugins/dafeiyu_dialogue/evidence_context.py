"""AstrBot orchestration for evidence context.

Business rules live in dafeiyu; event registration remains in main.py.
"""
import asyncio
import copy
import hashlib
from pathlib import Path
import time
import datetime as dt
import sqlite3
from dafeiyu.dialogue.catalog import MemoryCatalog
from dafeiyu.dialogue.review import review, verify_revision, packet_key, ReviewResult, UNAVAILABLE, SCHEMA_RETRY
from dafeiyu.dialogue.self_state import self_state


class EvidenceContext:
    async def own_activity(self):
        meta = self.context.get_registered_star('dafeiyu_life')
        life = getattr(meta, 'star_cls', None) if meta and meta.activated else None
        if not life:
            return None, None

        def read():
            completed = life.public_context()
            store = getattr(life, 'store', None)
            path = getattr(store, 'path', None)
            today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
            try:
                if callable(getattr(store,'in_progress',None)):return completed,store.in_progress()
                rows = MemoryCatalog._read(path, 'SELECT day,kind,status,updated FROM runs WHERE day=?', (today,)) if path else []
                progress = rows[0] if rows and rows[0]['status'] == 'started' else None
                # A stale/interrupted run is not proof of something still happening.
                if progress and not 0 <= time.time() - float(progress['updated']) <= 600:
                    progress = None
            except (OSError, ValueError, TypeError, sqlite3.Error):
                progress = None
            return completed, progress

        return await asyncio.to_thread(read)

    async def own_thoughts(self):
        meta=self.context.get_registered_star('dafeiyu_life')
        life=getattr(meta,'star_cls',None) if meta and meta.activated else None
        store=getattr(life,'store',None)
        if store is None or not hasattr(store,'root'):return {}
        def read():
            root=Path(store.root);path=root/'self_journal.db'
            if not path.is_file():return {}
            try:
                projects=MemoryCatalog._read(path,"SELECT id,title,question,status,version,updated FROM projects ORDER BY updated DESC LIMIT 12",())
                candidates=MemoryCatalog._read(path,'SELECT work_id,work_sha,project_id,insight,next_question,recorded FROM reflections ORDER BY recorded DESC LIMIT 12',())
                records={r['id']:r for r in store.records(60)};reflections=[]
                for row in candidates:
                    work=records.get(row['work_id'])
                    if not work or work.get('sha256')!=row['work_sha']:continue
                    artifact=(root/work['artifact']).resolve()
                    if (artifact.parent!=(root/'works').resolve() or not artifact.is_file() or artifact.stat().st_size>4*1024*1024
                            or hashlib.sha256(artifact.read_bytes()).hexdigest()!=row['work_sha']
                            or not 0<float(work['completed_at'])<=time.time()+5):continue
                    reflections.append(dict(row,source_work={k:work[k] for k in ('id','day','kind','note','completed_at')}))
                direction=getattr(getattr(life,'engine',None),'direction_profile',lambda:None)()
                return dict(projects=[dict(p,status='intent' if p['status']=='active' else p['status']) for p in projects],reflections=reflections,
                    intentions=[direction] if direction else [])
            except (OSError,sqlite3.Error,KeyError,ValueError,TypeError):return {}
        return await asyncio.to_thread(read)

    async def conversation_works(self,event):
        meta=self.context.get_registered_star('astrbot_plugin_dafeiyu_draw')
        drawing=getattr(meta,'star_cls',None) if meta and meta.activated else None
        if drawing is None or not callable(getattr(drawing,'conversation_works',None)):return []
        turn=self.identity(event)
        try:return await asyncio.to_thread(drawing.conversation_works,turn.actor,turn.origin)
        except (OSError,ValueError,sqlite3.Error):return []

    async def provider_call(self, event, prompt, policy, purpose):
        if not self.model_verification_enabled() and purpose in {
                'review','verification','learning_check','learning_check_background','learning_state','generation_repair'}:
            raise RuntimeError('Chat model verification is disabled')
        if SCHEMA_RETRY in policy:
            if event.get_extra('dafeiyu_recovery_attempts',0):raise ValueError('Per-turn recovery budget exhausted')
            event.set_extra('dafeiyu_recovery_attempts',1)
            self.review_stats['schema_recovery_attempts']+=1
        provider = await self.context.get_using_provider_async(event.unified_msg_origin)
        if provider is None:
            raise ValueError('Current provider unavailable')
        response=await self.metered_provider(provider,purpose).text_chat(prompt=prompt,
            system_prompt=policy,contexts=[],func_tool=None,request_max_retries=1)
        return response.completion_text

    async def reviewed(self, event, segments, audience=''):
        if not self.model_verification_enabled():
            return ReviewResult(tuple(segments), 'off')
        mode = self.config.get('response_review', 'enforce')
        if mode == 'off':
            return ReviewResult(tuple(segments), 'off')
        evidence = copy.deepcopy(event.get_extra('dafeiyu_dialogue_evidence', {}))
        evidence['delivery_receipts']=[{k:r[k] for k in ('kind','at','media_types') if k in r}
            for r in event.get_extra('dafeiyu_confirmed_delivery',[]) if r.get('kind')=='reply'][-8:]
        review_epoch=await asyncio.to_thread(self.learning_store().context_epoch) if self.feature('learning_v2') else None
        turn = self.identity(event)
        # Recheck current rows after generation: a forgotten/corrected source is no longer evidence.
        current = await asyncio.to_thread(self.catalog(automatic=True).read, turn, time.time())
        existing = {row.id: row.data() for row in current.items if row.visible_to(turn)}
        evidence['memory'] = [row for row in evidence.get('memory', []) if existing.get(row.get('id')) == row]
        completed, progress = await self.own_activity()
        evidence['verified_self'] = self_state(completed, progress, await self.conversation_works(event))
        evidence['self_thoughts']=await self.own_thoughts()
        if not turn.is_group and turn.actor==str(self.natural().actor_owner()):
            meta=self.context.get_registered_star('dafeiyu_companion')
            companion=getattr(meta,'star_cls',None) if meta and meta.activated else None
            store=getattr(companion,'initiative',None)
            if store is not None:evidence['recent_own_shares']=await asyncio.to_thread(store.recent_sent)
        cross_session = audience and audience != event.unified_msg_origin
        if cross_session:
            evidence['memory'] = [row for row in evidence.get('memory', []) if row.get('visibility') == 'shared']
            evidence['historical_tool_results']=[]
            evidence['verified_self']['conversation_works']=[]
        history = event.get_extra('dafeiyu_dialogue_history', [])
        tools = event.get_extra('dafeiyu_dialogue_tool_evidence', [])
        packet = {'segments': segments, 'evidence': evidence, 'history': history, 'tools': tools, 'audience': audience}
        key = packet_key(packet)
        cache = event.get_extra('dafeiyu_dialogue_review_cache', {})
        if key in cache:
            self.review_stats['cache_hits'] += 1
            return cache[key]
        calls = event.get_extra('dafeiyu_dialogue_review_calls', 0)
        if calls >= 2:
            raise ValueError('Per-turn review budget exceeded')
        event.set_extra('dafeiyu_dialogue_review_calls', calls + 1)
        result = await review(segments, evidence,
            lambda prompt, policy: self.provider_call(event, prompt, policy, 'review'),
            human=self.natural().input_snapshot(event)['text'], history=history, tool_results=tools, audience=audience)
        if result.decision == 'revise':
            self.review_stats['revision_checks'] += 1
            verified = await verify_revision(result.segments, evidence,
                lambda prompt, policy: self.provider_call(event, prompt, policy, 'verification'),
                human=self.natural().input_snapshot(event)['text'], history=history, tool_results=tools,report=True)
            if not verified.supported:
                self.review_stats['revision_rejected'] += 1
                if event.get_extra('dafeiyu_dialogue_review_calls',0)>=2:
                    raise ValueError('Rewritten facts were not supported')
                event.set_extra('dafeiyu_dialogue_review_calls',calls+2)
                self.review_stats['revision_repair_attempts']+=1
                repair_materials=dict(evidence,rejected_revision={'violations':list(verified.violations)})
                result=await review(result.segments,repair_materials,
                    lambda prompt,policy:self.provider_call(event,prompt,policy,'review'),
                    human=self.natural().input_snapshot(event)['text'],history=history,tool_results=tools,audience=audience)
                self.review_stats['revision_checks']+=1
                supported=await verify_revision(result.segments,evidence,
                    lambda prompt,policy:self.provider_call(event,prompt,policy,'verification'),
                    human=self.natural().input_snapshot(event)['text'],history=history,tool_results=tools)
                if not supported:raise ValueError('Rewritten facts were not supported after bounded repair')
                self.review_stats['revision_repaired']+=1
        after_completed, after_progress = await self.own_activity()
        after_self=self_state(after_completed, after_progress, [] if cross_session else await self.conversation_works(event))
        if after_self != evidence['verified_self']:
            raise ValueError('Activity evidence changed during review')
        if await self.own_thoughts()!=evidence['self_thoughts']:raise ValueError('Reflection evidence changed during review')
        if (review_epoch is not None and review_epoch!=await asyncio.to_thread(self.learning_store().context_epoch)):
            raise ValueError('Conversation evidence changed during review')
        after = await asyncio.to_thread(self.catalog(automatic=True).read, turn, time.time())
        after_rows = {row.id: row.data() for row in after.items if row.visible_to(turn)}
        if any(after_rows.get(row['id']) != row for row in evidence['memory']):
            raise ValueError('Memory changed during review')
        self.review_stats[result.decision] += 1
        for kind in result.issue_kinds:
            self.review_stats[kind] += 1
        if mode == 'audit':
            result = ReviewResult(tuple(segments), 'audit')
        cache[key] = result
        event.set_extra('dafeiyu_dialogue_review_cache', cache)
        event.set_extra('dafeiyu_dialogue_last_review', result.decision)
        return result
