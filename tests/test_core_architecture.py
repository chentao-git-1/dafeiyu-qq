"""Exercise standalone imports, old imports, installed bundles and persisted state."""
import ast
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
if len(sys.argv) > 1 and Path(sys.argv[1]).is_dir():
    sys.argv.pop(1)
spec = importlib.util.spec_from_file_location('dafeiyu_installer', ROOT / 'scripts/install_plugins.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


def isolated(code, *paths):
    bootstrap = 'import sys; sys.path[:0] = ' + repr([str(path) for path in paths]) + '\n'
    return subprocess.run([sys.executable, '-I', '-c', bootstrap + code],
                          capture_output=True, text=True, encoding='utf-8', errors='replace')


class Architecture(unittest.TestCase):
    def test_every_core_module_imports_without_astrbot_or_plugins(self):
        result = isolated('''
import importlib, importlib.abc, pkgutil, dafeiyu
class NoRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'astrbot' or fullname.startswith(('astrbot.', 'data.plugins', 'plugins.')):
            raise AssertionError('Core depends on runtime: ' + fullname)
sys.meta_path.insert(0, NoRuntime())
names = [item.name for item in pkgutil.walk_packages(dafeiyu.__path__, 'dafeiyu.')]
for name in names:
    importlib.import_module(name)
from dafeiyu.memory.self_profile import self_profile
from dafeiyu.life.topic_context import world_profile
assert self_profile() and world_profile()
assert not any(name.startswith('astrbot') for name in sys.modules)
print(len(names))
''', ROOT / 'src')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreater(int(result.stdout.strip()), 50)

    def test_no_hidden_runtime_imports_in_core_functions(self):
        violations = []
        for path in (ROOT / 'src/dafeiyu').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.ImportFrom):
                    modules = [node.module or '']
                elif isinstance(node, ast.Import):
                    modules = [item.name for item in node.names]
                else:
                    continue
                for module in modules:
                    if module == 'astrbot' or module.startswith(('astrbot.', 'data.plugins', 'plugins.')):
                        violations.append(f'{path.name}:{node.lineno}: {module}')
        self.assertEqual(violations, [])

    def test_old_and_new_names_share_one_module_and_mutable_state(self):
        import data.plugins
        data.plugins.__path__ = [str(ROOT / 'plugins'), *data.plugins.__path__]
        pairs = [('dafeiyu_dialogue.contracts', 'dafeiyu.dialogue.contracts'),
                 ('dafeiyu_runtime.diagnostics', 'dafeiyu.runtime.diagnostics'),
                 ('dafeiyu_life.planning', 'dafeiyu.life.planning')]
        for old, new in pairs:
            canonical = importlib.import_module(new)
            for legacy in (old, 'data.plugins.' + old):
                self.assertIs(importlib.import_module(legacy), canonical)
        from data.plugins.dafeiyu_dialogue.contracts import TurnContext as old_turn
        from dafeiyu.dialogue.contracts import TurnContext
        self.assertIs(old_turn, TurnContext)

    def test_fresh_install_renders_core_and_imports_without_source_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary) / 'runtime'
            target = runtime / 'data/plugins'
            installer.install(target, {'7000000001': '7000000002', '7000000002': '8000000002'})
            result = isolated('''
from dafeiyu.memory.state import OWNER
from dafeiyu.memory.self_profile import self_profile
from dafeiyu.life.topic_context import world_profile
from dafeiyu.dialogue.contracts import TurnContext
from data.plugins.dafeiyu_dialogue.contracts import TurnContext as legacy
assert OWNER == '7000000002', OWNER
assert legacy is TurnContext
assert self_profile() and world_profile()
''', runtime)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((target / 'dafeiyu').exists())
            self.assertFalse(list(runtime.rglob('*.db')))
            self.assertFalse((target / '__pycache__').exists())

    def test_existing_core_is_rejected_before_copying_plugins(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            core = runtime / 'dafeiyu'
            core.mkdir()
            sentinel = core / 'existing.txt'
            sentinel.write_text('preserve me', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                installer.install(runtime / 'data/plugins', {})
            self.assertEqual(sentinel.read_text(encoding='utf-8'), 'preserve me')
            self.assertFalse((runtime / 'data').exists())

    def test_existing_plugin_is_rejected_before_copying_core(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            existing = runtime / 'data/plugins/dafeiyu_dialogue'
            existing.mkdir(parents=True)
            with self.assertRaises(FileExistsError):
                installer.install(existing.parent, {})
            self.assertFalse((runtime / 'dafeiyu').exists())

    def test_invalid_destinations_and_identities_do_not_write(self):
        with self.assertRaises(ValueError):
            installer.install(ROOT / 'test-install/data/plugins', {})
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            target = runtime / 'data/plugins'
            with self.assertRaises(ValueError):
                installer.install(target, {'bad': '123'})
            with self.assertRaises(ValueError):
                installer.install(target, {}, target / 'dafeiyu')
            self.assertEqual(list(runtime.iterdir()), [])

    def test_life_store_reopens_without_repeating_an_unknown_action(self):
        from dafeiyu.life.store import LifeStore
        with tempfile.TemporaryDirectory() as temporary:
            store = LifeStore(temporary)
            run = store.schedule()
            self.assertTrue(store.claim(run['day'], 'drawing'))
            restored = LifeStore(temporary)
            self.assertEqual(restored.schedule()['status'], 'unknown')
            self.assertFalse(restored.claim(run['day'], 'drawing'))
            self.assertEqual(restored.count(), 0)


class StandaloneActivity(unittest.IsolatedAsyncioTestCase):
    async def test_activity_policy_uses_injected_provider_and_preserves_no_action(self):
        from dafeiyu.life.planning import SelfJournal, choose_activity
        decision = dict(kind='none', project_id='', expected_version=0, title='', question='',
                        source_ids=[], reason='没有新的方向，先停一停')
        provider = NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(decision))))
        with tempfile.TemporaryDirectory() as temporary:
            result = await choose_activity(provider, SelfJournal(temporary), [], ['reading', 'writing'])
        self.assertEqual(result, decision)
        provider.text_chat.assert_awaited_once()
        request = provider.text_chat.call_args.kwargs
        self.assertEqual(request['contexts'], [])
        self.assertIsNone(request['func_tool'])
        self.assertEqual(json.loads(request['prompt'])['max_activities_today'], 1)


if __name__ == '__main__':
    unittest.main()
