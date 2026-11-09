"""Integrate relationship views with the existing generation and confirmed-delivery path."""
import asyncio
import json
from dafeiyu.dialogue.relationships import RelationshipMemory,POLICY,proposals


class RelationshipContext:
    def relationship_enabled(self):
        return (self.config.get('mode')=='active' and self.config.get('generation_contract',True)
            and self.feature('learning_v2') and self.feature('relationship_memory_v1'))

    def relationship_store(self):return RelationshipMemory(self.learning_store())

    async def relationship_profile(self,turn):
        if not self.relationship_enabled():return []
        return await asyncio.to_thread(self.relationship_store().current,turn.actor,turn.origin)

    async def prepare_relationship_context(self,event,request,turn):
        event.set_extra('dafeiyu_relationship_versions',{})
        event.set_extra('dafeiyu_relationship_pending',[])
        if not self.relationship_enabled():return
        rows=await self.relationship_profile(turn)
        # Compact subjective views are separate from factual recall and public material.
        offered=[];size=0
        for row in rows:
            cost=len(json.dumps(row,ensure_ascii=False))
            if size+cost>2400:continue
            offered.append(row);size+=cost
        event.set_extra('dafeiyu_relationship_versions',{r['id']:r['version'] for r in offered})
        event.get_extra('dafeiyu_dialogue_evidence',{})['relationship_memory']=offered
        packet=dict(enabled=True,scope='same_person_same_conversation',nature='subjective_revisable_views',notes=offered)
        request.system_prompt+='\n<!-- BEGIN_DAFEIYU_RELATIONSHIP -->\n'+POLICY+'\n'+json.dumps(packet,ensure_ascii=False).replace('<','\\u003c')+'\n<!-- END_DAFEIYU_RELATIONSHIP -->'

    def prepare_relationship_update(self,event,generated,human,privacy):
        event.set_extra('dafeiyu_relationship_pending',[])
        if (not self.relationship_enabled() or privacy['action']!='none'
                or event.get_extra('dafeiyu_learning_source',None) is None):return
        event.set_extra('dafeiyu_relationship_pending',proposals(generated.relationship,human))

    def commit_relationship_update(self,event,source):
        if not self.relationship_enabled() or not event.get_extra('dafeiyu_generation_valid',False):return 0
        receipts=event.get_extra('dafeiyu_confirmed_delivery',[])
        actual='\n'.join(dict.fromkeys(r['text'] for r in receipts if r.get('kind')=='reply' and r.get('text')))
        final=event.get_extra('dafeiyu_generation_final_text','') or ''
        if not actual or ''.join(actual.split())!=''.join(final.split()):return 0
        saved=self.relationship_store().commit(source,self.natural().input_snapshot(event)['text'],
            event.get_extra('dafeiyu_relationship_pending',[]),event.get_extra('dafeiyu_relationship_versions',{}),actual,confirmed=True)
        return len(saved)
