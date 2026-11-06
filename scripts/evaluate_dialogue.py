"""Run synthetic, connected conversations through native hooks and a supplied model.

The caller supplies async call(system, history, human) -> {text, usage, elapsed_ms}.
No provider credentials or transports live here; QQ/tool sends are unavailable.
Results are review material, never an automatic naturalness pass or a human score.
"""
import copy
import gc
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'framework'),str(ROOT/'plugins'),str(ROOT/'tests')]
from dialogue_scenarios import scenarios


async def evaluate(call, case_ids, progress=lambda value:None, checkpoint=lambda value:None,
                   variants=('baseline','candidate'), split='dev',candidate_config=None):
    import data.plugins
    data.plugins.__path__=[str(ROOT/'plugins'),*list(data.plugins.__path__)]
    natural=importlib.import_module('data.plugins.dafeiyu_natural_chat.main')
    dialogue=importlib.import_module('data.plugins.dafeiyu_dialogue.main')
    from astrbot.core.provider.entities import ProviderRequest
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
    from astrbot.core.platform.astrbot_message import AstrBotMessage,MessageMember
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from astrbot.core.message.components import Plain
    from astrbot.core.agent.message import Message
    owner,bot='7000000001','7000000002'
    seed=(ROOT/'persona/live.md').read_text(encoding='utf-8')
    if split not in ('dev','hidden'):raise ValueError('Invalid scenario split')
    cases={case['id']:case for case in scenarios(split)}
    if any(key not in cases for key in case_ids):raise ValueError('Case does not belong to the selected split')
    if any(v not in ('baseline','candidate') for v in variants):raise ValueError('Invalid evaluation variant')
    report={'schema_version':2,'split':split,'real_qq_messages':0,'real_tools':0,'production_memory_writes':0,
        'naturalness_verdict':'pending_human_review','group_plugin_included':False,'cases':[]}
    for key in case_ids:
        case=cases[key];result={'id':key,'family':case['family'],'rubric':case['rubric'],'variants':{}}
        report['cases'].append(result)
        for variant in variants:
            with tempfile.TemporaryDirectory() as tmp:
                auxiliary=[]
                async def auxiliary_call(**kwargs):
                    raw=await call(kwargs['system_prompt'],kwargs['contexts'],kwargs['prompt'])
                    auxiliary.append(raw)
                    usage=raw.get('usage') or {}
                    return NS(completion_text=raw['text'],usage=NS(
                        input_other=usage.get('prompt_tokens',0)-usage.get('prompt_cache_hit_tokens',0),
                        input_cached=usage.get('prompt_cache_hit_tokens',0),output=usage.get('completion_tokens',0)))
                context=NS(register_web_api=lambda *x:None,persona_manager=NS(personas=[NS(system_prompt=seed)]),
                    get_using_provider_async=AsyncMock(return_value=NS(text_chat=auxiliary_call)),get_config=lambda *x:{'platform_settings':{
                    'enable_id_white_list':True,'id_whitelist':[owner]}},get_platform=lambda *x:None)
                with patch.object(natural,'get_astrbot_data_path',return_value=tmp):
                    n=natural.NaturalChat(context)
                context.get_registered_star=lambda name:NS(activated=True,star_cls=n) if name=='dafeiyu_natural_chat' else None
                with patch.object(dialogue,'get_astrbot_data_path',return_value=tmp):
                    d=dialogue.Dialogue(context,{'mode':'active' if variant=='candidate' else 'off','active_private_users':[owner],
                        **(candidate_config or {'generation_contract':True,'learning_v2':True,'retrieval_index_v2':True})})
                history=[];turns=[]
                result['variants'][variant]=turns
                for index,human in enumerate(case['turns']):
                    turn_started=time.perf_counter()
                    auxiliary_start=len(auxiliary)
                    msg=AstrBotMessage();msg.type=MessageType.FRIEND_MESSAGE;msg.self_id=bot;msg.session_id=owner
                    msg.group_id='';msg.message_id=variant+str(index);msg.sender=MessageMember(owner,'爸爸')
                    msg.message=[Plain(human)];msg.message_str=human
                    msg.raw_message={'post_type':'message','self_id':bot,'user_id':owner,'time':time.time()}
                    e=AiocqhttpMessageEvent(human,msg,PlatformMetadata('aiocqhttp','evaluation','dafeiyu-qq'),owner,object());e.send=AsyncMock()
                    req=ProviderRequest(prompt=human,system_prompt=seed,contexts=copy.deepcopy(history))
                    await d.choose_path(e,req);await n.prepare(e,req);await n.natural_delivery_context(e,req)
                    await n.prepare_spoken_style(e,req);await d.prepare(e,req)
                    raw=await call(req.system_prompt,req.contexts,human)
                    response=NS(completion_text=raw['text'],tools_call_name=[])
                    await n.conversational_response(e,response);await n.ground_current_facts(e,response)
                    await d.review_response(e,response)
                    delivered=response.completion_text
                    native=NS(messages=[Message.model_validate(m) for m in [*req.contexts,
                        {'role':'user','content':human},{'role':'assistant','content':raw['text']} ]])
                    await n.save_grounded_reply(e,native,response)
                    await d.save_native_context(e,native,response)
                    # Simulated confirmation writes only this variant's temporary exchange DB.
                    e.set_extra('dafeiyu_confirmed_delivery',[{'kind':'reply','text':delivered,'at':time.time()}])
                    await n.remember_delivered_reply(e)
                    await d.commit_learning(e)
                    history=[m.model_dump() for m in native.messages]
                    turns.append({'human':human,'raw':raw['text'],'delivered':delivered,
                        'elapsed_ms':raw.get('elapsed_ms'),'usage':raw.get('usage'),
                        'turn_wall_ms':round((time.perf_counter()-turn_started)*1000,2),
                        'review':e.get_extra('dafeiyu_dialogue_last_review','inactive'),
                        'failure_kind':e.get_extra('dafeiyu_generation_failure_kind',None),
                        'framing_recovered':e.get_extra('dafeiyu_generation_framing_recovered',False),
                        'learning_pending':e.get_extra('dafeiyu_learning_pending',[]),
                        'learning_counts':d._learning.counts() if d._learning is not None else None,
                        'learned_state':d._learning.current(owner,e.unified_msg_origin) if d._learning is not None else None,
                        'expression':e.get_extra('jingjing_expression_intent',None),
                        'focus':e.get_extra('dafeiyu_semantic_focus',[]),
                        'privacy_action':e.get_extra('dafeiyu_privacy_action','none'),
                        'auxiliary':copy.deepcopy(auxiliary[auxiliary_start:]),
                        'postprocessor_changed':raw['text']!=delivered,'prompt_characters':len(req.system_prompt)})
                    progress({'case':key,'variant':variant,'turn':index+1})
                    checkpoint(report)
                del n,d,context;gc.collect()
    return report
