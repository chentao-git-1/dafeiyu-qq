"""AstrBot orchestration for growth lifecycle.

Business rules live in dafeiyu; event registration remains in main.py.
"""
import asyncio
from astrbot.api import logger
from dafeiyu.dialogue.growth import GrowthStore,GLOBAL,own_experiences,preference_experiences,relation,perspective
from dafeiyu.dialogue.growth_policy import assess as assess_growth


class GrowthLifecycle:
    def growth_enabled(self):
        return self.config.get('mode')=='active' and self.config.get('generation_contract',True) and self.feature('personality_growth_v1')

    def growth_store(self):
        if self._growth is None:self._growth=GrowthStore(self.root/'plugin_data/dafeiyu_dialogue/personality_growth.db')
        return self._growth

    async def sync_growth(self,scope=GLOBAL,owner='',origin=''):
        growth=self.growth_store()
        epoch=await asyncio.to_thread(growth.epoch,scope,owner,origin)
        retained=await asyncio.to_thread(growth.retained,scope)
        if scope==GLOBAL:
            meta=self.context.get_registered_star('dafeiyu_life')
            life=getattr(meta,'star_cls',None) if meta and meta.activated else None
            rows=await asyncio.to_thread(own_experiences,getattr(life,'store',None),retained=retained)
        else:
            rows=await asyncio.to_thread(preference_experiences,self.learning_store(),owner,origin,retained=retained) if self.feature('learning_v2') else []
        return await asyncio.to_thread(growth.reconcile,scope,rows,owner,origin,epoch)

    async def growth_profile(self,turn=None):
        if not self.growth_enabled():return {}
        try:
            result={'global':perspective(await self.sync_growth())}
            if turn is not None and not turn.is_group and self.feature('learning_v2'):
                result['with_current_person']=perspective(await self.sync_growth(relation(turn.actor,turn.origin),turn.actor,turn.origin))
            return result
        except asyncio.CancelledError:raise
        except Exception as error:
            self.review_stats['personality_growth_context_failed']+=1
            logger.warning('Personality context unavailable: %s',type(error).__name__)
            return {}

    async def review_growth(self):
        if not self.growth_enabled() or self.growth_lock.locked():return 0
        async with self.growth_lock:
            from dafeiyu.memory.self_profile import identity_profile
            await self.sync_growth();store=self.growth_store();changed=0
            scopes=await asyncio.to_thread(store.scopes)
            for scope in sorted(scopes,key=lambda r:r['scope']!=GLOBAL):
                if not self.growth_enabled():break
                await self.sync_growth(scope['scope'],scope['owner'],scope['origin'])
                natural=self.natural()
                if scope['scope']==GLOBAL and natural is None:continue
                origin=scope['origin'] or 'dafeiyu-qq:FriendMessage:'+str(natural.actor_owner())
                provider=await self.context.get_using_provider_async(origin)
                if provider is None:continue
                claim=await asyncio.to_thread(store.claim,scope['scope'])
                if not claim:continue
                try:
                    claim['packet']['stable_profile']=identity_profile()
                    async def call(prompt,policy):
                        response=await self.metered_provider(provider,'personality_growth').text_chat(
                            prompt=prompt,system_prompt=policy,contexts=[],func_tool=None,request_max_retries=1)
                        return response.completion_text
                    proposal,accepted=await assess_growth(claim['packet'],call)
                    if not self.growth_enabled():raise ValueError('Personality growth disabled during consolidation')
                    await self.sync_growth(scope['scope'],scope['owner'],scope['origin'])
                    changed+=int(await asyncio.to_thread(store.finish,claim,proposal,accepted))
                except asyncio.CancelledError:raise
                except Exception as error:
                    await asyncio.to_thread(store.finish,claim,error=type(error).__name__)
                    self.review_stats['personality_growth_evaluation_failed']+=1
                    logger.warning('Personality consolidation unavailable: %s',type(error).__name__)
            return changed

    async def growth_loop(self):
        await asyncio.sleep(30)
        while True:
            try:await self.review_growth()
            except asyncio.CancelledError:raise
            except Exception as error:logger.warning('Personality background unavailable: %s',type(error).__name__)
            await asyncio.sleep(600)
