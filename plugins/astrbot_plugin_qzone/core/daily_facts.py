"""Collect minimal verified activity facts without importing private chat text."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import time
import zoneinfo

import aiosqlite

BOT = 7000000002
ALLOWED = {'new_friend', 'voice_greeting', 'voice_updated'}
FAMILY = {7000000001: '爸爸', 7000000011: '猛叔叔', 7000000012: '田阿姨', 7000000013: '定叔叔'}
PRAISE = {'你真可爱': '夸鲸鲸可爱', '真可爱': '夸鲸鲸可爱', '好可爱': '夸鲸鲸可爱',
          '好可爱呀': '夸鲸鲸可爱', '真棒': '夸鲸鲸做得不错', '你好棒': '夸鲸鲸做得不错'}


class DailyFacts:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        if self.db_path.parent.name == 'astrbot_plugin_qzone' and self.db_path.parent.parent.name == 'plugin_data':
            self.data_root = self.db_path.parents[2]
        else:
            # Isolated test/preview databases must not read runtime activity files.
            self.data_root = self.db_path.parent / '_activity_sources'

    async def initialize(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''CREATE TABLE IF NOT EXISTS qzone_daily_facts (
                event_key TEXT PRIMARY KEY, occurred_at INTEGER NOT NULL, kind TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1
            )''')
            await db.execute('DELETE FROM qzone_daily_facts WHERE occurred_at<?', (int(time.time()) - 31*86400,))
            await db.commit()

    @staticmethod
    def load(path):
        try:
            return json.loads(path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return {}

    async def collect(self, now=None):
        await self.initialize()
        now = float(time.time() if now is None else now)
        tz = zoneinfo.ZoneInfo('Asia/Shanghai')
        day = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start, end = day.timestamp(), min((day + timedelta(days=1)).timestamp(), now)
        counts = {'new_friend': 0, 'voice_greeting': 0, 'voice_updated': 0}
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute('SELECT kind,count FROM qzone_daily_facts WHERE occurred_at>=? AND occurred_at<=?', (start, end))
            for kind, count in await cursor.fetchall():
                if kind in ALLOWED:
                    counts[kind] += max(0, min(int(count), 100))
            cursor = await db.execute("SELECT comments FROM posts WHERE uin=? AND tid IS NOT NULL AND tid<>'' ORDER BY create_time DESC LIMIT 20", (BOT,))
            public_comments = await cursor.fetchall()
        friends = self.load(self.data_root / 'plugin_data/dafeiyu_friend_gate/state.json')
        for row in friends.get('welcomes', {}).values():
            observed = float(row.get('observed_at') or 0)
            if start <= observed <= end and observed > float(friends.get('welcome_enabled_at') or 0):
                counts['new_friend'] += 1
            sent = float(row.get('sent_at') or 0)
            if row.get('status') == 'sent' and row.get('message_id') is not None and start <= sent <= end:
                counts['voice_greeting'] += 1
        facts = []
        seen_praise = set()
        for (raw,) in public_comments:
            try:
                comments = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(comments, list):
                continue
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                try:
                    uid = int(comment.get('uin') or 0)
                    stamp = float(comment.get('create_time') or 0)
                except (TypeError, ValueError):
                    continue
                content = str(comment.get('content') or '').strip(' \n\r\t。！!～~')
                if uid == BOT or not start <= stamp <= end or content not in PRAISE:
                    continue
                who = FAMILY.get(uid, '有朋友')
                description = who + '在鲸鲸自己的公开空间评论里' + PRAISE[content]
                if description not in seen_praise:
                    facts.append({'kind': 'public_praise', 'detail': description, 'source': 'own_public_comment'})
                    seen_praise.add(description)
        if counts['new_friend']:
            facts.append({'kind': 'new_friend', 'detail': '今天确认认识了新朋友，身份和私聊内容不公开', 'count': counts['new_friend'], 'source': 'confirmed_friend_observation'})
        if counts['voice_updated']:
            facts.append({'kind': 'voice_updated', 'detail': '今天确实换用了更温柔、表达更自然的声音', 'source': 'verified_deployment'})
        if counts['voice_greeting']:
            facts.append({'kind': 'voice_greeting', 'detail': '今天确实用语音跟朋友打过招呼', 'count': counts['voice_greeting'], 'source': 'confirmed_qq_delivery'})
        people = self.load(self.data_root / 'plugin_data/dafeiyu_bailian_tts/relations.json').get('people', {})
        if any(start <= float(row.get('last_seen') or 0) <= end for row in people.values()):
            facts.append({'kind': 'visitors', 'detail': '今天有朋友来找鲸鲸说话；不使用私聊的话题、原话或对方心情', 'source': 'anonymous_interaction_statistic'})
        return facts[:3]
