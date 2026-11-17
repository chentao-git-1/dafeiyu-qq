"""Fish World format recovery using the production parser and captured model calls."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'framework')]
spec = importlib.util.spec_from_file_location(
    'fish_generation_workflow',
    ROOT / 'plugins/dafeiyu_dialogue/generation_workflow.py',
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Event:
    def __init__(self, rows):
        self.rows = rows

    def get_extra(self, name, default=None):
        return self.rows if name == 'dafeiyu_dialogue_tool_evidence' else default


async def main():
    valid = json.dumps({
        'focus': ['self'],
        'claims': [],
        'learning': {
            'privacy': {
                'action': 'none', 'evidence': '', 'ids': [],
                'subject': 'speaker', 'modality': 'direct',
            },
            'items': [],
        },
        'expression': 'chat',
        'reply': '我在松园小区楼下。',
    })
    workflow = module.GenerationWorkflow()
    workflow.provider_call = AsyncMock(return_value=valid)
    workflow.natural = lambda: SimpleNamespace(
        input_snapshot=lambda event: {'text': '你现在在哪？'},
    )
    row = {'tool': 'world_now', 'complete': True, 'result': '你在：松园小区楼下'}
    event = Event([row])

    parsed = await workflow.parse_generation_response(event, valid)
    assert parsed.metadata_valid
    workflow.provider_call.assert_not_awaited()

    missing_learning = json.dumps({
        'reply': '我在楼下。', 'focus': ['self'], 'claims': [], 'expression': 'chat',
    })
    for broken in ('我在松园小区楼下。', missing_learning):
        workflow.provider_call.reset_mock()
        parsed = await workflow.parse_generation_response(event, broken)
        assert not parsed.metadata_valid
        assert parsed.reply == '我在松园小区楼下。'
        assert workflow.provider_call.await_count == 1
        packet = json.loads(workflow.provider_call.call_args.args[1])
        assert packet['world_results'] == [row]
        assert workflow.provider_call.call_args.args[3] == 'generation_format'

    for rows in ([], [dict(row, complete=False)], [dict(row, tool='unrelated_tool')]):
        workflow.provider_call.reset_mock()
        try:
            await workflow.parse_generation_response(Event(rows), 'invalid')
        except ValueError:
            pass
        else:
            raise AssertionError('Unrelated or incomplete results were recovered')
        workflow.provider_call.assert_not_awaited()

    workflow.provider_call = AsyncMock(return_value='still invalid')
    try:
        await workflow.parse_generation_response(event, 'invalid')
    except ValueError:
        pass
    else:
        raise AssertionError('Invalid recovery was accepted')
    assert workflow.provider_call.await_count == 1
    print('FORMAT_RECOVERY_TESTS_PASSED 7')


if __name__ == '__main__':
    asyncio.run(main())
