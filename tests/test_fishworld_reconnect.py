"""Application-level FishWorld MCP recovery checks with an offline transport."""
import asyncio
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

import anyio
import httpx
from mcp.types import CallToolResult, TextContent, Tool

import data.plugins

stage = Path(sys.argv.pop(1))
data.plugins.__path__ = [str(stage), *data.plugins.__path__]
from data.plugins.astrbot_plugin_fishworld import mcp_recovery
from data.plugins.astrbot_plugin_fishworld.core import StateStore
from data.plugins.astrbot_plugin_fishworld.main import FishWorldPlugin
from astrbot.core.agent.mcp_client import MCPClient, MCPTool
from astrbot.core.agent.run_context import ContextWrapper


class Reconnect(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = MCPClient()
        self.client._server_name = 'fish-world'
        self.client._mcp_server_config = {'active': True}
        self.client.tools = [Tool(name=name, inputSchema={'type': 'object'})
                             for name in ('world_do', 'world_now')]
        self.tools = {tool.name: MCPTool(tool, self.client, 'fish-world') for tool in self.client.tools}
        self.runtime = NS(client=self.client, shutdown_event=asyncio.Event(),
                          lifecycle_task=asyncio.get_running_loop().create_future())
        self.runtimes = {'fish-world': self.runtime}
        manager = NS(mcp_server_runtime_view=self.runtimes, get_func=self.tools.get)
        self.plugin = FishWorldPlugin(NS(get_llm_tool_manager=lambda: manager),
                                      {'enabled': True, 'watch_key': 'fwr_fixture'})
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.plugin.store = StateStore(Path(directory.name)/'state.json', time.time())
        self.plugin.store.data['binding'] = {'umo': 'qq:private:fixture', 'group_id': ''}
        self.plugin.next_fetch = self.plugin.next_delivery = float('inf')
        self.addAsyncCleanup(self.plugin.terminate)
        self.clock = 1000.0
        clock = patch.object(mcp_recovery, 'time', NS(monotonic=lambda: self.clock))
        clock.start()
        self.addCleanup(clock.stop)
        self.context = ContextWrapper(context=None, tool_call_timeout=3)

    async def poll(self):
        await self.plugin.check()
        task = self.plugin.mcp_recovery.task
        if task is not None:
            await task

    async def test_failed_write_is_never_replayed_and_new_read_works_after_recovery(self):
        # The old session may already have accepted this action before losing its response.
        old_call = AsyncMock(side_effect=anyio.ClosedResourceError())
        self.client.session = NS(call_tool=old_call)
        state = CallToolResult(content=[TextContent(type='text', text='fixture world state')])
        new_call = AsyncMock(return_value=state)
        healthy_session = NS(call_tool=new_call,
                             list_tools=AsyncMock(return_value=NS(tools=self.client.tools)))
        upstream_restored = False

        async def connect(config, name):
            self.assertEqual(name, 'fish-world')
            if not upstream_restored:
                request = httpx.Request('POST', 'https://fish.example.invalid')
                raise httpx.HTTPStatusError('502 Bad Gateway', request=request,
                                            response=httpx.Response(502, request=request))
            self.client.session = healthy_session
            self.client._connection_task = asyncio.get_running_loop().create_future()
            self.addCleanup(self.client._connection_task.cancel)

        handshake = AsyncMock(side_effect=connect)
        with patch.object(self.client, 'connect_to_server', handshake):
            with self.assertRaises(httpx.HTTPStatusError):
                await self.tools['world_do'].call(self.context, action='buy fixture')
            self.assertIsNone(self.client.session)
            self.assertEqual(handshake.await_count, 1)

            await self.poll()
            self.assertEqual(handshake.await_count, 2)
            self.clock += 29
            await self.poll()
            self.assertEqual(handshake.await_count, 2)
            self.clock += 1
            await self.poll()
            self.assertEqual(handshake.await_count, 3)
            upstream_restored = True
            self.clock += 59
            await self.poll()
            self.assertEqual(handshake.await_count, 3)
            self.clock += 1
            await self.poll()
            self.assertEqual(handshake.await_count, 4)
            new_call.assert_not_awaited()
            self.assertEqual(await self.tools['world_now'].call(self.context), state)
            for _ in range(3):
                self.clock += 300
                await self.poll()
            self.assertEqual(handshake.await_count, 4)

        self.assertEqual([call.kwargs['name'] for call in old_call.await_args_list], ['world_do'])
        self.assertEqual([call.kwargs['name'] for call in new_call.await_args_list], ['world_now'])

    async def test_overlapping_ticks_share_a_single_handshake(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def reconnect():
            entered.set()
            await release.wait()
            self.client.session = object()
            self.client._connection_task = asyncio.get_running_loop().create_future()
            self.addCleanup(self.client._connection_task.cancel)
        with patch.object(self.client, '_reconnect', new=AsyncMock(side_effect=reconnect)) as handshake:
            await self.plugin.check()
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.gather(*(self.plugin.check() for _ in range(6)))
            self.assertEqual(handshake.await_count, 1)
            release.set()
            await self.poll()
            await self.poll()
            handshake.assert_awaited_once()

    async def test_discovery_failure_and_replacement_session_have_different_retry_behavior(self):
        partial = object()
        complete = object()
        owner = asyncio.get_running_loop().create_future()
        self.addCleanup(owner.cancel)

        async def fail_after_connect():
            self.client.session = partial
            self.client._connection_task = owner
            raise RuntimeError('fixture tool discovery failed')

        with patch.object(self.client, '_reconnect', new=AsyncMock(side_effect=fail_after_connect)) as handshake:
            await self.poll()
            self.assertIs(self.client.session, partial)
            self.clock += 30
            await self.poll()
            self.assertEqual(handshake.await_count, 2, 'an incomplete handshake still needs repair')

            # Native recovery or an administrator replaced the session on the same client.
            self.client.session = complete
            new_owner = asyncio.get_running_loop().create_future()
            self.client._connection_task = new_owner
            self.addCleanup(new_owner.cancel)
            self.clock += 1
            await self.poll()
            self.assertEqual(self.plugin.mcp_recovery.failures, 0)
            self.assertEqual(self.plugin.mcp_recovery.retry_at, 0)
            self.clock += 300
            await self.poll()
            self.assertEqual(handshake.await_count, 2, 'leave the healthy replacement untouched')
            self.assertIs(self.client.session, complete)

    async def test_healthy_disabled_stopping_and_unregistered_clients_are_untouched(self):
        other = MCPClient()
        other.tools = self.client.tools
        self.runtimes['other'] = NS(client=other, shutdown_event=asyncio.Event(),
                                    lifecycle_task=self.runtime.lifecycle_task)
        with patch.object(self.client, '_reconnect', new_callable=AsyncMock) as handshake, \
                patch.object(other, '_reconnect', new_callable=AsyncMock) as unrelated:
            self.client.session = object()
            owner = asyncio.get_running_loop().create_future()
            self.client._connection_task = owner
            await self.poll()
            self.client.session = None
            owner.cancel()
            self.client._connection_task = None
            self.plugin.store.data['paused'] = True
            await self.poll()
            self.plugin.store.data['paused'] = False
            self.plugin.config['enabled'] = False
            await self.poll()
            self.plugin.config['enabled'] = True
            self.client.active = False
            await self.poll()
            self.client.active = True
            self.runtime.shutdown_event.set()
            await self.poll()
            del self.runtimes['fish-world']
            await self.poll()
            handshake.assert_not_awaited()
            unrelated.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
