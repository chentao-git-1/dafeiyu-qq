import asyncio
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import time

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.web import json_response
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

OWNER = '7000000001'
BOT = '7000000002'



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


@register('dafeiyu_friend_gate', 'Local', '自动通过好友、同步白名单和一次性语音欢迎', '0.3.1')
class FriendGate(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.path = Path(get_astrbot_data_path()) / 'plugin_data/dafeiyu_friend_gate/state.json'
        self.state = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'allowed': {}, 'requests': {}}
        self.lock = asyncio.Lock()
        self.bot = None
        self.task = None
        self.callback = self.receive_request
        self.last_sync = {'checked_at': 0, 'added_count': 0}
        context.register_web_api('/dafeiyu_friend_gate/status', self.status, ['GET'], '好友申请管理状态')

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.chmod(0o600)
        os.replace(temporary, self.path)

    async def initialize(self):
        self.attach()
        self.task = asyncio.create_task(self.watch_platform())

    def attach(self):
        platform = self.context.get_platform('aiocqhttp')
        bot = getattr(platform, 'bot', None)
        if platform is None or platform.meta().id != 'dafeiyu-qq':
            bot = None
        if bot is self.bot:
            return
        if self.bot is not None:
            self.bot.unsubscribe('request.friend', self.callback)
        self.bot = bot
        if bot is not None:
            bot.subscribe('request.friend', self.callback)

    async def watch_platform(self):
        while True:
            self.attach()
            if self.bot is not None and time.time() - self.last_sync['checked_at'] >= 30:
                try:
                    async with self.lock:
                        if self.state.get('auto_approve_all'):
                            pending = {row['qq'] for row in self.state['requests'].values()
                                       if row['status'] == 'pending' and time.time() - row['received_at'] <= 7 * 86400}
                            for uid in pending:
                                if self.can_approve(uid):
                                    await self.approve_for(uid)
                        if self.state.get('auto_whitelist_friends') or self.state.get('auto_welcome'):
                            friends = await asyncio.wait_for(self.bot.get_friend_list(), timeout=12)
                            users = {str(friend['user_id']) for friend in friends}
                            await self.sync_whitelist(users)
                            await self.welcome_friends(users)
                    self.last_sync['checked_at'] = time.time()
                    self.last_sync.pop('error_type', None)
                except Exception as error:
                    self.last_sync.update({'checked_at': time.time(), 'error_type': type(error).__name__})
                    logger.warning('Friend policy synchronization failed: %s', type(error).__name__)
            await asyncio.sleep(10)

    def can_approve(self, uid):
        return (uid not in self.state.get('denied', {}) and
                (bool(self.state.get('auto_approve_all')) or uid in self.state['allowed']))

    async def sync_whitelist(self, users):
        if not self.state.get('auto_whitelist_friends'):
            return []
        users = {str(uid) for uid in users if re.fullmatch(r'[1-9]\d{4,11}', str(uid)) and str(uid) != BOT}
        settings = self.context.get_config()['platform_settings']
        allowed = {str(value) for value in settings.get('id_whitelist', [])}
        missing = sorted('dafeiyu-qq:FriendMessage:' + uid for uid in users
                         if 'dafeiyu-qq:FriendMessage:' + uid not in allowed)
        self.last_sync['friend_count'] = len(users)
        if not missing:
            self.last_sync['added_count'] = 0
            return []
        access_path = self.path.parent / 'dashboard-access.json'
        access = json.loads(access_path.read_text(encoding='utf-8-sig'))
        # Use the existing server-only dashboard login and native pipeline reload.
        async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=35) as client:
            response = await client.post('http://127.0.0.1:6185/api/v1/auth/login', json=access)
            if response.status_code != 200:
                raise RuntimeError('Local whitelist authentication failed')
            body = response.json()
            token = body.get('data', body).get('token')
            if not token:
                raise RuntimeError('Local whitelist token unavailable')
            desired = copy.deepcopy(dict(self.context.get_config()))
            current = desired['platform_settings']['id_whitelist']
            additions = [value for value in missing if value not in current]
            if not additions:
                return []
            current.extend(additions)
            response = await client.put('http://127.0.0.1:6185/api/v1/config-profiles/default',
                                        json=desired, headers={'Authorization': 'Bearer ' + token})
            if response.status_code != 200 or response.json().get('status') != 'ok':
                raise RuntimeError('Native whitelist update failed')
        actual = self.context.get_config()['platform_settings']['id_whitelist']
        if not all(value in actual for value in additions):
            raise RuntimeError('Native whitelist update not reflected in memory')
        self.last_sync.update({'added_count': len(additions), 'checked_at': time.time()})
        return additions

    async def welcome_friends(self, users):
        if not self.state.get('auto_welcome'):
            return
        users = {str(uid) for uid in users if re.fullmatch(r'[1-9]\d{4,11}', str(uid)) and str(uid) != BOT}
        known = self.state.setdefault('known_friends', {})
        welcomes = self.state.setdefault('welcomes', {})
        if not self.state.get('welcome_baseline_initialized'):
            known.update({uid: time.time() for uid in users})
            self.state['welcome_baseline_initialized'] = True
            self.save()
            return
        for uid in users - set(known):
            known[uid] = time.time()
            welcomes.setdefault(uid, {'status': 'queued', 'observed_at': time.time()})
        self.save()
        queued = [uid for uid in sorted(welcomes) if uid in users and welcomes[uid]['status'] == 'queued']
        if not queued:
            return
        audio = self.path.parent / 'welcome.wav'
        expected_hash = self.state.get('welcome_audio', {}).get('sha256')
        if not expected_hash or not audio.is_file() or hashlib.sha256(audio.read_bytes()).hexdigest() != expected_hash:
            raise RuntimeError('Welcome audio unavailable or changed')
        # Deliver at most two per scan and reuse the accepted voice recording.
        for index, uid in enumerate(queued[:2]):
            row = welcomes[uid]
            preference = social_profile(uid)
            if not preference['proactive']:
                row.update({'status':'skipped_preference','updated_at':time.time()})
                self.save()
                continue
            if not social_can_notify(uid):
                continue
            row['status'] = 'sending'
            self.save()
            try:
                message = ([{'type':'text','data':{'text':'你好呀，我是鲸鲸，很高兴认识你～'}}]
                           if preference['voice'] == 'text' else
                           [{'type':'record','data':{'file':'file://' + str(audio)}}])
                result = await asyncio.wait_for(self.bot.send_private_msg(user_id=int(uid), message=message), timeout=25)
                if not isinstance(result, dict) or result.get('message_id') is None:
                    raise RuntimeError('Welcome delivery not confirmed')
            except Exception as error:
                row.update({'status': 'unknown', 'error_type': type(error).__name__, 'updated_at': time.time()})
            else:
                row.update({'status': 'sent', 'message_id': result['message_id'], 'sent_at': time.time()})
            self.save()
            if index == 0 and len(queued) > 1:
                await asyncio.sleep(2)

    async def terminate(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        if self.bot is not None:
            self.bot.unsubscribe('request.friend', self.callback)
            self.bot = None

    async def status(self):
        counts = {}
        for row in self.state['requests'].values():
            counts[row['status']] = counts.get(row['status'], 0) + 1
        welcome_counts = {}
        for row in self.state.get('welcomes', {}).values():
            welcome_counts[row['status']] = welcome_counts.get(row['status'], 0) + 1
        return json_response({'listener_attached': self.bot is not None,
                              'auto_approve_all': bool(self.state.get('auto_approve_all')),
                              'auto_whitelist_friends': bool(self.state.get('auto_whitelist_friends')),
                              'auto_welcome': bool(self.state.get('auto_welcome')),
                              'welcome_baseline_count': len(self.state.get('known_friends', {})),
                              'welcome_status_counts': welcome_counts,
                              'denied_count': len(self.state.get('denied', {})),
                              'allowed_count': len(self.state['allowed']), 'request_status_counts': counts,
                              'whitelist_sync': self.last_sync})

    async def receive_request(self, raw):
        if raw.get('post_type') != 'request' or raw.get('request_type') != 'friend' or str(raw.get('self_id')) != BOT:
            return
        uid, flag = str(raw.get('user_id', '')), str(raw.get('flag') or '')
        if not re.fullmatch(r'[1-9]\d{4,11}', uid) or not flag or len(flag) > 1024:
            return
        key = hashlib.sha256((uid + ':' + flag).encode()).hexdigest()
        try:
            async with self.lock:
                self.state['requests'].setdefault(key, {'qq': uid, 'flag': flag, 'received_at': time.time(), 'status': 'pending'})
                # Expire only unprocessed requests. Completed/uncertain IDs remain deduplicated.
                for row in self.state['requests'].values():
                    if row['status'] == 'pending' and time.time() - row['received_at'] > 7 * 86400:
                        row['status'] = 'expired'
                        row.pop('flag', None)
                if len(self.state['requests']) > 512:
                    oldest = sorted(self.state['requests'], key=lambda item: self.state['requests'][item]['received_at'])
                    for item in oldest[:len(oldest) - 512]:
                        if self.state['requests'][item]['status'] != 'approving':
                            self.state['requests'].pop(item)
                self.save()
                if self.can_approve(uid):
                    await self.approve_for(uid)
        except Exception as error:
            logger.warning('Friend request processing failed: %s', type(error).__name__)

    async def approve_for(self, uid):
        if self.bot is None:
            return '名单已保存，QQ连接恢复后会处理新申请。'
        friends = await self.bot.get_friend_list()
        is_friend = uid in {str(friend['user_id']) for friend in friends}
        if is_friend:
            for row in self.state['requests'].values():
                if row['qq'] == uid and row['status'] in ('pending', 'approving', 'unknown'):
                    row['status'] = 'already_friend'
                    row.pop('flag', None)
            self.save()
            await self.sync_whitelist({uid})
            return '对方已经是好友。'
        pending = [row for row in self.state['requests'].values() if row['qq'] == uid and row['status'] == 'pending']
        pending = [row for row in pending if time.time() - row['received_at'] <= 7 * 86400]
        if not pending:
            return '名单已保存，等待对方发来新的好友申请。'
        row = max(pending, key=lambda item: item['received_at'])
        row['status'] = 'approving'
        self.save()
        try:
            await asyncio.wait_for(self.bot.set_friend_add_request(flag=row['flag'], approve=True, remark=''), timeout=25)
        except Exception as error:
            row['status'] = 'unknown'
            row['error_type'] = type(error).__name__
            self.save()
            return '处理结果不明，已避免重复提交，请在QQ里核对。'
        for other in self.state['requests'].values():
            if other['qq'] == uid and other['status'] == 'pending':
                other['status'] = 'superseded'
                other.pop('flag', None)
        row['status'] = 'approved'
        row['processed_at'] = time.time()
        row.pop('flag', None)
        self.save()
        await self.sync_whitelist({uid})
        return 'QQ接口已确认通过好友申请。'

    def authorized(self, event):
        return (event.get_platform_name() == 'aiocqhttp' and event.is_private_chat()
                and event.get_platform_id() == 'dafeiyu-qq' and str(event.get_sender_id()) == OWNER)

    async def manage(self, event, action, qq=''):
        if not self.authorized(event):
            return '只有爸爸本人的QQ私信可以管理好友申请。'
        if action not in ('allow', 'remove', 'list'):
            return '操作必须是 allow、remove 或 list。'
        async with self.lock:
            if action == 'list':
                numbers = sorted(self.state['allowed'])
                mode = '当前自动通过所有好友申请；新好友自动开放聊天。' if self.state.get('auto_approve_all') else ''
                denied = sorted(self.state.get('denied', {}))
                return mode + '指定放行名单：' + ('、'.join(numbers) if numbers else '空') + '；不自动通过名单：' + ('、'.join(denied) if denied else '空')
            qq = str(qq).strip()
            if not re.fullmatch(r'[1-9]\d{4,11}', qq) or qq in (BOT, OWNER):
                return '请提供有效的其他联系人QQ号。'
            if not re.search(r'(?<!\d)' + re.escape(qq) + r'(?!\d)', event.message_str or ''):
                return '请爸爸在本条私信里明确写出要管理的QQ号。'
            if action == 'remove':
                self.state['allowed'].pop(qq, None)
                self.state.setdefault('denied', {})[qq] = {'authorized_by': OWNER, 'denied_at': time.time()}
                self.save()
                return '已取消QQ ' + qq + '的自动通过资格；现有好友关系和聊天白名单不变。'
            self.state.setdefault('denied', {}).pop(qq, None)
            self.state['allowed'][qq] = {'authorized_by': OWNER, 'allowed_at': time.time()}
            self.save()
            try:
                result = await self.approve_for(qq)
            except Exception:
                result = '名单已保存，当前QQ接口检查失败。'
            note = ' 已开启好友自动同步聊天白名单。' if self.state.get('auto_whitelist_friends') else ' 好友放行不改变聊天白名单。'
            return 'QQ ' + qq + '：' + result + note

    @filter.llm_tool(name='dafeiyu_friend_allowlist')
    async def friend_allowlist(self, event: AstrMessageEvent, action: str, qq: str = '') -> str:
        """管理指定QQ自动通过好友申请。仅爸爸QQ7000000001在私信中明确指定QQ号时可操作，不主动发送添加请求。

        Args:
            action(string): allow新增自动通过名单，remove取消自动通过，list查看名单
            qq(string): allow或remove时填本条私信明确指定的QQ号；list留空
        """
        return await self.manage(event, action, qq)

    @filter.command('好友放行')
    async def allow_command(self, event: AstrMessageEvent, qq: str):
        yield event.plain_result(await self.manage(event, 'allow', qq))

    @filter.command('好友取消')
    async def remove_command(self, event: AstrMessageEvent, qq: str):
        yield event.plain_result(await self.manage(event, 'remove', qq))

    @filter.command('好友名单')
    async def list_command(self, event: AstrMessageEvent):
        yield event.plain_result(await self.manage(event, 'list'))
