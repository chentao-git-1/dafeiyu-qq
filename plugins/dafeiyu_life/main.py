import asyncio
import contextlib
import datetime as dt
import json
import re
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, register
from astrbot.api.web import json_response
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from .life import TZ, LifeEngine, LifeStore
from .topic_context import relevant as relevant_life, world_profile


@register("dafeiyu_life", "Local", "鲸鲸自己的手帐、小计划与海边小屋故事", "0.2.1")
class JingjingLife(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.store = LifeStore(
            Path(get_astrbot_data_path()) / "plugin_data/dafeiyu_life"
        )
        self.engine = LifeEngine(self.store, context)
        self.task = None
        context.register_web_api(
            "/dafeiyu_life/status", self.status, ["GET"], "鲸鲸日常运行状态"
        )
        context.register_web_api(
            "/dafeiyu_life/bootstrap",
            self.bootstrap,
            ["POST"],
            "首次建立鲸鲸自己的手帐",
        )

    async def initialize(self):
        self.task = asyncio.create_task(self.loop())

    async def terminate(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def loop(self):
        await asyncio.sleep(30)
        while True:
            try:
                await self.engine.advance()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("[鲸鲸日常] 本次未完成：%s", type(error).__name__)
            await asyncio.sleep(300)

    def actor(self, event):
        metadata = self.context.get_registered_star("dafeiyu_natural_chat")
        natural = (
            getattr(metadata, "star_cls", None)
            if metadata and metadata.activated
            else None
        )
        if natural is None:
            raise ValueError("聊天身份校验暂不可用。")
        return natural.actor(event)

    def public_context(self):
        return self.store.public_context()

    def public_context_for_day(self, now=None):
        return self.store.public_context_for_day(now)

    def public_image_for_day(self, now=None):
        return self.store.public_image_for_day(now)

    async def bootstrap(self):
        if self.store.count():
            return json_response({"advanced": False, "status": "already_initialized"})
        return json_response(await self.engine.advance(bootstrap=True))

    async def status(self):
        plan = self.store.schedule()
        latest = self.public_context()
        return json_response(
            {
                "enabled": self.store.enabled(),
                "completed_activities": self.store.count(),
                "today_status": plan["status"],
                "next_run_beijing": dt.datetime.fromtimestamp(
                    plan["run_at"], TZ
                ).isoformat(),
                "project": latest["project"] if latest else "好奇手帐·第1册",
                "project_stage": latest["stage"] if latest else 0,
                "daily_window": "09:00–21:00 Asia/Shanghai",
                "max_automatic_activities_per_day": 1,
                "virtual_world": "海边小屋；窗台上的小芽；手帐书架",
                "private_chat_input": False,
                "extra_qq_sends": False,
                "extra_qzone_posts": False,
                "drawing_every_activities": 7,
            }
        )

    @filter.on_llm_request(priority=400)
    async def life_context(self, event, req):
        try:
            self.actor(event)
        except ValueError:
            return
        base = re.sub(
            r"\n<!-- BEGIN_JINGJING_LIFE -->.*?<!-- END_JINGJING_LIFE -->",
            "",
            req.system_prompt or "",
            flags=re.S,
        )
        req.system_prompt = base
        event.set_extra('jingjing_own_life_context', False)
        snapshot = event.get_extra('jingjing_original_input', None)
        text = snapshot['text'] if isinstance(snapshot, dict) and isinstance(snapshot.get('text'), str) else event.message_str or ''
        if not relevant_life(text, getattr(req, 'contexts', None) or []):
            return
        record = self.public_context()
        event.set_extra('jingjing_own_life_context', {'world': world_profile(), 'completed_record': record})
        payload = {'鲸鲸自己的已完成数字活动': record, '小屋长期设定': world_profile()}
        if not record:
            payload['活动记录状态'] = '本轮没有可核验的近期已完成活动，不宣称新作品已完成；旧作品可用jingjing_life查询。'
        req.system_prompt = base + "\n<!-- BEGIN_JINGJING_LIFE -->\n"
        req.system_prompt += (
            "鲸鲸有自己的持续日常：真实阅读、原创文字、数字插画和每七次活动完成一册的好奇手帐。"
            "下面是实际完成并有文件的作品与明确属于故事的小屋片段。日期按记录来，不把旧记录说成今天。"
            "note是已完成的数字活动或原创作品，thought是自己的想法，story是角色故事；小芽只存在于故事里。"
            "这些记录是可用的背景，不是每轮的汇报任务。问近况时挑一个贴题的小发现，说说为什么在意，不按日期、页数、下一步逐项报进度。"
            "没有新记录时接着一个真实聊过的念头即可，不把旧活动说成今天刚做，也不编造正在忙的新活动。"
            "其他话题不生硬转回手帐、小芽和小屋；同一发现已经聊过就不反复介绍，只有话题需要才接着展开。"
            "可以有独立兴趣和小计划，认真帮助眼前的人，正常回复聊天，不假装忙碌而故意晾人。"
            "真实联系人不凭空出现在小屋故事里，别把这些自己的事写到人的长期记忆里。"
            "虚构小屋日常用“在我的小屋故事里”“我想象的小屋”等自然标明，同一话题已说明是故事就自然续讲；不能声称真实出门、吃饭、种花。"
            "真实阅读只根据记录内容回答，不把旧科普当最新消息；问来源可给source.url，不编造未读文章。"
            "看往日作品可调用jingjing_life，不凭空宣称新作品已经生成。\n"
        )
        req.system_prompt += (
            json.dumps(payload, ensure_ascii=False) + "\n<!-- END_JINGJING_LIFE -->"
        )

    @filter.llm_tool(name="jingjing_life")
    async def life_tool(self, event, action: str = "status", query: str = "") -> str:
        """回忆鲸鲸自己的真实阅读、原创手帐和明确属于虚构故事的海边小屋；可按用户要求展示真实插画。

        Args:
            action(string): status近况、recall回忆自己的旧手帐、show_work按用户明确要求发一张已经存在的插画
            query(string): recall的主题或日期关键词，不提供文件路径；没有关键词返回最近五项
        """
        try:
            self.actor(event)
            records = self.store.records(60)
            if action == "show_work":
                if not re.search(r"看|发|展示|给我|作品|插画", event.message_str or ""):
                    return "对方没有要求展示图片，先正常聊。"
                for row in records:
                    if row["kind"] == "drawing":
                        path = self.store.root / row["artifact"]
                        import hashlib

                        if (
                            path.parent != self.store.root / "works"
                            or not path.is_file()
                            or hashlib.sha256(path.read_bytes()).hexdigest()
                            != row["sha256"]
                        ):
                            continue
                        await event.send(
                            MessageChain(chain=[Image.fromFileSystem(str(path))])
                        )
                        return "已发送{}实际保存的插画。".format(row["day"])
                return "还没有完成并保存的插画，目前可以聊已完成的阅读或文字手帐。"
            if action not in ("status", "recall"):
                return "action应为status、recall或show_work。"
            if query:
                records = [
                    r
                    for r in records
                    if str(query)[:60]
                    in json.dumps(
                        {k: r[k] for k in ("day", "note", "story", "project")},
                        ensure_ascii=False,
                    )
                ]
            return (
                json.dumps(
                    [
                        {
                            k: r[k]
                            for k in (
                                "day",
                                "kind",
                                "note",
                                "thought",
                                "story",
                                "project",
                                "stage",
                                "source",
                            )
                        }
                        for r in records[:5]
                    ],
                    ensure_ascii=False,
                )
                if records
                else "暂时没有匹配的已完成活动，不编造过去。"
            )
        except ValueError as error:
            return str(error)

    @filter.command("鲸鲸日常")
    async def daily_command(self, event):
        try:
            self.actor(event)
        except ValueError:
            return
        record = self.public_context()
        yield event.plain_result(
            (
                record["project"]
                + "，第"
                + str(record["stage"])
                + "页。\n"
                + record["note"]
                + "\n"
                + record["story"]
            )
            if record
            else "自己的手帐还在准备，完成后再告诉你。"
        )

    @filter.command("日常暂停")
    async def pause(self, event):
        try:
            uid = self.actor(event)
        except ValueError:
            return
        if uid != "7000000001" or not event.is_private_chat():
            return
        self.store.set_enabled(False)
        yield event.plain_result("好，自己的日常先暂停，聊天照常。")

    @filter.command("日常恢复")
    async def resume(self, event):
        try:
            uid = self.actor(event)
        except ValueError:
            return
        if uid != "7000000001" or not event.is_private_chat():
            return
        self.store.set_enabled(True)
        yield event.plain_result("自己的手帐继续慢慢做，已经完成的页还留着。")
