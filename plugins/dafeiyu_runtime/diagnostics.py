"""Best-effort numeric telemetry. Never retain prompts, responses or credentials."""
import asyncio
import contextlib
import contextvars
import hashlib
import json
import math
import os
import re
import time
import uuid
from collections import deque
from pathlib import Path

KEY = 'dafeiyu_turn_diagnostics'
CURRENT = contextvars.ContextVar('dafeiyu_diagnostic_turn', default=None)
PHASES = {'preparation', 'history', 'memory', 'schedule', 'agent', 'tools', 'vision', 'tts', 'send'}
METRICS = {'recall_candidates', 'recall_selected', 'recall_time_filter', 'recall_synonym_matches', 'schedule_selected', 'schedule_unavailable'}
CALLS = {'agent_runs', 'reported_agent_runs', 'vision', 'tts', 'tools', 'tts_characters'}
OUTCOMES = {'reply', 'partial', 'timeout', 'delivery_failed', 'notice', 'no_reply', 'cancelled'}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value < 10**12


def usage_values(usage):
    data = usage if isinstance(usage, dict) else getattr(usage, '__dict__', {})
    keys = ('input_other', 'input_cached', 'output')
    if not isinstance(data, dict) or not all(number(data.get(k)) for k in keys):
        return None
    values = {k: int(data[k]) for k in keys}
    # AgentStats starts at all zero even when a provider never reports usage.
    return values if any(values.values()) else None


def active(event=None):
    try:
        turn = event.get_extra(KEY, None) if event is not None else CURRENT.get()
        return turn if isinstance(turn, Turn) and not turn.closed else None
    except Exception:
        return None


@contextlib.contextmanager
def phase(name, event=None, characters=None):
    turn = active(event)
    started = time.monotonic()
    if turn and name in ('vision', 'tts'):
        turn.calls[name] = turn.calls.get(name, 0) + 1
        if name == 'tts' and number(characters):
            turn.calls['tts_characters'] = turn.calls.get('tts_characters', 0) + int(characters)
    try:
        yield
    except BaseException:
        if turn and not turn.closed and name in PHASES:
            turn.failures[name] = turn.failures.get(name, 0) + 1
        raise
    finally:
        if turn and not turn.closed and name in PHASES:
            turn.phases[name] = turn.phases.get(name, 0) + max(0, time.monotonic() - started) * 1000


def report_usage(usage, source='vision', event=None):
    try:
        turn = active(event)
        values = usage_values(usage)
        if turn and source in ('main', 'vision') and values:
            prior = turn.usage.get(source) or dict.fromkeys(values, 0)
            turn.usage[source] = {k: prior[k] + v for k, v in values.items()}
    except Exception:
        pass


def metric(name, value, event=None):
    turn = active(event)
    if turn and name in METRICS and number(value):
        turn.metrics[name] = value


class Turn:
    def __init__(self, event):
        self.started = time.monotonic()
        self.at = time.time()
        self.closed = False
        self.trace_id = uuid.uuid4().hex
        self.scope = 'group' if event.get_group_id() else 'private'
        self.session = hashlib.sha256(str(event.unified_msg_origin).encode()).hexdigest()[:16]
        mid = str(getattr(getattr(event, 'message_obj', None), 'message_id', ''))
        self.message_id = mid if re.fullmatch(r'-?\d{1,24}', mid) else ''
        self.phases, self.calls, self.failures, self.metrics = {}, {}, {}, {}
        self.usage = {'main': None, 'vision': None}
        self.agent_started = None
        self.tool_started = []
        self.restore_trace = lambda: None

    def attach_trace(self, event):
        trace = getattr(event, 'trace', None)
        original = getattr(trace, 'record', None)
        if not callable(original):
            return

        def record(action, **fields):
            # Preserve the core recorder's inputs, output and exception behavior.
            try:
                if not self.closed:
                    self.observe(action, fields)
            except Exception:
                pass
            return original(action, **fields)

        try:
            trace.record = record
            def restore():
                if getattr(trace, 'record', None) is record:
                    trace.record = original
            self.restore_trace = restore
        except Exception:
            pass

    def observe(self, action, fields):
        now = time.monotonic()
        if action == 'astr_agent_prepare':
            self.phases.setdefault('preparation', max(0, now - self.started) * 1000)
            self.agent_started = now
            self.calls['agent_runs'] = self.calls.get('agent_runs', 0) + 1
        elif action == 'astr_agent_complete':
            if self.agent_started is not None:
                self.phases['agent'] = self.phases.get('agent', 0) + max(0, now - self.agent_started) * 1000
                self.agent_started = None
            stats = fields.get('stats')
            values = usage_values(stats.get('token_usage')) if isinstance(stats, dict) else None
            if values:
                prior = self.usage['main'] or dict.fromkeys(values, 0)
                self.usage['main'] = {k: prior[k] + v for k, v in values.items()}
                self.calls['reported_agent_runs'] = self.calls.get('reported_agent_runs', 0) + 1

    def tool_begin(self):
        self.tool_started.append(time.monotonic())
        self.calls['tools'] = self.calls.get('tools', 0) + 1

    def tool_end(self):
        if self.tool_started:
            elapsed = time.monotonic() - self.tool_started.pop(0)
            self.phases['tools'] = self.phases.get('tools', 0) + max(0, elapsed) * 1000

    def finish(self, event, cancelled=False):
        self.restore_trace()
        state = event.get_extra('dafeiyu_reply_delivery', {}) or {}
        receipts = event.get_extra('dafeiyu_confirmed_delivery', []) or []
        now = time.monotonic()
        if self.agent_started is not None:
            self.phases['agent'] = self.phases.get('agent', 0) + max(0, now - self.agent_started) * 1000
        self.closed = True
        if not (state.get('reply_expected') or receipts or self.calls):
            return None
        outcome = ('cancelled' if cancelled else 'timeout' if state.get('expired') else
                   'partial' if state.get('delivery_failed') and state.get('final_sends') else
                   'delivery_failed' if state.get('delivery_failed') else
                   'reply' if any(r.get('kind') == 'reply' for r in receipts) else 'notice' if receipts else 'no_reply')
        return {'trace_id': self.trace_id, 'at': self.at, 'scope': self.scope, 'session_hash': self.session,
                'message_id': self.message_id, 'outcome': outcome, 'total_ms': round(max(0, now - self.started) * 1000, 2),
                'phases_ms': {k: round(v, 2) for k, v in self.phases.items()},
                'reported_tokens': self.usage, 'calls': self.calls.copy(), 'failures': self.failures.copy(), 'metrics': self.metrics.copy()}


