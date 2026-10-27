"""Explicit QQ group mentions only; answer by quoting the triggering message."""
import re
import sys

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Star, register
from astrbot.api.web import json_response
from astrbot.core.message.components import At, Image, Plain, Record, Reply
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

POLICY='_dafeiyu_group_reply_policy'
CAPTURE='dafeiyu_explicit_group_mention_v130'


@register('dafeiyu_mention_guard','Local','群聊仅真实@唤醒，引用原消息回复','1.3.0')
class DafeiyuMentionGuard(Star):
    def __init__(self,context):
        super().__init__(context)
        self.callback=self.outgoing_policy
        setattr(AiocqhttpMessageEvent,POLICY,self.callback)
        context.register_web_api('/dafeiyu_mention_guard/status',self.status,['GET'],'群聊触发与引用回复规则')

    async def terminate(self):
        if getattr(AiocqhttpMessageEvent,POLICY,None) is self.callback:
            delattr(AiocqhttpMessageEvent,POLICY)

    @staticmethod
    def applies(event):
        return (event.get_platform_name()=='aiocqhttp' and event.get_platform_id()=='dafeiyu-qq'
                and not event.is_private_chat())

    def allowed(self,event):
        settings=self.context.get_config(event.unified_msg_origin)['platform_settings']
        return not settings.get('enable_id_white_list') or str(event.get_group_id()) in {str(x) for x in settings.get('id_whitelist',[])}

    @staticmethod
    def explicitly_mentioned(event):
        saved=event.get_extra(CAPTURE,None)
        if saved is not None:return saved
        target=str(event.get_self_id())
        raw=getattr(event.message_obj,'raw_message',None)
        if callable(getattr(raw,'get',None)) and raw.get('post_type') not in (None,'message'):
            found=False
        elif callable(getattr(raw,'get',None)) and raw.get('message') is not None:
            parts=raw['message']
            if isinstance(parts,list):
                found=any(isinstance(p,dict) and p.get('type')=='at' and str((p.get('data') or {}).get('qq'))==target for p in parts)
            elif isinstance(parts,str):
                found=bool(re.search(r'\[CQ:at,qq='+re.escape(target)+r'(?:,[^\]]*)?\]',parts))
            else:found=False
        else:
            found=any(isinstance(p,At) and str(p.qq)==target for p in event.get_messages())
        event.set_extra(CAPTURE,found)
        return found

    @staticmethod
    def suppress(event):
        event.stop_event()
        state=event.get_extra('dafeiyu_reply_delivery',None)
        if state:
            state['expired']=True;state['admitted']=False;state['reply_expected']=False
        result=event.get_result()
        if result:result.chain=[]

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE,priority=sys.maxsize+100)
    async def require_real_mention(self,event:AstrMessageEvent):
        if not self.applies(event):return
        if not self.allowed(event) or not self.explicitly_mentioned(event):
            self.suppress(event);return
        event.set_extra('dafeiyu_group_addressed',True)
        event.is_at_or_wake_command=True;event.is_wake=True

    @filter.on_llm_request(priority=sys.maxsize+100)
    async def check_queued_request(self,event,request):
        await self.require_real_mention(event)

    def outgoing_policy(self,event,message):
        if not self.applies(event):return True
        raw=getattr(event.message_obj,'raw_message',None)
        if callable(getattr(raw,'get',None)) and raw.get('post_type')!='message':
            return True
        if not self.allowed(event) or not self.explicitly_mentioned(event):
            self.suppress(event);return False
        chain=[p for p in (getattr(message,'chain',[]) or []) if not isinstance(p,(At,Reply))]
        mid=str(getattr(event.message_obj,'message_id','') or '')
        if re.fullmatch(r'-?[1-9]\d*',mid) and any(isinstance(p,(Plain,Image,Record)) for p in chain):
            chain.insert(0,Reply(id=mid))
        message.chain=chain
        return True

    @filter.on_decorating_result(priority=-sys.maxsize)
    async def quote_sender(self,event:AstrMessageEvent):
        result=event.get_result()
        if result and result.chain:self.outgoing_policy(event,result)

    async def status(self):
        return json_response({'version':'1.3.0','group_trigger':'explicit_at_only','name_trigger':False,
            'quote_trigger':False,'followup_window_seconds':0,'reply_style':'quote_original_message',
            'send_policy_active':getattr(AiocqhttpMessageEvent,POLICY,None) is self.callback})
