from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time
import uuid

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain, Record
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import json_response
from astrbot.core.agent.message import TextPart
from astrbot.core.message.message_event_result import ResultContentType
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .intent import IntentStore, request_key, FOLLOW, intent_kind
from .singing import SingingError, SingingGate, synthesize

OWNER='7000000001'
BOT='7000000002'


class DafeiyuSing(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config=config;self.gate=SingingGate()
        self.data=Path(get_astrbot_data_path())
        self.audio_dir=Path(StarTools.get_data_dir())/'audio'
        self.audio_dir.mkdir(parents=True,exist_ok=True);self.audio_dir.chmod(0o700)
        self.intents=IntentStore(self.audio_dir.parent/'intent.db')
        self._prune()
        context.register_web_api('/dafeiyu_sing/status',self.status,['GET'],'唱歌当前权限与请求衔接状态')

    def _prune(self):
        files=sorted(self.audio_dir.glob('song-*.wav'),key=lambda p:p.stat().st_mtime,reverse=True)
        used=0
        for index,path in enumerate(files):
            used+=path.stat().st_size
            if index>=6 or used>30*1024*1024:path.unlink(missing_ok=True)

    def _allowed(self,event):
        group=str(event.get_group_id() or '')
        if group:return group in {str(x) for x in self.config.get('allowed_groups',[])}
        return str(event.get_sender_id()) in {str(x) for x in self.config.get('allowed_private_users',[])}

    @staticmethod
    def _real(event):
        raw=getattr(event.message_obj,'raw_message',None)
        return (event.get_platform_name()=='aiocqhttp' and event.get_platform_id()=='dafeiyu-qq'
                and callable(getattr(raw,'get',None)) and raw.get('post_type')=='message'
                and str(raw.get('self_id'))==BOT and str(raw.get('user_id'))==str(event.get_sender_id())
                and not event.get_extra('cron_job',None) and not event.get_extra('cron_payload',None))

    @staticmethod
    def _text(event):
        captured=event.get_extra('jingjing_original_input',None)
        if isinstance(captured,dict):return str(captured.get('text',''))
        return ''.join(p.text for p in event.get_messages() if isinstance(p,Plain)).strip()

    def _names(self):
        from data.plugins.dafeiyu_natural_chat.continuity import verified_names
        allowed=self.context.get_config()['platform_settings'].get('id_whitelist',[])
        return verified_names(self.data,allowed)

    def _scope(self,event):
        return event.unified_msg_origin+'|'+str(event.get_sender_id())

    def _target(self,event,target_qq=''):
        actor=str(event.get_sender_id());text=self._text(event)
        if event.get_group_id() and not target_qq:return event.unified_msg_origin,''
        names=self._names();previous=self.intents.task(self._scope(event))
        matches={qq for qq,row in names.items() if qq!=actor and (row['name'] in text or re.search(r'(?<!\d)'+re.escape(qq)+r'(?!\d)',text))}
        target=str(target_qq or '').strip()
        own_explicit=bool(re.search(r'给我唱|唱给我|让我听|我自己听|在这[边里]唱',text))
        if own_explicit:
            if target and target!=actor:raise ValueError('本条消息要求在当前聊天听，本次不会发给其他人。')
            return event.unified_msg_origin,''
        if not target and actor==OWNER and len(matches)==1:target=next(iter(matches))
        continuation=bool(previous and (re.search(r'发过去|送过去|给她|给他|继续|再试|再唱一首',text) or FOLLOW.fullmatch(text.strip())))
        if not target and actor==OWNER and continuation:
            candidate=previous['target']
            if candidate.startswith('dafeiyu-qq:FriendMessage:'):target=candidate.rsplit(':',1)[-1]
        if not target:
            if len(matches)>1 or re.search(r'(?:给|向)(?!我|你|自己|一|个).{1,16}唱|唱.{0,16}(?:发|送)过去',text):
                raise ValueError('这首歌的收件人还没核准，本次没有生成或发送。请明确要在当前聊天听，还是发给哪位联系人。')
            return event.unified_msg_origin,''
        if not re.fullmatch(r'[1-9]\d{4,11}',target):raise ValueError('收件人QQ号不完整，这次没有生成或发送。')
        if target==actor:return event.unified_msg_origin,''
        if actor!=OWNER:raise ValueError('只能在当前会话唱歌；跨会话发送需要爸爸本人明确指定。')
        session='dafeiyu-qq:FriendMessage:'+target
        allowed={str(x) for x in self.context.get_config(session)['platform_settings'].get('id_whitelist',[])}
        if session not in allowed or target not in {str(x) for x in self.config.get('allowed_private_users',[])}:
            raise ValueError('对方还没有开放唱歌权限，本次没有生成或发送。')
        if matches and target not in matches:raise ValueError('收件人与本条消息指定的人不一致，本次没有生成或发送。')
        if target not in matches and not (previous and previous['target']==session and continuation):
            raise ValueError('本条消息没有确认这个收件人，本次没有生成或发送。')
        from data.plugins.dafeiyu_social.access import can_notify, profile
        if not can_notify(target):raise ValueError('对方现在关闭主动消息或处于免打扰时段，本次没有生成或发送。')
        if profile(target)['voice']=='text':raise ValueError('对方设置了文字优先，本次没有生成或发送歌声。')
        return session,names.get(target,{}).get('name','指定联系人')

    def _prepare_intent(self,event,req=None):
        existing=event.get_extra('dafeiyu_sing_intent',None)
        if existing is not None:return existing
        text=self._text(event)
        history=event.get_extra('jingjing_actual_chat',[]) or []
        last=next((row for row in reversed(history) if row.get('speaker')=='鲸鲸' and row.get('text')),None)
        offered=bool(last and time.time()-last.get('timestamp',0)<600 and re.search(r'想听哪首|唱哪首|给个主题|再唱个|再唱一|要不要.{0,10}唱|想听什么歌',last['text']))
        if not offered and req is not None:
            for row in reversed((getattr(req,'contexts',None) or [])[-4:]):
                if isinstance(row,dict) and row.get('role')=='assistant' and isinstance(row.get('content'),str):
                    offered=bool(re.search(r'想听哪首|唱哪首|给个主题|再唱个|再唱一|要不要.{0,10}唱|想听什么歌',row['content']))
                    break
        previous=self.intents.task(self._scope(event))
        kind=intent_kind(text,bool(previous),offered)
        if event.get_extra('dafeiyu_sing_command',False):kind='request'
        if kind!='request':
            self.intents.observe(self._scope(event),text,previous['target'] if previous else event.unified_msg_origin,offered)
            result={'kind':kind,'target':event.unified_msg_origin,'label':''}
            event.set_extra('dafeiyu_sing_intent',result)
            return result
        try:target,label=self._target(event)
        except ValueError as error:
            result={'kind':'target_error','target':event.unified_msg_origin,'label':'','reply':str(error)}
        else:
            kind=self.intents.observe(self._scope(event),text,target,offered)
            if event.get_extra('dafeiyu_sing_command',False):kind='request'
            result={'kind':kind,'target':target,'label':label}
        event.set_extra('dafeiyu_sing_intent',result)
        return result

    def _outcome(self,event,status,reply,**details):
        result={'operation':'sing','status':status,'reply':reply,**details}
        event.set_extra('dafeiyu_sing_outcome',result)
        return result

    @filter.on_llm_request(priority=-1000020)
    async def singing_capability(self,event,req):
        if not self._real(event):return
        enabled=self._allowed(event) and bool(self.config.get('enabled') and self.config.get('api_key'))
        event.set_extra('dafeiyu_sing_current_enabled',enabled)
        info=self._prepare_intent(event,req) if enabled else {'kind':'disabled'}
        block=('\n<!-- BEGIN_DAFEIYU_SING_CURRENT -->\n本轮唱歌权限与配置：'+('本会话已启用唱歌。' if enabled else '本会话目前没有开放或配置唱歌。')+
            '这是真实的当前配置，旧对话里的“未启用”不是当前状态；接口是否成功以本次工具结果为准，不提前说通道关闭。'
            'dafeiyu_sing 是实际歌声工具，普通TTS朗读不能当成唱歌。询问会不会唱可以直接答，明确要求唱才调用；'
            '紧接着选曲问题的“你自己选”“一首好听的”也是继续请求，后台会校验上下文。'
            '工具返回blocked_intent表示未识别到明确请求；cooldown表示尚未开始生成且在冷却，不能说唱到一半或通道关闭；'
            'sent表示已发出歌声，正常接住刚唱完的事，不突然说“在呢，接下来聊点啥”。'
            '不要在调用前发一段猜测成败的前言；生成开始后工具会发送真实进度。给别人唱时必须使用真实收件人，不能发到爸爸当前窗口充数。')
        if info.get('kind')=='request':
            block+='\n本轮人类唱歌请求已确认，目标会话：'+info['target']+'。需要写词时写短原创歌词，然后实际调用一次dafeiyu_sing。'
            # Keep the current action adjacent to the current user turn. Old
            # failed attempts in a long transcript must not turn it into a promise.
            marker='【本轮已确认的唱歌请求】'
            parts=req.extra_user_content_parts
            parts[:]=[p for p in parts if not isinstance(p,TextPart) or not p.text.startswith(marker)]
            parts.append(TextPart(text=marker+'本人这句“'+self._text(event)+'”是在要求现在唱；若是选曲补充，已由当前会话记录确认。'
                '目标是 '+info['target']+'。请写2到160字短原创歌词，立即调用一次 dafeiyu_sing(lyrics, style, target_qq)，'
                '不要只回复“我去找”“等下唱”或询问已经交给你选择的曲目。实际发送结果以工具返回为准。'))
            event.set_extra('dafeiyu_sing_action_requested',True)
        if info.get('kind')=='target_error':block+='\n目标校验结果：'+info['reply']
        req.system_prompt=re.sub(r'\n<!-- BEGIN_DAFEIYU_SING_CURRENT -->.*?<!-- END_DAFEIYU_SING_CURRENT -->','',req.system_prompt or '',flags=re.S)+block+'\n<!-- END_DAFEIYU_SING_CURRENT -->'

    async def _sing(self,event,lyrics,style='',target_qq=''):
        if not self._real(event):return self._outcome(event,'blocked_intent','这不是本人的真实QQ唱歌请求，本次没有生成或发送。')
        if not self._allowed(event):return self._outcome(event,'disabled_scope','这个会话还没有开放唱歌权限。')
        if not self.config.get('enabled') or not self.config.get('api_key'):
            return self._outcome(event,'disabled_service','唱歌服务还没有完成启用配置。')
        info=self._prepare_intent(event)
        if info['kind']=='target_error':return self._outcome(event,'target_unresolved',info['reply'])
        if info['kind']!='request':return self._outcome(event,'blocked_intent','这句还没有确认要开始唱歌，我先不生成。想让我唱时，直接说“唱首歌”就行；这不是通道关闭。')
        try:target,label=self._target(event,target_qq)
        except ValueError as error:return self._outcome(event,'target_unresolved',str(error))
        lyrics=str(lyrics or '').strip();maximum=max(2,min(int(self.config.get('max_lyrics_chars',160)),300))
        if not 2<=len(lyrics)<=maximum:return self._outcome(event,'invalid_lyrics','歌词需要在 2～'+str(maximum)+' 字之间，这次还没有生成。')
        key=request_key(event,target)
        if not key:return self._outcome(event,'blocked_intent','这条请求的消息编号不完整，本次没有生成或发送。')
        previous=self.intents.reserve(key,target,int(self.config.get('cooldown_seconds',90)))
        if previous:
            status=previous['status'];reply=previous['reply']
            if status=='sent':reply='这条请求的歌声已经发过，先不重复生成或发送。'
            return self._outcome(event,status,reply,target=target)
        outcome=None
        try:
            async with self.gate.enter(target,int(self.config.get('cooldown_seconds',90))):
                await event.send(MessageChain(chain=[Plain('我来唱一小段，等我一下。')]))
                try:
                    audio,duration=await synthesize(self.config['api_key'],lyrics,self.config.get('voice','茉莉'),style,int(self.config.get('timeout_seconds',75)))
                except SingingError as error:outcome=self._outcome(event,'generation_failed',str(error),target=target)
                else:
                    path=self.audio_dir/('song-'+uuid.uuid4().hex+'.wav')
                    descriptor=os.open(str(path),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                    with os.fdopen(descriptor,'wb') as output:output.write(audio)
                    self._prune()
                    chain=MessageChain(chain=[Record.fromFileSystem(str(path))])
                    try:
                        if target==event.unified_msg_origin:await event.send(chain)
                        elif not await self.context.send_message(target,chain):raise RuntimeError('No target platform')
                    except Exception:outcome=self._outcome(event,'delivery_unknown','歌声生成了，但这次发送没有确认成功，我先不重复发送。',target=target)
                    else:
                        reply='唱好啦，已经把这段歌发给'+label+'了。' if label else '唱好啦，这段已经发出来了。'
                        outcome=self._outcome(event,'sent',reply,target=target,duration_seconds=round(duration,1))
                        logger.info('Singing sent target=%s duration=%.1fs bytes=%s',target,duration,len(audio))
        except SingingError as error:outcome=self._outcome(event,'busy',str(error),target=target)
        except BaseException as error:
            outcome=self._outcome(event,'generation_unknown','这次唱歌处理没有确认完成，我先不自动重试。',target=target)
            self.intents.finish(key,outcome)
            if not isinstance(error,Exception):raise
            logger.warning('Singing operation unconfirmed: %s',type(error).__name__)
        self.intents.finish(key,outcome)
        return outcome

    @filter.on_llm_response(priority=sys.maxsize-80)
    async def ground_singing_result(self,event,response):
        if 'dafeiyu_sing' in (getattr(response,'tools_call_name',None) or []):
            response.completion_text='';return
        outcome=event.get_extra('dafeiyu_sing_outcome',None)
        if outcome and not getattr(response,'tools_call_name',None):response.completion_text=outcome['reply']
        elif event.get_extra('dafeiyu_sing_action_requested',False) and not getattr(response,'tools_call_name',None):
            response.completion_text='这次还没有开始生成歌声，刚才只回了话。'
        elif event.get_extra('dafeiyu_sing_current_enabled',None) is not None and re.fullmatch(r'(?:现在|你|这边|这里)?(?:会|能|可以)唱(?:歌)?了?吗[？?。\s]*',self._text(event)):
            response.completion_text='能，已经可以唱了。' if event.get_extra('dafeiyu_sing_current_enabled') else '这个会话目前还没有开放唱歌。'

    @filter.on_decorating_result(priority=sys.maxsize-70)
    async def final_singing_status(self,event):
        outcome=event.get_extra('dafeiyu_sing_outcome',None);result=event.get_result()
        if not outcome or not result or not result.is_llm_result():return
        result.chain=[p for p in result.chain if isinstance(p,At)]+[Plain(outcome['reply'])]
        result.result_content_type=ResultContentType.GENERAL_RESULT

    @filter.command('唱歌',alias={'sing'})
    async def sing_command(self,event:AstrMessageEvent):
        if not self._real(event):return
        event.set_extra('dafeiyu_sing_command',True)
        raw=re.sub(r'^\s*/?(?:唱歌|sing)(?:@\S+)?(?:\s+|$)','',self._text(event),count=1,flags=re.I).strip()
        if not raw:
            yield event.plain_result('用法：/唱歌 歌词。每次最多160字，也可以让我写一小段再唱。');return
        result=await self._sing(event,raw)
        yield event.plain_result(result['reply'])

    @filter.llm_tool(name='dafeiyu_sing')
    async def sing_tool(self,event:AstrMessageEvent,lyrics:str,style:str='',target_qq:str='')->str:
        """按真实人类请求生成一段原创女声歌声，发送后返回实际结果。不是普通语音朗读。

        用户明确让唱，或紧接选曲问题说“你自己选”“一首好听的”时可调用一次。
        只问会不会唱不调用。失败不能自动重试，不能把冷却或意图未识别说成通道关闭。

        Args:
            lyrics(string): 2到160字原创歌词，不含动作说明。
            style(string): 简短演唱风格。
            target_qq(string): 默认当前会话。仅爸爸明确要求给已开放联系人唱歌时填该人的真实QQ，不能猜收件人。
        """
        return json.dumps(await self._sing(event,lyrics,style,target_qq),ensure_ascii=False)

    async def status(self):
        return json_response({'version':'0.1.2','enabled':bool(self.config.get('enabled') and self.config.get('api_key')),
            'allowed_private_users':self.config.get('allowed_private_users',[]),'allowed_groups':self.config.get('allowed_groups',[]),
            'scoped_followup_intent':True,'durable_single_attempt':True,'grounded_tool_results':True})
