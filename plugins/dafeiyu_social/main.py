"""User-controlled chat preferences, bounded bot conversations and calendar notices."""
import asyncio
import contextlib
import datetime as dt
import re
import shlex
import sqlite3
import sys
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import filter, MessageChain
from astrbot.api.star import Star
from astrbot.api.web import json_response
from astrbot.core.message.components import At, Image, Plain, Record, Reply
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from .access import store
from .store import DEFAULT, OWNER, SELF, TZ, can_notify_profile, clock_minutes
from .cards import render_card

QUOTED = re.compile(r'```.*?```|`[^`]*`|“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"',re.S)


def setting_change(text):
    raw=str(text or '').strip()
    command=raw.lstrip('/')
    if command in ('我的设置','聊天设置','聊天设置 查看'): return 'show',{}
    if command=='聊天设置 恢复默认': return 'set',dict(DEFAULT)
    if command.startswith('聊天设置 '):
        value=command[5:].strip()
        modes={'文字':{'voice':'text'},'语音':{'voice':'voice'},'默认语音':{'voice':'auto'},
               '表情 关':{'memes':'off'},'表情 少':{'memes':'few'},'表情 默认':{'memes':'auto'},
               '主动消息 关':{'proactive':False},'主动消息 开':{'proactive':True},
               '免打扰 关':{'quiet_start':None,'quiet_end':None}}
        if value in modes:return 'set',modes[value]
        match=re.fullmatch(r'免打扰\s+(\d{2}:\d{2})\s*[-—至到]\s*(\d{2}:\d{2})',value)
        if match:return 'set',{'quiet_start':clock_minutes(match[1]),'quiet_end':clock_minutes(match[2])}
        raise ValueError('用法：/聊天设置 文字、语音、表情 少、免打扰 23:00-08:00、主动消息 关。')
    clean=QUOTED.sub('',raw).strip('。！!，, ')
    if re.fullmatch(r'(?:你)?(?:以后|今后)?(?:你)?(?:请)?(?:别|不要|不用)(?:再|老|总)?主动(?:来)?(?:找我|联系我|给我发(?:消息|信息)|发消息给我)?(?:了|吧|啦)?',clean):
        return 'set',{'proactive':False}
    if re.fullmatch(r'(?:你)?(?:以后|今后)?(?:你)?(?:可以|允许|恢复|继续)(?:再)?主动(?:来)?(?:找我|联系我|给我发(?:消息|信息))?(?:了|吧|啦)?',clean):
        return 'set',{'proactive':True}
    natural={
        '以后只给我发文字':{'voice':'text'}, '以后只对我发文字':{'voice':'text'},
        '以后别给我发语音':{'voice':'text'}, '以后用语音回复我':{'voice':'voice'},
        '以后少给我发表情':{'memes':'few'}, '以后少发表情':{'memes':'few'},
        '以后别给我发表情':{'memes':'off'}, '以后不要主动找我':{'proactive':False},
        '以后别主动找我':{'proactive':False}, '别主动找我':{'proactive':False},
        '以后可以主动找我':{'proactive':True},
        '晚上别主动找我':{'quiet_start':1380,'quiet_end':480},
    }
    return ('set',natural[clean]) if clean in natural else (None,{})


