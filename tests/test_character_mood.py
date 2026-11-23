"""Exercise the native QQ generation and FishWorld store with fictional, isolated turns."""
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import MethodType
import unittest
from unittest.mock import AsyncMock, patch

from test_fishworld_followthrough import (
    Dialogue, Event, FishWorldPlugin, LLMResponse, NS, StateStore, app, empty_learning,
    followthrough, result, toolset,
)
from data.plugins.astrbot_plugin_fishworld.self_mood import HALF_LIFE, MAX_MARKS, REPLAY_WINDOW, mood_view, next_mood
from data.plugins.astrbot_plugin_fishworld.self_mood import apply_feelings, audience, valid_mood
from data.plugins.dafeiyu_dialogue import self_mood
from data.plugins.dafeiyu_dialogue.prompt_cache import project_turn_data
from data.plugins.dafeiyu_dialogue.world_activity import FLOW
from dafeiyu.dialogue.review import ReviewResult
from dafeiyu.media.voice.expression import speech_plan
from dafeiyu.memory.exchanges import ExchangeStore


class QQ(Event):
    def get_platform_id(self):
        return "dafeiyu-qq"

    def get_self_id(self):
        return "22"

    def plain_result(self, text):
        return text


def feeling(source, quote, delta=6, tone="感动"):
    return dict(source=source, quote=quote, delta=delta, tone=tone)


class MoodScale(unittest.TestCase):
    def test_delayed_feeling_ages_before_it_changes_the_score(self):
        now = 1_000_000
        missed = {"source": "d"*24, "at": now-36*3600, "tone": "失落", "strength": 8,
                  "span": "短", "delta": -10}
        row = apply_feelings(None, "c"*24, [missed], now, "f"*24)
        self.assertEqual(mood_view(row, now), {"score": 50, "tone": "平静"})
        fresh = apply_feelings(None, "c"*24, [dict(missed, at=now)], now, "f"*24)
        self.assertFalse(valid_mood(dict(fresh, feelings=[dict(fresh["feelings"][0], at=10**18)])))

    def test_feelings_decay_independently_and_legacy_score_survives(self):
        now = 1_000_000
        old = {"score": 52, "tone": "好奇", "at": now, "seen": [[now, "a"*24, "b"*24]], "floor": 0}
        first = {"tone": "失落", "strength": 6, "span": "短", "delta": -4, "source": "d"*24, "at": now}
        second = dict(first, tone="期待", span="长", delta=4)
        self.assertTrue(valid_mood(old))
        self.assertEqual(mood_view(old, now), {"score": 52, "tone": "好奇"})
        self.assertIsNone(apply_feelings(old, "a"*24, [first], now, "f"*24))
        row = apply_feelings(old, "c"*24, [first, second], now, "f"*24)
        self.assertTrue(valid_mood(row))
        self.assertEqual(row["score"], 52)
        self.assertEqual(len(row["seen"]), 2)
        self.assertIsNone(apply_feelings(row, "e"*24, [first], now, "f"*24))
        view = mood_view(row, now+HALF_LIFE)
        self.assertEqual(view["score"], 51)
        strengths = {x["tone"]: x["strength"] for x in view["feelings"]}
        self.assertEqual((strengths["期待"], strengths["失落"]), (4, 1))
        self.assertEqual(mood_view(row, now+4*86400), {"score": 50, "tone": "平静"})

    def test_decay_bounds_and_replay(self):
        now = 1_000_000
        turn, source = "a"*24, "b"*24
        row = next_mood(None, turn, source, now, 10, "开心", now)
        self.assertEqual(mood_view(None, now), {"score": 50, "tone": "平静"})
        self.assertEqual(mood_view(row, now+HALF_LIFE)["score"], 55)
        self.assertEqual(mood_view(row, now+HALF_LIFE*8), {"score": 50, "tone": "平静"})
        self.assertIsNone(next_mood(row, turn, "c"*24, now, 7, "感动", now))
        self.assertIsNone(next_mood(row, "c"*24, source, now, 7, "感动", now))
        for score, change, expected in [(96, 10, 100), (4, -10, 0)]:
            changed = next_mood(dict(row, score=score), "c"*24, "d"*24, now, change, "复杂", now)
            self.assertEqual(mood_view(changed, now)["score"], expected)
        for index in range(1, MAX_MARKS+3):
            row = next_mood(row, f"{index:024x}", f"{10000+index:024x}", now+index, 1, "开心", now+index)
        self.assertLessEqual(len(row["seen"]), MAX_MARKS)
        self.assertIsNone(next_mood(row, turn, source, now, -2, "失落", now+MAX_MARKS+3))
        self.assertIsNone(next_mood(None, turn, source, now-REPLAY_WINDOW-1, 3, "惊喜", now))


