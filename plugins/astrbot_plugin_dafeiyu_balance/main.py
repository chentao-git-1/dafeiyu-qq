"""Read and monitor the existing DeepSeek account without using a chat model."""
import asyncio
import contextlib
import json
from pathlib import Path
import re
import time

import aiohttp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.api.web import json_response
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.agent.message import TextPart
from .reply_context import MARKER, notice_records, latest_notice, bridge_history, context_note

from .watch import (
    BalanceWatch, INTERVAL, OWNER, BOT, HELP_THRESHOLD, balance_snapshot,
    notice_text,
)

ENDPOINT = 'https://api.deepseek.com/user/balance'
QUERY = re.compile(
    r'/?(?:余额|查余额|查询余额|查看余额|查查余额|看看余额|账户余额|账号余额|deepseek余额|balance'
    r'|余额(?:是多少|有多少)|(?:你|自己)?(?:现在|目前)?(?:还有|还剩|剩下|剩余)多少(?:钱|余额)'
    r'|(?:帮我|请)?查(?:一下|查)?(?:deepseek)?(?:账户|账号)?余额)', re.I
)


def balance_text(data):
    """Format only validated balance fields from the official API."""
    if not isinstance(data.get('is_available'), bool):
        raise ValueError('Invalid balance response')
    rows = data.get('balance_infos')
    if not isinstance(rows, list) or not rows or len(rows) > 2:
        raise ValueError('Invalid balance rows')
    lines = ['DeepSeek 账户余额']
    for row in rows:
        currency = row.get('currency')
        if currency not in ('CNY', 'USD'):
            raise ValueError('Invalid currency')
        values = []
        for name in ('total_balance', 'topped_up_balance', 'granted_balance'):
            value = row.get(name)
            if not isinstance(value, str) or not re.fullmatch(r'-?\d{1,12}(?:\.\d{1,12})?', value):
                raise ValueError('Invalid balance value')
            values.append(value)
        unit = '元' if currency == 'CNY' else '美元'
        lines.extend(['总余额：{} {}'.format(values[0], unit), '充值：{}｜赠金：{} {}'.format(values[1], values[2], unit)])
    lines.append('账户状态：'+('可调用' if data['is_available'] else '余额不可用'))
    return '\n'.join(lines)



def social_profile(uid):
    try:
        from data.plugins.dafeiyu_social.access import profile
        return profile(str(uid))
    except ImportError:
        return {'voice':'auto','proactive':True}

def social_can_notify(uid, timestamp=None):
    try:
        from data.plugins.dafeiyu_social.access import can_notify
        return can_notify(str(uid), timestamp)
    except ImportError:
        return True


class BalanceHTTPError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__('Balance HTTP ' + str(status))


def deepseek_key(config):
    sources = [row for row in config.get('provider_sources', [])
               if str(row.get('api_base', '')).rstrip('/') in
               ('https://api.deepseek.com', 'https://api.deepseek.com/v1')]
    model = config.get('agent_runner', {}).get('config', {}).get('model', {}).get('provider_id')
    source_id = next((p.get('provider_source_id') for p in config.get('provider', [])
                      if p.get('id') == model), None)
    selected = next((row for row in sources if row.get('id') == source_id), None)
    if selected is None and len(sources) == 1:
        selected = sources[0]
    if selected is None:
        raise ValueError('DeepSeek account source unavailable or ambiguous')
    keys = selected.get('key') or []
    keys = [keys] if isinstance(keys, str) else keys
    key = next((value.strip() for value in keys
                if isinstance(value, str) and value.strip()), '')
    if not key:
        raise ValueError('DeepSeek key unavailable')
    return key


async def fetch_balance(key):
    """Use a fixed HTTPS endpoint; never expose API response bodies on errors."""
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=False) as session:
        async with session.get(ENDPOINT, headers={'Authorization':'Bearer '+key, 'Accept':'application/json'}, allow_redirects=False) as response:
            if response.status != 200:
                raise BalanceHTTPError(response.status)
            raw = await response.content.read(32769)
            if len(raw) > 32768:
                raise ValueError('Balance response too large')
            data = json.loads(raw)
            balance_text(data)
            return data


async def query_balance(key):
    try:
        return balance_text(await fetch_balance(key))
    except BalanceHTTPError as error:
        return 'DeepSeek 余额查询失败（HTTP {}），没有调用聊天模型。'.format(error.status)