def clean_row(row):
    """Accept only our explicit numeric schema, including on disk reload."""
    if not isinstance(row, dict) or not re.fullmatch(r'[a-f0-9]{32}', str(row.get('trace_id', ''))):
        return None
    if not number(row.get('at')) or not number(row.get('total_ms')) or row.get('outcome') not in OUTCOMES:
        return None
    if row.get('scope') not in ('private', 'group') or not re.fullmatch(r'[a-f0-9]{16}', str(row.get('session_hash', ''))):
        return None
    result = {k: row[k] for k in ('trace_id', 'at', 'scope', 'session_hash', 'outcome', 'total_ms')}
    mid = str(row.get('message_id', ''))
    result['message_id'] = mid if re.fullmatch(r'-?\d{1,24}', mid) else ''
    for key, allowed in (('phases_ms', PHASES), ('failures', PHASES), ('metrics', METRICS), ('calls', CALLS)):
        source = row.get(key, {})
        result[key] = {k: v for k, v in source.items() if k in allowed and number(v)} if isinstance(source, dict) else {}
    usage = row.get('reported_tokens', {})
    result['reported_tokens'] = {k: usage_values(usage.get(k)) if isinstance(usage, dict) else None for k in ('main', 'vision')}
    return result


class Journal:
    def __init__(self, path):
        self.path = Path(path)
        self.rows = deque(maxlen=200)
        self.io_failures = 0
        self.lock = asyncio.Lock()
        try:
            if self.path.exists() and self.path.stat().st_size <= 512 * 1024:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                for raw in data.get('turns', [])[-200:]:
                    row = clean_row(raw)
                    if row and time.time() - 7 * 86400 <= row['at'] <= time.time() + 60:
                        self.rows.append(row)
        except Exception:
            self.io_failures += 1

    def append(self, row):
        row = clean_row(row)
        if row:
            self.rows.append(row)

    def status(self):
        cutoff = time.time() - 7 * 86400
        self.rows = deque((r for r in self.rows if r['at'] >= cutoff), maxlen=200)
        return {'enabled': True, 'retained_turns': len(self.rows), 'retention_days': 7, 'max_turns': 200,
                'io_failures': self.io_failures, 'turns': list(self.rows)[-20:][::-1],
                'coverage': 'main=core cumulative reported usage; vision=separate caption calls; TTS=characters, not tokens; no price estimate',
                'timing': 'milliseconds; preparation includes history/memory/schedule/vision; agent includes tools; phases overlap',
                'persist_interval_seconds': 120, 'contains_chat_content': False}

    async def flush(self):
        async with self.lock:
            try:
                self.status()  # Physically expire old rows even if nobody opens diagnostics.
                payload = json.dumps({'turns': list(self.rows)}, ensure_ascii=True, allow_nan=False)
                await asyncio.to_thread(self._write, payload)
            except Exception:
                self.io_failures += 1

    def _write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as file:
                temporary.chmod(0o600)
                file.write(payload)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
