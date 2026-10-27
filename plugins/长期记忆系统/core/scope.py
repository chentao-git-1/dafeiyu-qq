"""Per-event memory visibility; task-local values also follow background extraction tasks."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
import inspect
import re

@dataclass(frozen=True)
class MemoryScope:
    user_id: str
    kind: str
    key: str
    platform: str
    allow_public: bool = False
    sender_name: str = ''

current_scope = ContextVar('dafeiyu_memory_scope', default=None)
requested_visibility = ContextVar('dafeiyu_memory_visibility', default='auto')

FAMILY_NAMES={'7000000001':'爸爸','7000000011':'猛叔叔','7000000012':'田阿姨'}
MEMORY_ADMIN='7000000001'

def clean_name(value):
    return re.sub(r'[\x00-\x1f\x7f<>\[\]&]', '', str(value or '')).strip()[:40]

def owner_name(user_id,nickname=''):
    if str(user_id) in FAMILY_NAMES:return FAMILY_NAMES[str(user_id)]
    display=clean_name(nickname)
    if display in FAMILY_NAMES.values():return 'QQ'+str(user_id)+'（昵称'+display+'）'
    return display or 'QQ'+str(user_id)

def origin_kind(session_id,fallback=''):
    if ':GroupMessage:' in str(session_id):return 'group'
    if ':FriendMessage:' in str(session_id):return 'private'
    return fallback if fallback in ('group','private') else 'unknown'

def scope_from_values(user_id, session_id):
    user_id, session_id = str(user_id), str(session_id or '')
    platform = session_id.split(':',1)[0] if ':' in session_id else 'legacy'
    if ':GroupMessage:' in session_id:
        return MemoryScope(user_id,'group',session_id,platform)
    return MemoryScope(user_id,'private',platform+':private:'+user_id,platform)

def scope_for_event(event):
    scope=scope_from_values(event.get_sender_id(),event.unified_msg_origin)
    public=bool(re.match(r'^\s*(?:公开记住|公开记忆|公开更正)[：:\s]',event.message_str or ''))
    get_name=getattr(event,'get_sender_name',None)
    nickname=get_name() if callable(get_name) else ''
    return MemoryScope(scope.user_id,scope.kind,scope.key,scope.platform,public,owner_name(scope.user_id,nickname))

@contextmanager
def using_scope(scope):
    token=current_scope.set(scope)
    try:yield scope
    finally:current_scope.reset(token)

@contextmanager
def write_visibility(value):
    value=str(value or 'auto').lower()
    if value not in ('auto','shared','public'):
        raise ValueError('当前采用共享记忆；需要保密的内容请仅留在当前聊天，不写入长期记忆')
    token=requested_visibility.set('shared')
    try:yield
    finally:requested_visibility.reset(token)

def scoped_event(function):
    if inspect.isasyncgenfunction(function):
        @wraps(function)
        async def generator(self,event,*args,**kwargs):
            with using_scope(scope_for_event(event)):
                async for result in function(self,event,*args,**kwargs):yield result
        return generator
    @wraps(function)
    async def coroutine(self,event,*args,**kwargs):
        with using_scope(scope_for_event(event)):
            return await function(self,event,*args,**kwargs)
    return coroutine