class DafeiyuBalance(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data = Path(get_astrbot_data_path())
        self.watch = BalanceWatch(self.data / 'plugin_data/astrbot_plugin_dafeiyu_balance/state.json')
        self.lock = asyncio.Lock()
        self.task = None
        self.stopping = False
        self.next_check_at = None
        context.register_web_api(
            '/dafeiyu_balance/status', self.status, ['GET'],
            'DeepSeek 两档饭钱提醒状态',
        )

    async def initialize(self):
        await self.backfill_notice_text()
        self.task = asyncio.create_task(self.watch_loop())

    async def backfill_notice_text(self):
        """Recover legacy notice text by its confirmed ID in target-scoped QQ history."""
        platform=self.context.get_platform('aiocqhttp')
        bot=getattr(platform,'bot',None)
        if platform is None or platform.meta().id!='dafeiyu-qq' or not callable(getattr(bot,'call_action',None)):return
        try:from data.plugins.dafeiyu_natural_chat.continuity import decode_history
        except ImportError:return
        async with self.lock:
            now=self.watch.clock()
            rows=notice_records(self.watch.state,None,now)
            targets=list(dict.fromkeys(r['qq'] for r in rows if not r['record'].get('text')))[:3]
            for qq in targets:
                if not self.private_allowed(qq,self.context.get_config('dafeiyu-qq:FriendMessage:'+qq)):continue
                try:
                    raw=await asyncio.wait_for(bot.call_action('get_friend_msg_history',user_id=int(qq),count=30,self_id=int(BOT)),timeout=4)
                    proof=latest_notice(self.watch.state,qq,now,decode_history(raw,qq,before=now))
                    if proof and not proof['record'].get('text'):
                        proof['record']['text']=proof['text'];self.watch.save()
                except Exception as error:logger.warning('Balance notice context unavailable: %s',type(error).__name__)

    @filter.on_llm_request(priority=-1000040)
    async def notice_reply_context(self,event,req):
        raw=getattr(event.message_obj,'raw_message',None)
        uid=str(event.get_sender_id())
        if (event.get_platform_name()!='aiocqhttp' or event.get_platform_id()!='dafeiyu-qq' or not event.is_private_chat()
                or not isinstance(raw,dict) or raw.get('post_type')!='message' or str(raw.get('self_id'))!=BOT or str(raw.get('user_id'))!=uid
                or event.get_extra('cron_job',None) or event.get_extra('cron_payload',None)
                or not self.private_allowed(uid,self.context.get_config(event.unified_msg_origin))):return
        stamp=raw.get('time')
        if isinstance(stamp,bool) or not isinstance(stamp,(int,float)) or not 0<=self.watch.clock()-stamp<=300:return
        history=event.get_extra('jingjing_actual_chat',[]) or []
        notice=latest_notice(self.watch.state,uid,stamp,history)
        if not notice:return
        contexts,added=bridge_history(getattr(req,'contexts',None),notice,history)
        if added:req.contexts=contexts
        source=event.get_extra('jingjing_original_input',{}) or {}
        human=str(source.get('text',event.message_str) or '')
        parts=getattr(req,'extra_user_content_parts',None)
        if parts is None:parts=[];req.extra_user_content_parts=parts
        parts[:]=[p for p in parts if not isinstance(p,TextPart) or not p.text.startswith(MARKER)]
        parts.append(TextPart(text=context_note(notice,human,history)))
        event.set_extra('dafeiyu_balance_context',{'notice_id':notice['id'],'history_added':added})

    async def terminate(self):
        self.stopping = True
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        async with self.lock:
            pass

    @staticmethod
    def private_allowed(qq, config):
        settings = config['platform_settings']
        allowed = {str(value) for value in settings.get('id_whitelist', [])}
        return (not settings.get('enable_id_white_list', True) or str(qq) in allowed
                or 'dafeiyu-qq:FriendMessage:' + str(qq) in allowed)

    async def ranked_contacts(self, config, bot):
        if bot is None:
            return None
        try:
            from data.plugins.dafeiyu_social.access import activity, store
            friends = await asyncio.wait_for(bot.get_friend_list(), timeout=8)
            if isinstance(friends, dict):
                friends = friends.get('friends', friends.get('data'))
            if not isinstance(friends, list):
                return None
            reachable = {str(row['user_id']) for row in friends
                         if isinstance(row, dict) and 'user_id' in row and row.get('is_bot') is not True}
            ranking = activity().ranked(store().bots())
            return [{'qq': row['qq'], 'name': '朋友'} for row in ranking
                    if row['qq'] in reachable and self.private_allowed(row['qq'], config)
                    and social_profile(row['qq']).get('proactive', True)
                    and (not self.watch.daytime() or social_can_notify(row['qq'], self.watch.clock()))]
        except Exception as error:
            logger.warning('Balance contacts unavailable: %s', type(error).__name__)
            return None

    async def run_once(self):
        async with self.lock:
            if self.stopping:
                return None
            config = self.context.get_config('dafeiyu-qq:FriendMessage:' + OWNER)
            try:
                data = await fetch_balance(deepseek_key(config))
                amount, available = balance_snapshot(data)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.watch.record_error(type(error).__name__)
                logger.warning('DeepSeek balance check failed: %s', type(error).__name__)
                return None
            platform = self.context.get_platform('aiocqhttp')
            bot = getattr(platform, 'bot', None)
            if platform is None or platform.meta().id != 'dafeiyu-qq':
                bot = None
            contacts = await self.ranked_contacts(config, bot) if amount < HELP_THRESHOLD else []
            actions = self.watch.observe(amount, available, contacts)
            outcome = {'amount': format(amount, 'f'), 'available': available, 'owner_notified': False}
            if not actions or self.stopping:
                return outcome
            if platform is None or platform.meta().id != 'dafeiyu-qq' or bot is None:
                return outcome
            try:
                online = await asyncio.wait_for(bot.get_status(), timeout=8)
                if online.get('online') is not True:
                    return outcome
            except Exception:
                return outcome
            for action in actions:
                if self.stopping:
                    break
                # Re-check the live allowlist immediately before a private notice.
                current = self.context.get_config('dafeiyu-qq:FriendMessage:' + OWNER)
                if not self.private_allowed(action['qq'], current):
                    if action['kind'] == 'help':
                        self.watch.skip_helper(action['qq'])
                    continue
                if action['kind'] == 'help':
                    from data.plugins.dafeiyu_social.access import store
                    if action['qq'] in store().bots() or not social_can_notify(action['qq'], self.watch.clock()):
                        self.watch.skip_helper(action['qq'])
                        continue
                if not self.watch.reserve(action):
                    continue
                sent_text=notice_text(action,format(amount,'f'))
                try:
                    # Account notifications are independent of model reply quotas.
                    reply = await asyncio.wait_for(bot.send_private_msg(
                        user_id=int(action['qq']),
                        message=[{'type': 'text', 'data': {
                            'text': sent_text,
                        }}],
                    ), timeout=12)
                    message_id = reply.get('message_id') if isinstance(reply, dict) else None
                    if (
                        isinstance(message_id, bool)
                        or not isinstance(message_id, (int, str))
                        or not re.fullmatch(r'-?\d+', str(message_id))
                        or int(message_id) == 0
                    ):
                        self.watch.complete(action, 'unknown')
                    else:
                        self.watch.complete(action, 'sent', message_id, text=sent_text)
                    outcome['owner_notified'] |= action['qq'] == OWNER
                except asyncio.CancelledError:
                    self.watch.complete(action, 'unknown')
                    raise
                except Exception as error:
                    status = 'failed' if type(error).__name__ in ('ActionFailed', 'ApiNotAvailable') else 'unknown'
                    self.watch.complete(action, status)
                    logger.warning('Balance notice unconfirmed: %s', type(error).__name__)
            return outcome

    async def watch_loop(self):
        await asyncio.sleep(5)
        while not self.stopping:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning('Balance monitor iteration failed: %s', type(error).__name__)
            self.next_check_at = time.time() + INTERVAL
            await asyncio.sleep(INTERVAL)

    async def status(self):
        return json_response(dict(
            self.watch.status(), version='0.3.1', monitored_account='deepseek',
            loop_running=self.task is not None and not self.task.done(),
            next_check_at=self.next_check_at, owner_qq=OWNER,
            helper_count_per_cycle=2, helper_hours='09:00–21:00 Asia/Shanghai',
            uses_chat_model=False, uses_tts=False,
            confirmed_notice_context=True,
        ))

    @staticmethod
    def control_intent(text):
        raw = str(text or '').strip()
        commands = {
            '饭钱状态': 'status', '饭钱检查': 'check',
            '饭钱暂停': 'pause', '饭钱继续': 'resume',
        }
        if raw.lstrip('/') in commands:
            return commands[raw.lstrip('/')]
        if re.search(r'[?？吗么]', raw):
            return None
        clean = re.sub(r'```.*?```|`[^`]*`|“[^”]*”|「[^」]*」|"[^"]*"', '', raw, flags=re.S)
        for clause in reversed(re.split(r'[，,。.!！;；\n]', clean)):
            clause = re.sub(r'\s+', '', clause)
            if re.search(r'(?:不|没|别).{0,4}充', clause):
                return None
            if re.fullmatch(
                r'(?:我)?(?:已经|刚刚|刚才)?(?:充好了|充完了|充值完成|充值成功|已充值|饭钱到账了)', clause,
            ):
                return 'check'
            if re.fullmatch(
                r'(?:我)?(?:现在|这就|马上)?(?:去|来)(?:给你)?充(?:值|钱|饭钱)?(?:一下|点)?(?:了|啦|哈)?'
                r'|(?:我)?(?:已经|现在)?(?:正在|在)充(?:值|钱|饭钱)?(?:了|呢|啦)?'
                r'|(?:我)?(?:马上|现在|这就)(?:给你)?充(?:值|钱|饭钱)?(?:一下|点)?(?:了|啦|哈)?', clause,
            ):
                return 'pause'
        return None

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5100)
    async def control_reminder(self, event):
        raw = getattr(event.message_obj, 'raw_message', None)
        if (
            event.get_platform_id() != 'dafeiyu-qq'
            or not event.is_private_chat() or str(event.get_sender_id()) != OWNER
            or not callable(getattr(raw, 'get', None)) or raw.get('post_type') != 'message'
            or str(raw.get('self_id')) != BOT or str(raw.get('user_id')) != OWNER
            or event.get_extra('cron_job', None) or event.get_extra('cron_payload', None)
        ):
            return
        text = ''.join(p.text for p in event.get_messages() if isinstance(p, Plain))
        intent = self.control_intent(text)
        if not intent:
            return
        explicit = text.strip().lstrip('/') in ('饭钱状态', '饭钱检查', '饭钱暂停', '饭钱继续')
        cycle = self.watch.state.get('cycle')
        if not explicit and (not cycle or cycle['status'] != 'low'):
            return
        if intent == 'pause':
            async with self.lock:
                paused = self.watch.pause()
            reply = ('好，爸爸先忙着充，鲸鲸两小时内不催，也不去喊别人～'
                     if paused else '现在没有低余额提醒，鲸鲸先不催饭～')
        elif intent == 'status':
            info = self.watch.status()
            balance = info['last_balance_cny']
            reply = ('DeepSeek 可用余额：{} 元。'.format(balance) if balance is not None else '余额还没检查到。')
            reply += '低于 1.00 元提醒爸爸，低于 0.70 元进入亲友提醒。'
            if cycle and cycle['status'] == 'low':
                if cycle.get('pause_until', 0) > time.time():
                    reply += '爸爸正在处理，催促已暂停。'
                else:
                    reply += '当前处于低余额提醒中。'
            else:
                reply += '当前没有催饭提醒。'
            if info['last_error']:
                reply += '最近一次检查没成功，显示的是上次查到的余额。'
        else:
            if intent == 'resume':
                async with self.lock:
                    self.watch.resume()
            outcome = await self.run_once()
            if outcome is None:
                reply = '这次余额没查到，鲸鲸先不把它当成没钱或已经到账，稍后再查。'
            elif outcome['owner_notified']:
                event.stop_event()
                return
            else:
                reply = '刚查过，DeepSeek 可用余额 {} 元，提醒已按实际余额更新。'.format(outcome['amount'])
        await event.send(MessageChain(chain=[Plain(reply)]))
        event.stop_event()

    def _allowed(self, event):
        if event.get_platform_name() != 'aiocqhttp':
            return False
        config = self.context.get_config(event.unified_msg_origin)
        group = str(event.get_group_id() or '')
        if group:
            whitelist = {str(value) for value in config['platform_settings'].get('id_whitelist', [])}
            mentioned = any(isinstance(part, At) and str(part.qq) == str(event.get_self_id()) for part in event.get_messages())
            return group in whitelist and mentioned
        return str(event.get_sender_id()) == OWNER

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5000)
    async def check_balance(self, event: AstrMessageEvent):
        text = ''.join(part.text for part in event.get_messages() if isinstance(part, Plain))
        text = re.sub(r'\s+', '', text).strip('？?。！!，,')
        if not QUERY.fullmatch(text):
            return
        if not self._allowed(event):
            event.stop_event()
            return
        config = self.context.get_config(event.unified_msg_origin)
        try:
            reply = await query_balance(deepseek_key(config))
        except Exception:
            reply = 'DeepSeek 余额查询暂时失败，请稍后再试。'
        chain = []
        if event.get_group_id() and config['platform_settings'].get('reply_with_mention'):
            chain.append(At(qq=str(event.get_sender_id()), name=event.get_sender_name()))
        chain.append(Plain(reply))
        await event.send(MessageChain(chain=chain))
        event.stop_event()