class CharacterMood(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.plugin, self.fish, self.world = app(Path(directory.name))

    async def begin(self, text, *, auto=False, events=(), ident=None, session=None, group=False, **flags):
        event = QQ(text, auto=auto)
        event.message_obj.raw_message = {"time": int(time.time())}
        if ident:
            event.message_obj.message_id = ident
        if session:
            event.unified_msg_origin = f"dafeiyu-qq:{'Group' if group else 'Friend'}Message:{session}"
            event.get_sender_id = lambda: session
        if group:
            event.is_private_chat = lambda: False
        event.d.update(flags, fishworld_mood_events=list(events))
        request = NS(func_tool=toolset(self.world), system_prompt="TEST_CONTRACT", contexts=[],
                     _dafeiyu_outfit_choice=None)
        await followthrough.begin(self.plugin, event, request, {"text": text}, [], self.plugin.identity(event))
        return event, request

    async def finish(self, event, proposal=None, *, quiet=False, delivery=None):
        reply = "" if quiet else "我听到了。"
        obj = {"reply": reply, "claims": [], "learning": empty_learning(), "focus": ["social"], "expression": "chat"}
        if quiet:
            obj["fishworld"] = {"share": "quiet"}
        if proposal is not None:
            obj["self_mood"] = proposal
        if delivery:
            obj["delivery"] = delivery
        response = LLMResponse(role="assistant", completion_text=json.dumps(obj, ensure_ascii=False))
        await Dialogue.review_response(self.plugin, event, response)
        self.assertTrue(event.get_extra("dafeiyu_generation_valid"))
        self.assertEqual(response.completion_text, reply)
        self.assertEqual(event.sent, [])
        return response.completion_text

    def score(self, event):
        return self.fish.read_self_mood(event, self.fish.mood_scope(event))["score"]

    async def test_mixed_chat_feelings_recall_and_voice(self):
        root = self.fish.path.parent
        self.plugin.root = root
        exchanges = ExchangeStore(root / "plugin_data/dafeiyu_natural_chat/interaction.db")
        text = "周六来不了，周日还想一起看花"
        event, request = await self.begin(text, ident="qq_dinner")
        feelings = [dict(feeling("chat", "周六来不了", -4, "失落"), strength=7, span="短"),
                    dict(feeling("chat", "周日还想一起看花", 4, "期待"), strength=6, span="长")]
        spoken = await self.finish(event, {"feelings": feelings}, delivery="前半句放轻，后半句带一点笑意。")
        intent = event.get_extra("jingjing_expression_intent")
        instruction = speech_plan(spoken, {"semantic_generation": True, "expression": intent})["instruction"]
        self.assertIn("前半句放轻，后半句带一点笑意。", instruction)
        self.assertNotIn("周六来不了", instruction)
        self.assertNotIn("self_mood", spoken)
        self.assertEqual(self.score(event), 50)
        stored = self.fish.path.read_text(encoding="utf-8")
        self.assertNotIn("周六来不了", stored)
        self.assertNotIn("周日还想一起看花", stored)
        self.fish.store = StateStore(self.fish.path, time.time())
        self.assertTrue(exchanges.record("11", event.unified_msg_origin, "qq_dinner", text, spoken))
        owner, next_request = await self.begin("最近心情怎么样")
        self.assertIn("周六来不了", next_request.system_prompt)
        self.assertIn("周日还想一起看花", next_request.system_prompt)
        self.assertEqual(len(owner.get_extra(self_mood.TURN)["available"]), 2)
        shown = [value async for value in FishWorldPlugin.mood_command(self.fish, owner)]
        self.assertIn("失落 7/10", shown[0])
        self.assertIn("期待 6/10", shown[0])
        for e, r in [await self.begin("你好", session="33"), await self.begin("鱼世界提醒", auto=True)]:
            self.assertIn("失落", r.system_prompt)
            self.assertIn("期待", r.system_prompt)
            self.assertNotIn("周六来不了", r.system_prompt)
            self.assertNotIn("周日还想一起看花", r.system_prompt)
            self.assertFalse(e.get_extra(self_mood.TURN)["available"])
        def wire(req):
            messages = [{"role":"system", "content":req.system_prompt}, {"role":"user", "content":"你好"}]
            project_turn_data(messages, "TEST_CONTRACT")
            return messages
        first, following = wire(request), wire(next_request)
        self.assertEqual(first[0], following[0])
        self.assertIn("周六来不了", following[1]["content"])
        exchanges.forget("11", event.unified_msg_origin, "一起看花")
        forgotten, after = await self.begin("心情呢")
        self.assertFalse(forgotten.get_extra(self_mood.TURN)["available"])
        self.assertNotIn("周六来不了", after.system_prompt)

    async def test_world_receipts_overlap_and_scoped_resolution(self):
        now = time.time()
        rain = {"id":"a"*64, "received":now, "text":"下雨推迟了野餐"}
        sun = {"id":"b"*64, "received":now, "text":"朋友已确认改期"}
        first, _ = await self.begin("通知", auto=True, events=[rain])
        await self.finish(first, {"feelings":[dict(feeling("event1","推迟了野餐",-6,"失落"), strength=8)]}, quiet=True)
        self.assertEqual(self.score(first), 44)
        replay, request = await self.begin("通知", auto=True, events=[rain,sun])
        self.assertIn("推迟了野餐", request.system_prompt)
        key = next(iter(replay.get_extra(self_mood.TURN)["available"]))
        outsider, request = await self.begin("今天如何", session="33")
        self.assertNotIn("推迟了野餐", request.system_prompt)
        self.assertFalse(outsider.get_extra(self_mood.TURN)["available"])
        await self.finish(outsider, {"feelings":[dict(feeling("chat","今天如何",1,"好奇"), settles=key)]})
        self.assertIn("失落", [f["tone"] for f in self.fish.store.data["self_mood"]["feelings"]])
        await self.finish(replay, {"feelings":[dict(feeling("event1","推迟了野餐",-6,"失落"), strength=8),
            dict(feeling("event2","确认改期",5,"安心"), settles=key, strength=7)]}, quiet=True)
        row = self.fish.store.data["self_mood"]
        self.assertEqual(self.score(replay), 50)
        self.assertNotIn("失落", [f["tone"] for f in row["feelings"]])
        self.assertEqual(len(row["seen"]), 3)
        reloaded = StateStore(self.fish.path, time.time())
        self.assertTrue(valid_mood(reloaded.data["self_mood"]))
        self.assertIn("确认改期", json.dumps(mood_view(row,now,audience(first.unified_msg_origin)),ensure_ascii=False))
        self.assertNotIn("确认改期", json.dumps(mood_view(row,now,audience(outsider.unified_msg_origin)),ensure_ascii=False))

    async def test_chat_cause_requires_original_text_and_privacy_cutoff(self):
        self.plugin.root = self.fish.path.parent
        directory = self.plugin.root / "plugin_data"
        exchanges = ExchangeStore(directory / "dafeiyu_natural_chat/interaction.db")
        text = "总算找回了原稿"
        event, _ = await self.begin(text, ident="qq_note")
        await self.finish(event, {"feelings":[dict(feeling("chat", "找回了原稿"), strength=7)]})
        origin = event.unified_msg_origin
        self.assertTrue(exchanges.record("33", origin, "qq_note", text, "好的"))
        self.assertTrue(exchanges.record("11", origin, "qq_note", "我想重新修改", "好的"))
        _, unavailable = await self.begin("你好")
        self.assertNotIn("找回了原稿", unavailable.system_prompt)
        self.assertNotIn("重新修改", unavailable.system_prompt)
        with exchanges.connect() as db:
            db.execute("UPDATE exchanges SET human=? WHERE qq=? AND message_id=?", (text, "11", "qq_note"))
        _, visible = await self.begin("最近呢")
        self.assertIn("找回了原稿", visible.system_prompt)
        self.assertIn("记录时间", visible.system_prompt)
        learning = directory / "dafeiyu_dialogue"
        learning.mkdir(exist_ok=True)
        with sqlite3.connect(learning / "learning.db") as db:
            db.execute("CREATE TABLE privacy_barriers(platform TEXT, at REAL)")
            db.execute("INSERT INTO privacy_barriers VALUES(?,?)", ("dafeiyu-qq", time.time()+1))
        _, withdrawn = await self.begin("最近呢")
        self.assertNotIn("找回了原稿", withdrawn.system_prompt)
        self.assertIn("感动", withdrawn.system_prompt)

    async def test_private_feeling_claim_enters_only_the_same_audience_review(self):
        self.plugin.root = self.fish.path.parent
        exchanges = ExchangeStore(self.plugin.root / "plugin_data/dafeiyu_natural_chat/interaction.db")
        text = "原稿找到了"
        original, _ = await self.begin(text, ident="qq_found")
        await self.finish(original, {"feelings":[dict(feeling("chat", text), strength=7)]})
        self.assertTrue(exchanges.record("11", original.unified_msg_origin, "qq_found", text, "我听到了。"))
        followup, request = await self.begin("你为什么会有这个感受")
        packet = request.system_prompt.split("<!-- BEGIN_DAFEIYU_SELF_MOOD -->\n", 1)[1].split("\n<!-- END_", 1)[0]
        evidence_id = json.loads(packet)["感受"][0]["evidence_id"]
        reply = "你之前说过原稿找到了。"
        claim = {"kind": "memory", "span": text, "evidence_ids": [evidence_id]}
        obj = {"focus": ["social"], "claims": [claim], "expression": "chat", "learning": empty_learning(), "reply": reply}
        response = LLMResponse(role="assistant", completion_text=json.dumps(obj, ensure_ascii=False))
        natural = self.plugin.natural()
        natural.actor_owner = lambda: ""
        self.plugin.natural = lambda: natural
        self.plugin.model_verification_enabled = lambda: True
        self.plugin.reviewed = MethodType(Dialogue.reviewed, self.plugin)
        reviewer = AsyncMock(return_value=ReviewResult((reply,), "pass"))
        with patch("data.plugins.dafeiyu_dialogue.evidence_context.review", reviewer):
            await Dialogue.review_response(self.plugin, followup, response)
            self.assertTrue(followup.get_extra("dafeiyu_generation_valid"))
            self.assertEqual(response.completion_text, reply)
            self.assertEqual(self.plugin.review_stats["metadata_recovered"], 0)
            reviewer.assert_awaited_once()
            proof = reviewer.await_args.args[1]["mood_evidence"][evidence_id]
            self.assertEqual((proof["kind"], proof["data"]["excerpt"]), ("memory", text))
            await self.plugin.reviewed(followup, ["你好。"], "dafeiyu-qq:FriendMessage:33")
            cross_session_evidence = reviewer.await_args.args[1]
            self.assertNotIn(text, json.dumps(cross_session_evidence, ensure_ascii=False))

        another, _ = await self.begin("还记得吗")
        async def forget_during_review(segments, *args, **kwargs):
            exchanges.forget("11", original.unified_msg_origin, text)
            return ReviewResult(tuple(segments), "pass")
        with patch("data.plugins.dafeiyu_dialogue.evidence_context.review", side_effect=forget_during_review):
            with self.assertRaises(ValueError):
                await self.plugin.reviewed(another, [reply], original.unified_msg_origin)

    async def test_bounded_change_and_unsupported_or_sensitive_causes(self):
        event, _ = await self.begin("收到贺卡和花束")
        base = dict(feeling("chat","收到贺卡",9,"惊喜"), strength=7)
        await self.finish(event, {"feelings":[base, dict(base,tone="开心",quote="花束"),
                          dict(base, tone="感动", quote="原话里没有这一句")]})
        self.assertEqual(self.score(event), 60)
        self.assertEqual(len(self.fish.store.data["self_mood"]["feelings"]), 2)
        before = self.fish.path.read_bytes()
        sens, _ = await self.begin("这是我的验证码，不要保存")
        await self.finish(sens, {"feelings":[dict(base,quote="不要保存")]})
        self.assertEqual(before, self.fish.path.read_bytes())
        bad, _ = await self.begin("真的收到了")
        await self.finish(bad, {"feelings":[dict(base, quote="收到了", span=[]), dict(base, quote="收到了", strength=True)]})
        self.assertEqual(before, self.fish.path.read_bytes())

    async def test_chat_round_trip_and_wire_cache(self):
        line = "你之前惦记的小花终于开了"
        event, before = await self.begin(line, ident="qq_stable")
        await self.finish(event, feeling("chat", "小花终于开了"))
        self.assertEqual(self.score(event), 56)
        replay, _ = await self.begin(line, ident="qq_stable")
        await self.finish(replay, feeling("chat", "小花终于开了"))
        self.fish.store = StateStore(self.fish.path, time.time())
        other, after = await self.begin("今天想聊点什么", session="33")
        self.assertEqual(self.score(other), 56)
        self.assertIsNone(other.get_extra(FLOW))
        self.assertNotIn("小花", self.fish.path.read_text(encoding="utf-8"))
        def wire(request):
            messages = [{"role": "system", "content": request.system_prompt},
                        {"role": "assistant", "content": "上一次的回复"}, {"role": "user", "content": "新消息"}]
            project_turn_data(messages, "TEST_CONTRACT")
            return messages
        first, second = wire(before), wire(after)
        self.assertEqual(first[:2], second[:2])
        self.assertNotIn("BEGIN_DAFEIYU_SELF_MOOD", second[0]["content"])
        self.assertIn("50", first[-1]["content"])
        self.assertIn("56", second[-1]["content"])
        self.assertNotIn("小花", str(second))
        await self.finish(other, feeling("chat", "想聊点什么", -3, "惦记"))
        self.assertEqual(self.score(event), 53)

    async def test_silent_new_event_and_repeated_idle(self):
        note = {"id": "a"*64, "text": "朋友发来一张手绘明信片", "received": time.time()}
        for expected in [55, 55]:
            event, _ = await self.begin("世界的后台提示", auto=True, events=[note])
            await self.finish(event, feeling("event1", "手绘明信片", 5, "惊喜"), quiet=True)
            self.assertEqual(self.score(event), expected)
        idle, _ = await self.begin("世界的后台提示", auto=True)
        self.assertEqual(idle.get_extra(self_mood.TURN)["sources"], {})
        await self.finish(idle, feeling("chat", "后台提示"), quiet=True)
        self.assertEqual(self.score(idle), 55)

    async def test_native_world_receipts(self):
        event, _ = await self.begin("轮询", auto=True)
        async def observe(name, arguments, text, *, server="fish-world", failed=False):
            await followthrough.tool_result(self.plugin, event, NS(name=name, mcp_server_name=server),
                                            arguments, result(text, failed))
        await observe("world_now", {}, "有人准备看信")
        await observe("world_go", {"place":"pier"}, "可选步行")
        await observe("world_do", {"id":"read"}, "工具未能工作", failed=True)
        await observe("world_say", {"text":"你好"}, "已发送", server="another-world")
        self.assertEqual(event.get_extra(self_mood.TURN)["sources"], {})
        await observe("world_say", {"text":"问候朋友"}, "留言已送达")
        await self.finish(event, feeling("world_say", "留言已送达", 2, "期待"), quiet=True)
        self.assertEqual(self.score(event), 52)

    async def test_no_change_from_ordinary_or_invalid_metadata(self):
        for item in [None, [], {"source": []}, feeling("chat","没说过的话"), feeling("chat","早上好",11),
                     feeling("chat","早上好",True), feeling("chat","早上好",3,"未知名字")]:
            event, _ = await self.begin("早上好")
            await self.finish(event, item)
            self.assertEqual(self.score(event), 50)
        event, _ = await self.begin("把这句忘了", dafeiyu_semantic_forgetting=True)
        await self.finish(event, feeling("chat","把这句忘了"))
        self.assertIsNone(self.fish.store.data["self_mood"])
        technical, _ = await self.begin("测试", auto=True, fishworld_manual_test=True)
        self.assertIsNone(technical.get_extra(self_mood.TURN))

    async def test_scope_and_read_only_command(self):
        owner, _ = await self.begin("/心情")
        async def status(event):
            return [msg async for msg in FishWorldPlugin.mood_command(self.fish, event)]
        before = self.fish.path.read_bytes()
        self.assertEqual(await status(owner), ["听澜的心情 50/100 · 平静"])
        self.assertTrue(owner.is_stopped())
        self.assertEqual(self.fish.path.read_bytes(), before)
        group, _ = await self.begin("很开心见到你", session="33", group=True, dafeiyu_group_contract=True)
        self.assertIsNotNone(group.get_extra(self_mood.TURN))
        self.assertEqual(await status(group), [])
        denied, _ = await self.begin("旁人的群聊", session="33", group=True)
        self.assertIsNone(denied.get_extra(self_mood.TURN))
        self.assertIsNone(self.fish.mood_scope(denied))
        group.get_self_id = lambda: "another-bot"
        self.assertIsNone(self.fish.mood_scope(group))
        stale, _ = await self.begin("约好的小花开了")
        self.fish.store.use_key("another-fixture-key", time.time())
        await self.finish(stale, feeling("chat","小花开了"))
        self.assertIsNone(self.fish.store.data["self_mood"])

    async def test_persistence_failure_and_concurrent_turns(self):
        event, _ = await self.begin("约好的小花开了")
        with patch.object(self.fish.store, "save", side_effect=OSError("fixture")):
            await self.finish(event, feeling("chat","小花开了"))
        self.assertEqual(self.score(event), 50)
        self.assertFalse(self.fish.storage_error)
        first, _ = await self.begin("那条新步道开通了")
        second, _ = await self.begin("我的计划完成了")
        await asyncio.gather(self.finish(first, feeling("chat","步道开通了",3,"期待")),
                             self.finish(second, feeling("chat","计划完成了",4,"开心")))
        self.assertEqual(self.score(event), 57)
        raw = json.loads(self.fish.path.read_text(encoding="utf-8"))
        self.assertEqual(set(raw["self_mood"]), {"score", "tone", "at", "seen", "floor"})
        self.assertTrue(raw["self_mood_details"]["entries"])
        old_writer = dict(raw, self_mood=dict(raw["self_mood"], score=61, tone="放松", at=time.time()))
        self.fish.path.write_text(json.dumps(old_writer), encoding="utf-8")
        self.fish.store = StateStore(self.fish.path, time.time())
        self.assertEqual(mood_view(self.fish.store.data["self_mood"], time.time()), {"score": 61, "tone": "放松"})
        for legacy in [dict(raw, self_mood={"score":"坏值"}), {k:v for k,v in raw.items() if k!="self_mood"}]:
            self.fish.path.write_text(json.dumps(legacy), encoding="utf-8")
            self.fish.store = StateStore(self.fish.path, time.time())
            self.assertEqual(self.score(event), 50)
            self.assertEqual(self.fish.store.data["binding"], raw["binding"])


if __name__ == "__main__":
    unittest.main(argv=[__file__])
