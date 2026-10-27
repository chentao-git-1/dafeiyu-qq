"""Persistent, bounded replies to new comments on this bot's own Qzone posts."""
import asyncio
import contextlib
import hashlib
import time

import aiosqlite
from astrbot.api import logger
from .operations import classify, problem, result
from .qzone import QzoneParser


class AutoReplies:
    def __init__(self, config, service):
        self.cfg = config
        self.service = service
        self.task = None
        self.lock = asyncio.Lock()
        self.enabled_at = 0
        self.next_check = None
        self.last_result = None

    async def start(self):
        async with aiosqlite.connect(self.cfg.db_path) as db:
            await db.execute('CREATE TABLE IF NOT EXISTS qzone_auto_reply_meta (key TEXT PRIMARY KEY,value INTEGER NOT NULL)')
            await db.execute('INSERT OR IGNORE INTO qzone_auto_reply_meta VALUES (?,?)', ('enabled_at', int(time.time())))
            self.enabled_at = (await (await db.execute("SELECT value FROM qzone_auto_reply_meta WHERE key='enabled_at'")).fetchone())[0]
            await db.execute('''CREATE TABLE IF NOT EXISTS qzone_auto_reply_comments (
                key TEXT PRIMARY KEY,post_tid TEXT NOT NULL,cid INTEGER,parent_id INTEGER,
                author INTEGER,status TEXT NOT NULL,reply TEXT NOT NULL DEFAULT '',
                observed_at INTEGER NOT NULL,updated_at INTEGER NOT NULL)''')
            await db.execute('CREATE TABLE IF NOT EXISTS qzone_auto_reply_seen_posts (tid TEXT PRIMARY KEY,first_seen INTEGER NOT NULL)')
            await db.commit()
        self.task = asyncio.create_task(self.loop())

    async def loop(self):
        self.next_check = int(time.time()) + 3
        await asyncio.sleep(3)
        while True:
            started = time.monotonic()
            try:
                await asyncio.wait_for(self.run_once(), timeout=150)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                code = classify(error)
                await problem(self.cfg.client, 'qzone_replies', code if code in ('QZONE_LOGIN', 'QZONE_WRITE') else 'QZONE_READ')
                self.last_result = {'status': 'error', 'error_type': type(error).__name__}
                logger.warning('Qzone auto reply round failed: %s', type(error).__name__)
            delay = max(5, 600 - (time.monotonic() - started))
            self.next_check = int(time.time() + delay)
            await asyncio.sleep(delay)

    async def status(self):
        async with aiosqlite.connect(self.cfg.db_path) as db:
            rows = await (await db.execute('SELECT status,count(*) FROM qzone_auto_reply_comments GROUP BY status')).fetchall()
        return {'enabled': True, 'interval_seconds': 600, 'max_replies_per_round': 3,
                'scan_latest_posts': 10, 'enabled_at': self.enabled_at,
                'task_running': self.task is not None and not self.task.done(),
                'next_check': self.next_check, 'counts': dict(rows), 'last_result': self.last_result}

    async def run_once(self):
        if self.cfg.client is None:
            self.last_result = {'status': 'waiting_for_qq'}
            return self.last_result
        async with self.lock:
            own = int(await self.service.session.get_uin())
            if own != 7000000002:
                raise RuntimeError('Unexpected Qzone account')
            posts = await self.service.query_feeds(target_id=str(own), pos=0, num=10)
            queued = []
            read_errors = 0
            checked = 0
            async with aiosqlite.connect(self.cfg.db_path, timeout=10) as db:
                for post in posts[:10]:
                    if not post.tid or int(post.uin) != own:
                        continue
                    try:
                        detail = await self.service.qzone.get_detail(post)
                        if not detail.ok:
                            raise RuntimeError('Qzone detail unavailable')
                        parsed = QzoneParser.parse_feeds([detail.data])
                        if not parsed:
                            raise RuntimeError('Qzone detail parse failed')
                        # The owner and post ID are already pinned by the own-post query.
                        if parsed[0].tid and str(parsed[0].tid) != str(post.tid):
                            raise RuntimeError('Qzone detail post mismatch')
                        post.comments = parsed[0].comments
                        checked += 1
                    except Exception:
                        read_errors += 1
                        continue
                    seeded = await (await db.execute('SELECT 1 FROM qzone_auto_reply_seen_posts WHERE tid=?', (post.tid,))).fetchone()
                    for comment in post.comments[:500]:
                        if comment.uin == own or not comment.uin or not comment.tid:
                            continue
                        key = hashlib.sha256('{}:{}:{}:{}'.format(post.tid, comment.parent_tid or 0, comment.tid, comment.uin).encode()).hexdigest()
                        old = comment.create_time and comment.create_time <= self.enabled_at
                        unknown_old = not comment.create_time and not seeded
                        prior_reply = not comment.parent_tid and any(
                            c.uin == own and c.parent_tid == comment.tid for c in post.comments)
                        state = 'already_replied' if prior_reply else 'baseline' if old or unknown_old else 'queued'
                        await db.execute('INSERT OR IGNORE INTO qzone_auto_reply_comments (key,post_tid,cid,parent_id,author,status,observed_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
                            (key, post.tid, comment.tid, comment.parent_tid or 0, comment.uin, state, int(time.time()), int(time.time())))
                        current = (await (await db.execute('SELECT status FROM qzone_auto_reply_comments WHERE key=?', (key,))).fetchone())[0]
                        if prior_reply and current == 'queued':
                            await db.execute("UPDATE qzone_auto_reply_comments SET status='already_replied',updated_at=? WHERE key=? AND status='queued'", (int(time.time()), key))
                        elif current == 'queued':
                            queued.append((post, comment, key))
                    await db.execute('INSERT OR IGNORE INTO qzone_auto_reply_seen_posts VALUES (?,?)', (post.tid, int(time.time())))
                    await db.commit()
                sent = 0
                failed = 0
                for post, comment, key in sorted(queued, key=lambda item: item[1].create_time)[:3]:
                    cursor = await db.execute("UPDATE qzone_auto_reply_comments SET status='generating',updated_at=? WHERE key=? AND status='queued'", (int(time.time()), key))
                    await db.commit()
                    if cursor.rowcount != 1:
                        continue
                    # Persist the claim before generation; never repeat an uncertain write.
                    writing = False
                    try:
                        content = await asyncio.wait_for(self.service.llm.generate_reply(post, comment), timeout=45)
                        content = str(content or '').strip()[:140]
                        if not content:
                            raise ValueError('Empty generated reply')
                        target = comment
                        options = {}
                        if comment.parent_tid:
                            target = next((c for c in post.comments if not c.parent_tid and c.tid == comment.parent_tid), None)
                            if target is None:
                                raise ValueError('Missing parent comment')
                            options = {'reply_uin': comment.uin, 'reply_nick': comment.nickname, 'comment_uin': target.uin}
                        await db.execute("UPDATE qzone_auto_reply_comments SET status='sending',reply=?,updated_at=? WHERE key=?", (content, int(time.time()), key))
                        await db.commit()
                        writing = True
                        response = await self.service.qzone.reply(post, target, content, **options)
                        if not response.ok:
                            raise RuntimeError('Qzone reply not confirmed')
                        await db.execute("UPDATE qzone_auto_reply_comments SET status='sent',updated_at=? WHERE key=?", (int(time.time()), key))
                        await db.commit()
                        sent += 1
                    except asyncio.CancelledError:
                        await db.execute('UPDATE qzone_auto_reply_comments SET status=?,updated_at=? WHERE key=?', ('unknown' if writing else 'generation_failed', int(time.time()), key))
                        await db.commit()
                        raise
                    except Exception as error:
                        await db.execute('UPDATE qzone_auto_reply_comments SET status=?,updated_at=? WHERE key=?', ('unknown' if writing else 'generation_failed', int(time.time()), key))
                        await db.commit()
                        failed += 1
                        logger.warning('Qzone auto reply skipped after failure: %s', type(error).__name__)
                self.last_result = {'status': 'completed' if sent else 'no_new_comments', 'checked_at': int(time.time()),
                                    'posts_checked': checked, 'replied': sent, 'read_failures': read_errors, 'reply_failures': failed}
                if failed or read_errors:
                    await problem(self.cfg.client, 'qzone_replies', 'QZONE_WRITE' if failed else 'QZONE_READ')
                else:
                    result('qzone_replies', **self.last_result, error_code=None)
                return self.last_result

    async def terminate(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
