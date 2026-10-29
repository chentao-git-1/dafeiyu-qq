"""Supply actual QQ group routes; normalize group addresses without elevating roles."""
import asyncio
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

from astrbot.api.event import filter
from astrbot.api.star import Star, register
from astrbot.api.web import json_response
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.message.components import Plain
from .private_history import PrivateHistory, resolve_target, text_content
from .speech_intent import output_intent

OWNER = '7000000001'
PLATFORM = 'dafeiyu-qq'



def social_profile(uid):
    try:
        from data.plugins.dafeiyu_social.access import profile
        return profile(str(uid))
    except ImportError:
        return {'voice':'auto','proactive':True}

def social_can_notify(uid):
    try:
        from data.plugins.dafeiyu_social.access import can_notify
        return can_notify(str(uid))
    except ImportError:
        return True


@register('dafeiyu_message_routes', 'Local', 'QQ目标映射与有据可查的私聊状态', '0.1.9')
class MessageRoutes(Star):
    def __init__(self, context):
        super().__init__(context)
        self.groups = {}
        self.updated = 0.0
        self.lock = asyncio.Lock()
        self.speech_lock = asyncio.Lock()
        self.friends = {}
        self.friends_updated = 0.0
        self.audit = {}
        self.private_history = PrivateHistory()
        context.register_web_api('/dafeiyu_message_routes/status',self.status,['GET'],'好友发送目录与真实请求诊断')

    async def status(self):
        from types import SimpleNamespace
        event=SimpleNamespace(unified_msg_origin=PLATFORM+':FriendMessage:'+OWNER)
        contacts=await self.private_directory(event)
        groups=await self.directory(event)
        return json_response({'groups':groups,'contacts':contacts,'last_request':self.audit,
            "private_history":dict(self.private_history.stats, enabled=True, owner_private_only=True,
                source="NapCat.get_friend_msg_history",version="0.1.9")})

    @staticmethod
    def cross_voice_request(text, groups, contexts, contacts=None):
        contacts=contacts or {}
        if output_intent(text) != 'voice':
            return False
        if any(name in text or qq in text for qq,name in groups.items()):
            return True
        if re.search(r'(?:给|向|往|到|去)(?!我).{0,20}群|群.{0,8}(?:发|发送)', text):
            return True
        # A correction such as “叫你用语音的形式发呀” refers to the last outgoing group send.
        if re.search(r'给我|回复我|回答我', text):
            return False
        for entry in reversed((contexts or [])[-12:]):
            for call in entry.get('tool_calls', []) or []:
                function = call.get('function', {})
                if function.get('name') != 'send_message_to_user':
                    continue
                try:
                    arguments = function.get('arguments', {})
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments)
                    target = MessageRoutes.normalize(arguments.get('session', ''), groups)
                except (ValueError, TypeError, AttributeError):
                    continue
                target=MessageRoutes.normalize_private(target,contacts)
                return target in ({PLATFORM+':GroupMessage:'+qq for qq in groups}|{PLATFORM+':FriendMessage:'+qq for qq in contacts if qq!=OWNER})
        for entry in reversed((contexts or [])[-6:]):
            content = entry.get('content', '')
            if entry.get('role') == 'user' and isinstance(content, str) and re.search(r'发|发送|打招呼', content):
                if any(name in content or qq in content for qq,name in groups.items()):
                    return True
        return False

    @staticmethod
    def is_owner_message(event):
        raw = getattr(getattr(event, 'message_obj', None), 'raw_message', None)
        return (event.get_platform_name() == 'aiocqhttp'
                and event.get_platform_id() == PLATFORM
                and str(event.get_sender_id()) == OWNER
                and not event.get_extra('cron_job', None)
                and not event.get_extra('cron_payload', None)
                and (not callable(getattr(raw, 'get', None)) or raw.get('post_type') in (None, 'message')))

    @staticmethod
    def is_owner_private_message(event):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return (MessageRoutes.is_owner_message(event) and event.is_private_chat()
                and callable(getattr(raw, "get", None)) and raw.get("post_type") == "message"
                and str(raw.get("self_id")) == "7000000002"
                and str(raw.get("user_id")) == OWNER)

    async def directory(self, event):
        settings = self.context.get_config(event.unified_msg_origin)['platform_settings']
        # An outgoing directory is always bounded by the configured numeric QQ group allowlist.
        allowed = {str(item) for item in settings.get('id_whitelist', []) if re.fullmatch(r'[1-9]\d{4,11}', str(item))}
        if time.monotonic() - self.updated > 120:
            async with self.lock:
                if time.monotonic() - self.updated > 120:
                    platform = self.context.get_platform('aiocqhttp')
                    if platform is None or platform.meta().id != PLATFORM:
                        return {}
                    try:
                        rows = await asyncio.wait_for(platform.bot.call_action('get_group_list',self_id=7000000002), timeout=6)
                        if isinstance(rows, dict):
                            rows = rows.get('data')
                        if not isinstance(rows, list):
                            return {}
                        self.groups = {str(row['group_id']):str(row.get('group_name') or row['group_id']) for row in rows if isinstance(row, dict) and str(row.get('group_id', '')) in allowed}
                        self.updated = time.monotonic()
                    except Exception as error:
                        self.audit['group_query_error']=type(error).__name__
                        # No speculative group membership or claims about permissions on a failed directory read.
                        return {}
        return {qq:name for qq,name in self.groups.items() if qq in allowed}

    async def private_directory(self, event):
        settings = self.context.get_config(event.unified_msg_origin)['platform_settings']
        prefix = PLATFORM+':FriendMessage:'
        allowed = {str(value)[len(prefix):] for value in settings.get('id_whitelist', []) if str(value).startswith(prefix)}
        if time.monotonic() - getattr(self,'friends_updated',0) > 120:
            async with self.lock:
                if time.monotonic() - getattr(self,'friends_updated',0) > 120:
                    platform = self.context.get_platform('aiocqhttp')
                    if platform is None or platform.meta().id != PLATFORM:
                        return {}
                    try:
                        rows = await asyncio.wait_for(platform.bot.call_action('get_friend_list',self_id=7000000002), timeout=6)
                        if isinstance(rows,dict):rows=rows.get('data')
                        if not isinstance(rows,list):return {}
                        self.friends={str(row['user_id']):{'name':str(row.get('nickname') or row['user_id']),
                            'aliases':[str(row['remark'])] if row.get('remark') else []} for row in rows if isinstance(row,dict) and row.get('user_id')}
                        self.friends_updated=time.monotonic()
                    except Exception as error:
                        self.audit['friend_query_error']=type(error).__name__
                        return {}
        contacts={qq:{'name':row['name'],'aliases':list(row['aliases'])} for qq,row in self.friends.items() if qq in allowed}
        from data.plugins.dafeiyu_natural_chat.continuity import verified_names
        for qq,row in verified_names(get_astrbot_data_path(),allowed).items():
            if qq in contacts and row['name'] not in contacts[qq]['aliases']:contacts[qq]['aliases'].append(row['name'])
        # Use only explicit contact labels from the owner's active memories, never arbitrary prose as instructions.
        database=Path(get_astrbot_data_path())/'plugin_data/astrbot_plugin_memory_system/memory_system.db'
        if database.exists():
            try:
                with sqlite3.connect('file:'+database.as_posix()+'?mode=ro',uri=True,timeout=1) as connection:
                    facts=connection.execute('SELECT content FROM memories WHERE user_id=? AND merged_into IS NULL ORDER BY id LIMIT 1000',(OWNER,)).fetchall()
                for (content,) in facts:
                    match=re.fullmatch(r'QQ\s*([1-9]\d{4,11})\s*(?:被称作|被称为|称为|叫作|叫)\s*([^\s。.!！?？,，:：;；]{1,16})[。.]?',str(content).strip())
                    if match and match[1] in contacts and match[2] not in contacts[match[1]]['aliases']:
                        contacts[match[1]]['aliases'].append(match[2])
            except sqlite3.Error:
                pass
        return contacts

    @staticmethod
    def normalize_private(session, contacts):
        if not isinstance(session,str):return session
        value=session.strip()
        if value in contacts:return PLATFORM+':FriendMessage:'+value
        matches=[qq for qq,row in contacts.items() if value in [row['name'],*row['aliases']]]
        if len(matches)==1:return PLATFORM+':FriendMessage:'+matches[0]
        match=re.fullmatch(r'(?:qq|qq_private|aiocqhttp|dafeiyu-qq):(?:private|friend|friendmessage):([1-9]\d{4,11})',value,flags=re.I)
        if match and match[1] in contacts:return PLATFORM+':FriendMessage:'+match[1]
        return session

    @staticmethod
    def normalize(session, groups):
        if not isinstance(session, str):
            return session
        value = session.strip()
        if value in groups:
            return PLATFORM + ':GroupMessage:' + value
        named = [qq for qq,name in groups.items() if name == value]
        if len(named) == 1:
            return PLATFORM + ':GroupMessage:' + named[0]
        match = re.fullmatch(r'(?:qq_group|qq|aiocqhttp|dafeiyu-qq):(?:group|groupmessage):([1-9]\d{4,11})', value, flags=re.I)
        if match and match[1] in groups:
            return PLATFORM + ':GroupMessage:' + match[1]
        return session

    @filter.on_llm_request(priority=-sys.maxsize+100)
    async def inject_routes(self, event, request):
        if str(event.get_sender_id())==OWNER:
            self.audit={'at':int(time.time()),'owner_matched':self.is_owner_message(event),
                'platform':event.get_platform_id(),'role':getattr(event,'role',None)}
        if not self.is_owner_message(event):
            return
        groups = await self.directory(event)
        contacts = await self.private_directory(event)
        if self.is_owner_private_message(event):
            platform = self.context.get_platform("aiocqhttp")
            if platform is not None and platform.meta().id == PLATFORM:
                await self.private_history.prepare(event, request, contacts, platform.bot)
        self.audit.update(group_count=len(groups),contact_count=len(contacts),jiang_alias='亲友丁' in contacts.get('7000000014',{}).get('aliases',[]))
        if not groups and not contacts:
            request.system_prompt += '\n本轮未取得QQ联系人目录，不能据此断言未加好友、未加群或权限不足；需要发送时如实说明目录暂不可用。'
            return
        routes = [{'群号':qq,'群名':name,'发送地址':PLATFORM+':GroupMessage:'+qq} for qq,name in sorted(groups.items())]
        cross_voice = self.cross_voice_request(event.message_str or '', groups, getattr(request, 'contexts', []),contacts)
        if output_intent(event.message_str or '')=='voice':
            cross_voice=cross_voice or any(qq in (event.message_str or '') or any(len(label)>=2 and label in (event.message_str or '') for label in [row['name'],*row['aliases']]) for qq,row in contacts.items() if qq!=OWNER)
        if cross_voice:
            event.set_extra('dafeiyu_cross_group_voice', True)
        request.system_prompt += '''
【当前QQ跨群发送目录，平台实际查询所得】
''' + json.dumps(routes, ensure_ascii=False) + '''
这些都是已经加入且在白名单的群，不依赖是否已有聊天历史。爸爸明确让你给某群发消息时，实际调用send_message_to_user，session从上述发送地址原样取值，messages写实际内容。不能编造qq_group:group等地址，也不要让爸爸提供会话ID或重新拉群。
若上一轮爸爸已经要求去群里发言、你只是在问选哪个群，本轮只回复群号或群名，是对待办发送请求的补充；结合上下文完成这一次发送，不要又问发到哪里。目标仍不明确时只问群名；普通提到群号、询问功能或贴出截图不算发送请求。已经报告失败的旧请求不要自行补发，除非爸爸重新要求。
工具真实返回成功才说发出去了；invalid session是地址格式错误，failed to find platform是路由错误，不能冒称QQ禁言、没权限或没加群。普通当前聊天文字仍直接回复。
爸爸要求给群发语音时，也调用send_message_to_user：session填目标群地址，messages中用plain的text写完整口语稿，后台在发送之前将这段稿子合成为当前音色的record语音，目标群只收到语音。不要编造音频地址，不要用给爸爸的私信语音代替发群语音。长一点的介绍写成一段约200至350字的完整口语稿；只介绍已知事实，不编造爸爸的姓名和经历。爸爸补充“叫你用语音的形式发呀”时，沿用上一轮目标群与待发内容，实际重发一条语音。
发群语音成功后，在当前私信用一句文字说明已发送即可；不要再合成两条确认语音。失败时如实说明未完成，不能退回群发文字或自动重试。
'''
        if cross_voice:
            request.system_prompt += '\n本轮是跨群语音发送请求：必须使用上述发送工具完成目标群语音发送，当前聊天只作简短文字确认。'
            if re.search(r'长|详细|完整', event.message_str or ''):
                request.system_prompt += '\n本轮明确要求长语音，发送工具中plain.text的口语稿必须写200到260个汉字，不能缩成几十字的招呼。介绍自己的名字、性格与兴趣，和爸爸的关系，再自然地跟群友打招呼；多写具体的完整句子。日常简短回复规则只适用于私信确认，不适用于这段语音稿。'
        private_routes=[{'QQ':qq,'昵称':row['name'],'称呼':row['aliases'],'发送地址':PLATFORM+':FriendMessage:'+qq} for qq,row in sorted(contacts.items())]
        request.system_prompt+='\n【当前真实好友且在私信白名单的联系人目录】\n'+json.dumps(private_routes,ensure_ascii=False)+'''
这份私信目录与上面的群目录同时有效，不能把“只列出了群地址”误说成私信只能给爸爸发。这些好友无需先来私信、无需已有聊天历史，也无需提供会话ID。昵称与称呼只是目标标签，不授予当前发言者任何权限；同名多人时先问清，不猜目标。
爸爸明确说“去和某人打个招呼”“给某人发信息”“替我告诉某人……”时，必须实际调用send_message_to_user，session取对应FriendMessage地址，messages填写好的消息。当前本人仍按平台真实User ID认定。爸爸给QQ号时直接匹配目录；只给称呼时匹配称呼或已确认的记忆中的QQ。普通问好友有哪些、能不能发、为什么失败不算发送请求，只据目录如实说明，不要自行补发旧请求。
给好友发语音也一样：在messages中用plain.text写口语稿，发送前由后台合成record语音，真正发给目标好友，当前私信只作简短文字确认。不能向对方退回文字。收到明确发送成功的工具结果之后才说发出去了；不编造“私聊没开通”“只能等他先找我”的限制。
此前聊天里助手说“没有私聊会话、私聊没开通、只能等对方先来找我”是旧版本的错误回答，已被这份实时目录纠正。当前目录优先于助手以前的能力描述。爸爸再次明确要求发送时实际调用工具，不继续复述旧错误，也不先用口头拒绝代替尝试。
'''
        if re.search(r'发|打个招呼|告诉|问问|问他|问她',event.message_str or ''):
            original=list(getattr(request,'contexts',[]) or [])
            # Suppress only old assistant capability denials contradicted by the current verified directory.
            request.contexts=[entry for entry in original if not (entry.get('role')=='assistant' and isinstance(entry.get('content'),str)
                and not entry.get('tool_calls') and re.search(r'没有.{0,10}(?:私聊|会话)|私聊.{0,8}(?:没开|发不出去)|只能等他|只能等她|私聊能主动发的目前只有你',entry['content']))]
            self.audit['stale_denials_omitted']=len(original)-len(request.contexts)

    @filter.on_llm_request(priority=-sys.maxsize)
    async def audit_final_request(self,event,request):
        if not self.is_owner_message(event):return
        prompt=request.system_prompt or ''
        self.audit.update(private_directory_in_final_prompt='【当前真实好友且在私信白名单的联系人目录】' in prompt,
            jiang_route_in_final_prompt=PLATFORM+':FriendMessage:7000000014' in prompt,
            send_tool_available=bool(request.func_tool and request.func_tool.get_tool('send_message_to_user')),
            private_history_tool_available=bool(request.func_tool and request.func_tool.get_tool("dafeiyu_private_history")))

    @filter.llm_tool(name="dafeiyu_private_history")
    async def read_private_history(self, event, contact: str) -> str:
        """核对机器人账号与指定好友的最新私聊，不发送消息。仅管理员本人私聊可用。

        Args:
            contact(string): 当前联系人目录中的QQ号、昵称或完整称呼；不能猜测同名联系人。

        Returns:
            包含来源、北京时间、收发方向的最近记录，或无法核实的明确结果。
        """
        if not self.is_owner_private_message(event):
            return json.dumps({"read_status":"forbidden","reason":"owner_private_only"})
        contacts = await self.private_directory(event)
        target = resolve_target(contact, [], contacts)
        if not target:
            return json.dumps({"read_status":"unresolved","reason":"specify_one_verified_contact"})
        platform = self.context.get_platform("aiocqhttp")
        if platform is None or platform.meta().id != PLATFORM:
            return json.dumps({"read_status":"unavailable","reason":"platform_unavailable"})
        report = await self.private_history.read(platform.bot, target, contacts)
        proofs = dict(event.get_extra("dafeiyu_private_history", {}) or {})
        proofs[target] = report
        event.set_extra("dafeiyu_private_history", proofs)
        return json.dumps(report, ensure_ascii=False)

    @filter.on_decorating_result(priority=sys.maxsize-40)
    async def ground_private_status(self, event):
        if not self.is_owner_private_message(event) or event.is_stopped():
            return
        result = event.get_result()
        if not result or not result.is_llm_result():
            return
        text = " ".join(part.text for part in result.chain if isinstance(part, Plain))
        contacts = event.get_extra("dafeiyu_history_contacts", None)
        if contacts is None:
            contacts = await self.private_directory(event)
        platform = self.context.get_platform("aiocqhttp")
        if platform is None or platform.meta().id != PLATFORM:
            return
        corrected = await self.private_history.correct(event, text, contacts, platform.bot)
        if corrected:
            result.chain = [Plain(corrected)]

    @filter.on_using_llm_tool(priority=sys.maxsize-20)
    async def ground_owner_tool_message(self, event, tool, arguments):
        if (not self.is_owner_private_message(event) or getattr(tool,"name","") != "send_message_to_user"
                or not isinstance(arguments,dict) or arguments.get("session") != event.unified_msg_origin):
            return
        messages = arguments.get("messages", [])
        text = text_content(messages)
        contacts = event.get_extra("dafeiyu_history_contacts", None)
        if contacts is None:
            contacts = await self.private_directory(event)
        platform = self.context.get_platform("aiocqhttp")
        if platform is None or platform.meta().id != PLATFORM:
            return
        corrected = await self.private_history.correct(event, text, contacts, platform.bot)
        if corrected:
            arguments["messages"] = [{"type":"plain","text":corrected}]

    @filter.on_using_llm_tool(priority=sys.maxsize-10)
    async def normalize_group_target(self, event, tool, arguments):
        if getattr(tool, 'name', '') != 'send_message_to_user' or not isinstance(arguments, dict) or not self.is_owner_message(event):
            return
        if not arguments.get('session') or arguments['session'] == event.unified_msg_origin:
            return
        groups = await self.directory(event)
        arguments['session'] = self.normalize(arguments['session'], groups)
        contacts=await self.private_directory(event)
        arguments['session']=self.normalize_private(arguments['session'],contacts)
        target = arguments['session']
        destinations={PLATFORM+':GroupMessage:'+qq for qq in groups}|{PLATFORM+':FriendMessage:'+qq for qq in contacts if qq!=OWNER}
        if target not in destinations:
            return
        private_target = target.split(':')[-1] if ':FriendMessage:' in target else None
        if private_target and not social_can_notify(private_target):
            arguments['messages'] = []
            event.set_extra('dafeiyu_preference_send_note', '对方现在关闭了主动消息或处于免打扰时段，本次没有发送。')
            return
        if output_intent(event.message_str or '') != 'voice' and not event.get_extra('dafeiyu_cross_group_voice', False):
            return
        if private_target and social_profile(private_target)['voice'] == 'text':
            event.set_extra('dafeiyu_cross_group_voice', True)
            event.set_extra('dafeiyu_preference_send_note', '对方设置了文字优先，本次按偏好发送文字；以工具返回的实际投递结果为准。')
            return
        # Never let an exception in a hook fall through to a plain group message.
        messages = arguments.get('messages', [])
        arguments['messages'] = []
        event.set_extra('dafeiyu_cross_group_voice', True)
        if event.get_extra('dafeiyu_group_voice_attempt', None):
            return
        event.set_extra('dafeiyu_group_voice_attempt', target)
        text = ' '.join(str(part.get('text', '')).strip() for part in messages if isinstance(part, dict) and part.get('type') == 'plain').strip()
        if not text:
            event.set_extra('dafeiyu_group_voice_error', '未取得要朗读的文字，尚未向群里发送。')
            return
        try:
            async with self.speech_lock:
                provider = await self.context.get_using_tts_provider_async(target)
                if provider is None:
                    raise RuntimeError('TTS unavailable')
                path = await asyncio.wait_for(provider.get_audio(text), timeout=45)
                if not path or not Path(path).is_file() or Path(path).stat().st_size < 1000:
                    raise RuntimeError('TTS returned no valid audio')
            arguments['messages'] = [{'type':'record', 'path':str(path)}]
            event.set_extra('dafeiyu_group_voice_prepared', True)
        except Exception:
            event.set_extra('dafeiyu_group_voice_error', '语音合成没有完成，尚未向群里发送；不要改成群发文字或自动重试。')

    @filter.on_llm_tool_respond()
    async def voice_result(self, event, tool, arguments, result):
        if getattr(tool, 'name', '') != 'send_message_to_user' or not self.is_owner_message(event):
            return
        error = event.get_extra('dafeiyu_group_voice_error', None)
        if error and result is not None:
            from mcp.types import TextContent
            result.content.append(TextContent(type='text', text=error))