def profile_text(profile):
    voice={'auto':'默认比例','text':'文字优先','voice':'语音优先'}[profile['voice']]
    memes={'auto':'默认','few':'少量（最多五分钟一次）','off':'关闭自动表情'}[profile['memes']]
    quiet='未设置'
    if profile['quiet_start'] is not None:
        quiet='{:02d}:{:02d}–{:02d}:{:02d}'.format(profile['quiet_start']//60,profile['quiet_start']%60,profile['quiet_end']//60,profile['quiet_end']%60)
    return '你的聊天设置：\n回复：{}\n表情：{}\n主动消息：{}\n免打扰：{}（北京时间）'.format(voice,memes,'开启' if profile['proactive'] else '关闭',quiet)


class DafeiyuSocial(Star):
    def __init__(self,context):
        super().__init__(context)
        self.db=store()
        self.task=None
        self.data=Path(get_astrbot_data_path())
        self.stats={'bot_allowed':0,'bot_suppressed':0,'calendar_sent':0,'calendar_unknown':0,'meme_suppressed':0}
        context.register_web_api('/dafeiyu_social/status',self.status,['GET'],'聊天偏好、机器人互聊及纪念日状态')

    async def initialize(self):
        self.task=asyncio.create_task(self.calendar_loop())

    async def terminate(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.task

    def real_event(self,event):
        raw=getattr(event.message_obj,'raw_message',None)
        return (event.get_platform_id()=='dafeiyu-qq' and callable(getattr(raw,'get',None))
                and raw.get('post_type')=='message' and str(raw.get('self_id'))==SELF
                and str(raw.get('user_id'))==str(event.get_sender_id())
                and not event.get_extra('cron_job',None) and not event.get_extra('cron_payload',None))

    def allowed_private(self,event):
        if not self.real_event(event) or not event.is_private_chat():return False
        cfg=self.context.get_config(event.unified_msg_origin)['platform_settings']
        return event.unified_msg_origin in {str(x) for x in cfg.get('id_whitelist',[])}

    @filter.event_message_type(filter.EventMessageType.ALL,priority=sys.maxsize+200)
    async def bot_guard(self,event):
        if not self.real_event(event) or event.is_private_chat():return
        group=str(event.get_group_id())
        cfg=self.context.get_config(event.unified_msg_origin)['platform_settings']
        if group not in {str(x) for x in cfg.get('id_whitelist',[])}:return
        parts=event.get_messages()
        addressed=any(isinstance(p,At) and str(p.qq)==SELF or isinstance(p,Reply) and str(p.sender_id)==SELF
                      or isinstance(p,Plain) and any(n in p.text for n in ('鲸鲸','大肥鱼')) for p in parts)
        uid=str(event.get_sender_id())
        if uid not in self.db.bots():
            if addressed:self.db.human_turn(group)
            return
        if not addressed:
            event.stop_event();self.stats['bot_suppressed']+=1;return
        turn=str(getattr(event.message_obj,'message_id',None) or event.message_obj.raw_message.get('message_id') or event.message_str)
        allowed,reason=self.db.bot_turn(group,uid+':'+turn)
        if not allowed:
            self.stats['bot_suppressed']+=1
            event.stop_event()
        else:self.stats['bot_allowed']+=1

    def contacts(self):
        names={OWNER:'爸爸','7000000011':'猛叔叔','7000000012':'田阿姨','7000000013':'定叔叔'}
        path=self.data/'plugin_data/astrbot_plugin_memory_system/memory_system.db'
        if path.exists():
            try:
                with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1) as db:
                    rows=db.execute('SELECT content FROM memories WHERE user_id=? AND merged_into IS NULL ORDER BY id LIMIT 1000',(OWNER,)).fetchall()
                for (content,) in rows:
                    match=re.fullmatch(r'QQ\s*([1-9]\d{4,11})\s*(?:被称作|被称为|称为|叫作|叫)\s*([^\s。.!！?？,，:：;；]{1,16})[。.]?',str(content).strip())
                    if match:names.setdefault(match[1],match[2])
            except sqlite3.Error:pass
        allowed={str(x) for x in self.context.get_config()['platform_settings'].get('id_whitelist',[])}
        return {qq:name for qq,name in names.items() if 'dafeiyu-qq:FriendMessage:'+qq in allowed}

    def resolve(self,actor,label,event):
        if label in ('我','我的','自己'):
            return actor,event.get_sender_name()
        if actor!=OWNER:raise ValueError('只能设置自己的日期；请用“我”。')
        names=self.contacts()
        matches=[qq for qq,name in names.items() if label in (qq,name)]
        if len(matches)!=1:raise ValueError('请使用已确认的称呼或私信白名单中的 QQ 号。')
        return matches[0],names[matches[0]]

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE,priority=sys.maxsize+100)
    async def controls(self,event):
        if not self.allowed_private(event):return
        uid=str(event.get_sender_id())
        text=''.join(p.text for p in event.get_messages() if isinstance(p,Plain)).strip()
        command=text.lstrip('/')
        try:
            intent,changes=setting_change(text)
            if intent:
                result=self.db.set_profile(uid,changes) if intent=='set' else self.db.profile(uid)
                await event.send(MessageChain(chain=[Plain(profile_text(result))]));event.stop_event();return
            if command.split(' ',1)[0] in ('机器人名单','机器人标记','机器人解除'):
                if uid!=OWNER:raise ValueError('只有爸爸能管理机器人名单。')
                args=shlex.split(command)
                if args[0]=='机器人名单':reply='机器人名单：\n'+('\n'.join(qq+' '+name for qq,name in self.db.bots().items()) or '暂未标记。')
                elif args[0] in ('机器人标记','机器人解除') and len(args)>=2:
                    self.db.set_bot(uid,args[1],' '.join(args[2:]),remove=args[0]=='机器人解除')
                    reply='机器人标记已更新。互聊每组最多三轮，之后冷却三十分钟；真人叫鲸鲸会重新接话。'
                else:raise ValueError('用法：/机器人名单、/机器人标记 QQ 名称、/机器人解除 QQ。')
                await event.send(MessageChain(chain=[Plain(reply)]));event.stop_event();return
            natural=re.fullmatch(r'记住(?:我的|(.{1,16})的)生日(?:是|在)?\s*((?:\d{4}年)?\d{1,2}月\d{1,2}日)[。！!]?',text)
            if natural:
                label=natural[1] or '我'
                date=natural[2].replace('年','-').replace('月','-').replace('日','')
                command='生日 设置 '+label+' '+date
            if command.split(' ',1)[0] not in ('生日','纪念日','生日卡','纪念卡'):return
            if '农历' in command:raise ValueError('当前按公历提醒，请提供公历的月日。')
            args=shlex.split(command)
            if args[0] in ('生日卡','纪念卡'):
                if len(args)<2:raise ValueError('用法：/生日卡 记录编号 [祝福语]。先用 /生日 列表 查看编号。')
                row=self.db.get_date(uid,int(args[1]))
                if row is None:raise ValueError('没有可查看的这个日期记录。')
                path=await asyncio.to_thread(render_card,row,self.data/'plugin_data/dafeiyu_social/cards',' '.join(args[2:]))
                await event.send(MessageChain(chain=[Image.fromFileSystem(str(path))]));event.stop_event();return
            if len(args)==1 or args[1]=='列表':
                rows=self.db.dates(uid)
                reply='日期记录（公历）：\n'+('\n'.join('{}：{} {} {:02d}-{:02d} {}'.format(r['id'],r['name'],'生日' if r['kind']=='birthday' else '纪念日',r['month'],r['day'],r['title']) for r in rows) or '还没有记录。')
            elif args[1]=='删除' and len(args)==3:
                self.db.delete_date(uid,int(args[2]));reply='已删除这条日期及之后的提醒。'
            elif args[1]=='设置':
                kind='birthday' if args[0]=='生日' else 'anniversary'
                rest=args[2:]
                if not rest:raise ValueError('请提供日期，例如 /生日 设置 03-21。')
                label='我' if re.fullmatch(r'(?:\d{4}-)?\d{1,2}-\d{1,2}',rest[0]) else rest.pop(0)
                target,name=self.resolve(uid,label,event)
                date=rest[0] if rest else ''
                if not re.fullmatch(r'(?:\d{4}-)?\d{1,2}-\d{1,2}',date):raise ValueError('日期请用 03-21 或 2000-03-21。')
                numbers=list(map(int,date.split('-')))
                year=numbers[0] if len(numbers)==3 else None
                row=self.db.set_date(uid,target,name,kind,numbers[-2],numbers[-1],year,' '.join(rest[1:]))
                reply='记下了，编号 {}：{} {:02d}-{:02d}{}。按公历在前一天和当天白天提醒爸爸；2 月 29 日在平年按 2 月 28 日提醒。'.format(row['id'],row['name'],row['month'],row['day'],' '+row['title'] if row['title'] else ' 生日')
            else:raise ValueError('用法：/生日 设置 03-21；/纪念日 设置 10-26 认识鲸鲸；/生日 列表；/生日 删除 编号。')
            await event.send(MessageChain(chain=[Plain(reply)]));event.stop_event()
        except (ValueError,IndexError) as error:
            await event.send(MessageChain(chain=[Plain(str(error) or '参数不完整，请检查命令。')]))
            event.stop_event()

    @filter.on_llm_request(priority=100000)
    async def preference_context(self,event,request):
        if event.get_platform_id()!='dafeiyu-qq':return
        p=self.db.profile(str(event.get_sender_id()))
        request.system_prompt=(request.system_prompt or '')+'\n【当前真实发言者的聊天偏好】'+profile_text(p)+'。这些设置只属于当前 QQ，不能套给其他人。'
        if p['memes']=='off':request.system_prompt+=' 本轮用正常文字表达，不用自动表情标签代替回答。'
        if not p['proactive']:request.system_prompt+=' 本人已关闭主动消息，不创建自动关心回访；本人明确安排的定时提醒照常处理。'

    @filter.on_decorating_result(priority=99998)
    async def filter_automatic_memes(self,event):
        if event.get_platform_id()!='dafeiyu-qq':return
        automatic=event.get_extra('dafeiyu_automatic_meme_components',[]) or []
        pending=event.get_extra('meme_manager_pending_images',[]) or []
        if not automatic and not pending:return
        p=self.db.profile(str(event.get_sender_id()))
        if self.db.allow_meme(str(event.get_sender_id()),p['memes']):return
        result=event.get_result()
        if result:
            result.chain=[part for part in result.chain if not any(part is image for image in automatic)]
            if not result.chain:result.chain=[Plain('嗯，我在。')]
        event.set_extra('meme_manager_pending_images',None)
        event.set_extra('found_emotions',None)
        event.set_extra('meme_manager_semantic_selected_ids',None)
        self.stats['meme_suppressed']+=1

    @filter.on_using_llm_tool(priority=sys.maxsize-30)
    async def outgoing_policy(self,event,tool,arguments):
        if event.get_platform_id()!='dafeiyu-qq' or getattr(tool,'name','')!='send_message_to_user' or not isinstance(arguments,dict):return
        target=arguments.get('session') or event.unified_msg_origin
        match=re.fullmatch(r'dafeiyu-qq:FriendMessage:([1-9]\d{4,11})',str(target))
        if not match:return
        uid=match[1]
        own=uid==str(event.get_sender_id()) and self.real_event(event)
        p=self.db.profile(uid)
        payload=event.get_extra('cron_payload',{}) or {}
        explicit_reminder=payload.get('dafeiyu_companion') and payload.get('dafeiyu_kind')=='reminder'
        if not own and not explicit_reminder and not can_notify_profile(p,time.time()):
            arguments['messages']=[]
            event.set_extra('dafeiyu_preference_send_note','对方现在关闭了主动消息或处于免打扰时段，本次没有发送。')

    @filter.on_llm_tool_respond()
    async def preference_tool_result(self,event,tool,arguments,result):
        note=event.get_extra('dafeiyu_preference_send_note',None)
        if note and getattr(tool,'name','')=='send_message_to_user' and result is not None:
            from mcp.types import TextContent
            result.content.append(TextContent(type='text',text=note))
            event.set_extra('dafeiyu_preference_send_note',None)

    async def check_calendar(self):
        now=time.time()
        if not can_notify_profile(self.db.profile(OWNER),now):return
        session='dafeiyu-qq:FriendMessage:'+OWNER
        allowed={str(x) for x in self.context.get_config(session)['platform_settings'].get('id_whitelist',[])}
        if session not in allowed:return
        platform=self.context.get_platform('aiocqhttp')
        bot=getattr(platform,'bot',None)
        if platform is None or platform.meta().id!='dafeiyu-qq' or bot is None:return
        actions=self.db.due_dates(now)
        if not actions:return
        try:
            online=await asyncio.wait_for(bot.get_status(),timeout=8)
            if online.get('online') is not True:return
        except Exception:return
        for action in actions:
            row=action['row']
            if 'dafeiyu-qq:FriendMessage:'+row['target'] not in allowed:continue
            # A date can be removed while the background coroutine awaits QQ.
            current=self.db.get_date(OWNER,row['id'])
            if not current or current['revision']!=row['revision']:continue
            if not self.db.reserve_notice(action['key']):continue
            what='生日' if row['kind']=='birthday' else '纪念日“'+row['title']+'”'
            text='爸爸，{}是{}的{}，鲸鲸替你记着呢～需要祝福卡的话，私信我 /生日卡 {} 就好。'.format('明天' if action['days_before'] else '今天',row['name'],what,row['id'])
            try:
                sent=await asyncio.wait_for(bot.send_private_msg(user_id=int(OWNER),message=[{'type':'text','data':{'text':text}}]),timeout=12)
                message_id=sent.get('message_id') if isinstance(sent,dict) else None
                confirmed=(not isinstance(message_id,bool) and isinstance(message_id,(int,str))
                           and re.fullmatch(r'-?\d+',str(message_id)) and int(message_id)!=0)
                self.db.finish_notice(action['key'],'sent' if confirmed else 'unknown',message_id)
                self.stats['calendar_sent' if confirmed else 'calendar_unknown']+=1
            except asyncio.CancelledError:
                self.db.finish_notice(action['key'],'unknown');raise
            except Exception:
                self.db.finish_notice(action['key'],'unknown');self.stats['calendar_unknown']+=1

    async def calendar_loop(self):
        await asyncio.sleep(8)
        while True:
            try:await self.check_calendar()
            except asyncio.CancelledError:raise
            except Exception as error:logger.warning('Calendar check failed: %s',type(error).__name__)
            await asyncio.sleep(60)

    async def status(self):
        return json_response({'version':'0.1.1','stats':self.stats,'bots':self.db.bots(),'calendar_records':len(self.db.dates(OWNER)),'calendar_running':self.task is not None and not self.task.done(),'bot_round_limit':3,'bot_cooldown_seconds':1800,'calendar_timezone':'Asia/Shanghai'})
