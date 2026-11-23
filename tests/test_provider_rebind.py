"""Reload boundaries for an admitted chat, in-flight companion work and unowned calls."""
from collections import Counter
import functools
import importlib.util
from types import MethodType, SimpleNamespace as NS
import unittest

from test_fishworld_followthrough import Dialogue
from data.plugins.dafeiyu_dialogue import provider_contract as contract


class Adapter:
    def __init__(self):
        self.calls = 0

    def _finally_convert_payload(self, payload):
        payload["converted"] = True

    def _apply_provider_specific_request_overrides(self, payload, extra):
        pass

    async def text_chat(self, **kwargs):
        self.calls += 1
        payload = {"messages": list(kwargs.get("contexts", []))}
        self._finally_convert_payload(payload)
        self._apply_provider_specific_request_overrides(payload, {})
        return NS(payload=payload, tools_call_name=[], usage=None)


def primary_contexts():
    nonce = contract.arm_contract(True)
    content = "\n" + contract.MARKER + "\ncontract\n<!-- END_DAFEIYU_GENERATION -->\n<!-- DAFEIYU_CAP:" + nonce + " -->"
    return contract.NativeContexts([{"role":"system", "content":content}, {"role":"user","content":"hello"}], nonce)


class ProviderRebind(unittest.IsolatedAsyncioTestCase):
    async def test_authoritative_registration_required_for_takeover(self):
        # Reloaded plugin modules have distinct classes and ContextVars but the same qualified name.
        spec = importlib.util.spec_from_file_location(contract.__name__, contract.__file__)
        older = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(older)
        current = [False]
        old = older.ProviderContracts(is_current=lambda: not current[0])
        live = contract.ProviderContracts(is_current=lambda: current[0])
        competing = contract.ProviderContracts(is_current=lambda: True)
        self.addCleanup(old.close)
        self.addCleanup(live.close)
        self.addCleanup(competing.close)
        provider = Adapter()
        self.assertTrue(old.install(provider))
        self.assertFalse(live.install(provider))
        self.assertFalse(competing.install(provider))
        current[0] = True
        self.assertTrue(live.install(provider))
        self.assertFalse(old.install(provider))
        self.assertFalse(old.install(Adapter()))
        old.bind_request(NS(), "dead")
        self.assertIsNone(old.runner_adapter)
        result = await provider.text_chat(contexts=primary_contexts())
        self.assertEqual(result.payload["response_format"], {"type":"json_object"})
        old.close()
        self.assertIs(provider._dafeiyu_generation_json_owner, live)
        foreign = Adapter()
        other_plugin = object()
        foreign._dafeiyu_generation_json_owner = other_plugin
        self.assertFalse(live.install(foreign))
        self.assertIs(foreign._dafeiyu_generation_json_owner, other_plugin)

    async def test_legacy_owner_that_was_reinstalled_after_shutdown(self):
        # Previous versions could call install() after close() and their factory
        # ignored a False return. Simulate exactly their recorded method layout.
        def old_close(owner):
            for adapter, originals, wrappers in owner.installed:
                for name, wrapper in wrappers.items():
                    if getattr(adapter, name) is wrapper:
                        setattr(adapter, name, originals[name])
                if getattr(adapter, "_dafeiyu_generation_json_owner", None) is owner:
                    del adapter._dafeiyu_generation_json_owner
            owner.installed.clear()
        previous_cls = type("ProviderContracts", (), {"__module__": contract.__name__, "close": old_close})
        previous = previous_cls()
        previous.installed = []
        provider = Adapter()
        original = provider.text_chat
        async def old_chat(**kwargs):
            return await original(**kwargs)
        provider.text_chat = old_chat
        provider._dafeiyu_generation_json_owner = previous
        previous.installed.append((provider, {"text_chat":original}, {"text_chat":old_chat}))
        live = contract.ProviderContracts(is_current=lambda: True)
        self.addCleanup(live.close)
        self.assertTrue(live.install(provider))
        with self.assertRaises(RuntimeError):
            previous.install(provider)
        self.assertTrue((await provider.text_chat(contexts=primary_contexts())).payload["converted"])
        self.assertIs(provider._dafeiyu_generation_json_owner, live)

    async def test_outer_hooks_survive_and_closed_inner_hooks_are_transparent(self):
        provider = Adapter()
        old = contract.ProviderContracts()
        old.install(provider)
        previous_chat = provider.text_chat
        previous_convert = provider._finally_convert_payload
        calls = []
        @functools.wraps(previous_chat)
        async def outer_chat(**kwargs):
            calls.append("chat")
            return await previous_chat(**kwargs)
        @functools.wraps(previous_convert)
        def outer_convert(payload):
            calls.append("convert")
            previous_convert(payload)
        provider.text_chat = outer_chat
        provider._finally_convert_payload = outer_convert
        old.close()
        live = contract.ProviderContracts()
        self.addCleanup(live.close)
        self.assertTrue(live.install(provider))
        owned = primary_contexts()
        reply = await provider.text_chat(contexts=owned)
        self.assertIn(contract.MARKER, reply.payload["messages"][0]["content"])
        self.assertIn("response_format", reply.payload)
        plain = await provider.text_chat(contexts=list(owned))
        self.assertNotIn("response_format", plain.payload)
        self.assertNotIn(contract.MARKER, plain.payload["messages"][0]["content"])
        self.assertEqual(calls, ["chat","convert","chat","convert"])
        live.close()
        self.assertIs(provider.text_chat, outer_chat)
        self.assertIs(provider._finally_convert_payload, outer_convert)

    async def test_delayed_auxiliary_request_uses_live_meter_and_scope(self):
        provider = Adapter()
        def controller():
            instance = NS(provider_contracts=contract.ProviderContracts(),review_stats=Counter())
            instance.metered_provider = MethodType(Dialogue.metered_provider, instance)
            return instance
        old, live = controller(), controller()
        old.current_dialogue = lambda: live
        self.addCleanup(old.provider_contracts.close)
        self.addCleanup(live.provider_contracts.close)
        late = old.metered_provider(provider, "initiative")
        old.provider_contracts.close()
        reply = await late.text_chat(system_prompt="Plan", contexts=[])
        self.assertIn("response_format", reply.payload)
        self.assertEqual(old.review_stats["initiative_calls"], 0)
        self.assertEqual(live.review_stats["initiative_calls"], 1)
        self.assertIs(old.metered_provider(provider,"agenda").owner, live)
        old.current_dialogue = lambda: old
        with self.assertRaises(RuntimeError):
            await late.text_chat(system_prompt="Plan")
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main(argv=[__file__])
