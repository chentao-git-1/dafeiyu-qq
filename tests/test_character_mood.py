"""Exercise the native QQ generation and FishWorld store with fictional, isolated turns."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_fishworld_followthrough import (
    Dialogue, Event, FishWorldPlugin, LLMResponse, NS, StateStore, app, empty_learning,
    followthrough, result, toolset,
)
from data.plugins.astrbot_plugin_fishworld.self_mood import HALF_LIFE, MAX_MARKS, REPLAY_WINDOW, mood_view, next_mood
from data.plugins.dafeiyu_dialogue import self_mood
from data.plugins.dafeiyu_dialogue.prompt_cache import project_turn_data
from data.plugins.dafeiyu_dialogue.world_activity import FLOW


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

    async def finish(self, event, proposal=None, *, quiet=False):
        reply = "" if quiet else "我听到了。"
        obj = {"reply": reply, "claims": [], "learning": empty_learning(), "focus": ["social"], "expression": "chat"}
        if quiet:
            obj["fishworld"] = {"share": "quiet"}
        if proposal is not None:
            obj["self_mood"] = proposal
        response = LLMResponse(role="assistant", completion_text=json.dumps(obj, ensure_ascii=False))
        await Dialogue.review_response(self.plugin, event, response)
        self.assertTrue(event.get_extra("dafeiyu_generation_valid"))
        self.assertEqual(response.completion_text, reply)
        self.assertEqual(event.sent, [])

    def score(self, event):
        return self.fish.read_self_mood(event, self.fish.mood_scope(event))["score"]

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
        for legacy in [dict(raw, self_mood={"score":"坏值"}), {k:v for k,v in raw.items() if k!="self_mood"}]:
            self.fish.path.write_text(json.dumps(legacy), encoding="utf-8")
            self.fish.store = StateStore(self.fish.path, time.time())
            self.assertEqual(self.score(event), 50)
            self.assertEqual(self.fish.store.data["binding"], raw["binding"])


if __name__ == "__main__":
    unittest.main(argv=[__file__])
