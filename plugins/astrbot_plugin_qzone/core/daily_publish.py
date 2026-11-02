"""One persistent evening diary per Shanghai calendar day."""
import asyncio
from datetime import datetime, timedelta
import random
import zoneinfo

from .operations import problem, result
from .post_images import prepare_post_images
from .diary import DiaryCancelled, ImagePreparationFailed, window
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger
from astrbot.api import logger


class DailyRandomPublisher:
    def __init__(self, config, service, sender):
        self.cfg = config
        self.service = service
        self.sender = sender
        self.timezone = zoneinfo.ZoneInfo('Asia/Shanghai')
        self.scheduler = AsyncIOScheduler(timezone=self.timezone)
        self.db_path = config.db_path
        self._terminated = False
        self._running = asyncio.Lock()

    def _now(self):
        return datetime.now(self.timezone)

    async def start(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('''CREATE TABLE IF NOT EXISTS daily_publish_runs (
                day TEXT PRIMARY KEY, run_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'scheduled',
                text TEXT NOT NULL DEFAULT '', tid TEXT,
                error_type TEXT, updated_at INTEGER NOT NULL
            )''')
            columns = await (await db.execute('PRAGMA table_info(daily_publish_runs)')).fetchall()
            if 'policy_version' not in {row[1] for row in columns}:
                await db.execute('ALTER TABLE daily_publish_runs ADD COLUMN policy_version INTEGER NOT NULL DEFAULT 1')
            await db.commit()
        self.scheduler.start()
        await self._schedule_next()

    async def _schedule_next(self):
        if self._terminated:
            return
        now = self._now()
        for offset in range(3):
            date = now.date() + timedelta(days=offset)
            day = date.isoformat()
            start, end = window(date)
            earliest = max(int(start.timestamp()), int(now.timestamp()) + 1)
            latest = int(end.timestamp()) - 1
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute('BEGIN IMMEDIATE')
                cursor = await db.execute('SELECT run_at,status,policy_version FROM daily_publish_runs WHERE day=?', (day,))
                row = await cursor.fetchone()
                if row and row[1] != 'scheduled':
                    await db.commit()
                    continue
                if earliest > latest:
                    if row:
                        await db.execute("UPDATE daily_publish_runs SET status='expired',policy_version=2,updated_at=? WHERE day=? AND status='scheduled'", (int(now.timestamp()), day))
                    await db.commit()
                    continue
                run_at = row[0] if row else None
                if run_at is None or not int(start.timestamp()) <= run_at < int(end.timestamp()):
                    run_at = random.SystemRandom().randint(earliest, latest)
                elif run_at < int(now.timestamp()):
                    run_at = earliest  # Resume once inside the window, without redrawing.
                if row:
                    await db.execute("UPDATE daily_publish_runs SET run_at=?,policy_version=2,updated_at=? WHERE day=? AND status='scheduled'",
                                     (run_at, int(now.timestamp()), day))
                else:
                    await db.execute("INSERT INTO daily_publish_runs(day,run_at,status,updated_at,policy_version) VALUES (?,?,'scheduled',?,2)",
                                     (day, run_at, int(now.timestamp())))
                await db.execute('DELETE FROM daily_publish_runs WHERE day < ?',
                    ((now.date() - timedelta(days=31)).isoformat(),))
                await db.commit()
            target = datetime.fromtimestamp(run_at, self.timezone)
            self.scheduler.add_job(self._run_task, trigger=DateTrigger(run_date=target),
                id='dafeiyu_qzone_daily', name='鲸鲸晚间空间日记',
                kwargs={'day':day}, replace_existing=True, max_instances=1, misfire_grace_time=300)
            logger.info('QQ空间晚间日记已排定，下次北京时间：%s', target.isoformat())
            return
        raise RuntimeError('无法安排下一篇晚间日记')

    async def _set_status(self, day, status, **values):
        allowed = {'text', 'tid', 'error_type'}
        assert values.keys() <= allowed
        fields = ['status=?', 'updated_at=?'] + [key + '=?' for key in values]
        args = [status, int(self._now().timestamp()), *values.values(), day]
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute('UPDATE daily_publish_runs SET ' + ','.join(fields) + ' WHERE day=?', args)
            await db.commit()

    async def _run_task(self, day):
        async with self._running:
            if self._terminated:
                return
            # Claim before generation/publication. A process restart or ambiguous API
            # timeout cannot cause an automatic duplicate on the same date.
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute("UPDATE daily_publish_runs SET status='generating',updated_at=? WHERE day=? AND status='scheduled'",
                    (int(self._now().timestamp()), day))
                claimed = cursor.rowcount == 1
                await db.commit()
            if not claimed:
                await self._schedule_next()
                return
            publication_started = False
            try:
                start, end = window(day)
                if not start <= self._now() < end:
                    await self._set_status(day, 'expired')
                    return
                editorial = self.service.llm.editorial
                if await editorial.has_diary(day):
                    await self._set_status(day, 'skipped')
                    return
                text = await self.service.llm.generate_post(diary=True, now=self._now().timestamp())
                if not text or len(text) > 1000:
                    raise ValueError('Invalid generated post length')
                if not start <= self._now() < end:
                    await self._set_status(day, 'expired', text=text)
                    return
                try:
                    own_image = await editorial.own_image(text, self._now().timestamp())
                    if own_image:
                        await editorial.review_image(text, own_image)
                        images = [own_image]
                    else:
                        images = await prepare_post_images(self.cfg.context, text, editorial=editorial)
                except DiaryCancelled:
                    raise
                except Exception:
                    images = []
                    logger.warning('QQ空间每日配图未完成，本次使用纯文字，不重复生成')
                if not start <= self._now() < end:
                    await self._set_status(day, 'expired', text=text)
                    return
                await self._set_status(day, 'publishing', text=text)
                async def before_publish():
                    if not start <= self._now() < end:
                        raise DiaryCancelled('晚间日记时间窗已结束')
                    await editorial.ensure_current(text, self._now().timestamp())
                await before_publish()
                publication_started = True
                try:
                    post = await self.service.publish_post(text=text, images=images, before_publish=before_publish)
                except ImagePreparationFailed:
                    # Only an error known to precede the post write can fall back.
                    post = await self.service.publish_post(text=text, images=[], before_publish=before_publish)
                if not post.tid:
                    raise RuntimeError("No published post ID")
                await self._set_status(day, 'published', tid=post.tid)
                result("qzone_publish", "published", day=day, tid=post.tid, image_count=len(images), error_code=None)
                logger.info('QQ空间每日说说已发布：%s', day)
                if self.cfg.trigger.send_admin:
                    try:
                        await self.sender.send_admin_post(post, message='QQ空间晚间日记已发布')
                    except Exception as error:
                        logger.warning('日记已发布，管理员通知未完成：%s', type(error).__name__)
            except asyncio.CancelledError:
                # Leave the claim in place when the server stops during a request.
                raise
            except DiaryCancelled:
                await self._set_status(day, 'cancelled', text='')
            except Exception as exc:
                await self._set_status(day, 'unknown' if publication_started else 'failed', error_type=type(exc).__name__)
                await problem(self.cfg.client, 'qzone_publish', 'QZONE_WRITE')
                logger.warning('QQ空间每日发布未完成：%s，%s；保留记录避免重复发送', day, type(exc).__name__)
            finally:
                if not self._terminated:
                    await self._schedule_next()

    async def terminate(self):
        self._terminated = True
        if self.scheduler.running:
            self.scheduler.remove_all_jobs()
            self.scheduler.shutdown(wait=False)
