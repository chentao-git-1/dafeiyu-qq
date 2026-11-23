"""Content-free telemetry must distinguish a prepared turn from a confirmed primary request."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_cognition  # Load the same framework/plugin namespace as the native runner.
from dafeiyu.dialogue.generation import request_contract
from data.plugins.dafeiyu_dialogue import turn_audit as audit
from data.plugins.dafeiyu_dialogue.provider_contract import constrain_payload
from data.plugins.dafeiyu_dialogue.world_activity import add_packet


class Event:
    def __init__(self):
        self.message_obj = SimpleNamespace(message_id="fishworld_" + "b" * 32)
        self.data = {"fishworld_wake_id": "a" * 32}

    def get_extra(self, name, default=None):
        return self.data.get(name, default)

    def set_extra(self, name, value):
        self.data[name] = value


class WorldTurnAudit(unittest.TestCase):
    def setUp(self):
        self.event = Event()
        self.nonce = "f" * 32
        self.log = []
        capture = patch.object(audit.logger, "info", side_effect=lambda fmt, *args: self.log.append(fmt % args))
        capture.start()
        self.addCleanup(capture.stop)

    def project(self, *, rich=False, baseline=False):
        audit.arm(self.event, self.nonce)
        request = SimpleNamespace(system_prompt=request_contract("", {}, True, nonce=self.nonce))
        mood = {"听澜自己的心情": 50 if baseline else 0, "感受": "平静" if baseline else [{"昵称": "私下的名字"}]}
        world = {"evidence_id": "tool:world_now", "result": "虚构回执<私密地名>"}
        for key, obj in (("SELF_MOOD", mood), ("WORLD_PREFETCH", world)):
            audit.expect(self.event, key, obj)
            add_packet(request, key, obj)
        user = "非公开发言"
        payload = {"messages": [{"role": "system", "content": request.system_prompt},
                                {"role": "user", "content": [{"type":"text", "text":user}] if rich else user}]}
        constrain_payload(payload, allow_contract=True)
        return payload

    def test_first_confirmed_takes_effective_metadata_only(self):
        payload = self.project(rich=True)
        with audit.provider_attempt(self.nonce):
            with audit.provider_attempt(None):
                audit.observe_payload({}, {})
            audit.observe_payload(payload, {})
        with audit.provider_attempt(self.nonce):
            audit.observe_payload({}, {})
        self.event.set_extra("dafeiyu_generation_valid", True)
        self.event.set_extra("dafeiyu_world_quiet", True)
        audit.finish(self.event)
        audit.finish(self.event)
        self.assertEqual(len(self.log), 3)
        self.assertIn("phase=wire contract=1 json=1 mood=1 world=1", self.log[1])
        self.assertIn("mood_mode=layered", self.log[1])
        self.assertIn("phase=end payloads=2 first_confirmed=1 format_calls=0 shape=none valid=1 quiet=1", self.log[2])
        for private in ("私下的名字", "私密地名", "非公开发言", self.nonce, "a"*32):
            self.assertNotIn(private, str(self.log))

    def test_changed_sections_and_provider_override(self):
        payload = self.project(baseline=True)
        # A valid packet in previous history must not authenticate the current user message.
        good = payload["messages"][-1]
        payload["messages"].append(dict(good, content=good["content"].replace("虚构回执", "不同回执")))
        with audit.provider_attempt(self.nonce):
            audit.observe_payload(payload, {"response_format":{"type":"text"}})
        self.assertIn("json=0 mood=1 world=0 prepared_mood=1 prepared_world=1 mood_mode=baseline", self.log[1])
        self.event.set_extra("dafeiyu_generation_valid", True)
        audit.finish(self.event)

    def test_no_observation_failed_call_and_internal_retry_are_not_confirmed(self):
        payload = self.project()
        with audit.provider_attempt(self.nonce):
            pass
        with self.assertRaises(RuntimeError), audit.provider_attempt(self.nonce):
            audit.observe_payload(payload, {})
            raise RuntimeError("fixture")
        with audit.provider_attempt(self.nonce):
            audit.observe_payload(payload, {})
        audit.failed_shape(self.event, "虚构文本", json.JSONDecodeError("example", "", 0))
        audit.format_recovery(self.event)
        audit.finish(self.event)
        self.assertIn("first_confirmed=0 format_calls=1 shape=plain_text", self.log[-1])
        fresh = Event()
        audit.arm(fresh, "d"*32)
        with audit.provider_attempt("d"*32):
            audit.observe_payload(payload, {})
            audit.observe_payload(payload, {})
        audit.finish(fresh)
        self.assertIn("payloads=2 first_confirmed=0", self.log[-1])

    def test_unsent_and_non_autonomous_turns_are_distinct(self):
        audit.arm(self.event, None)
        audit.finish(self.event)
        self.assertIn("phase=start bound=0", self.log[0])
        self.assertIn("phase=end payloads=0 first_confirmed=0", self.log[1])
        for flag in ("cron_job", "cron_payload", "fishworld_manual_test", "fishworld_manual_wake"):
            event = Event(); event.set_extra(flag, True)
            audit.arm(event, self.nonce)
            audit.finish(event)
        event = Event()
        event.message_obj.message_id = "normal-qq"
        audit.arm(event, self.nonce)
        self.assertEqual(len(self.log), 2)


if __name__ == "__main__":
    unittest.main(argv=[__file__])
