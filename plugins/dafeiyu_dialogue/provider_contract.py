"""Request-local JSON mode for the inspected AstrBot OpenAI-compatible adapter."""
import functools
import inspect
import json
import re
import copy
from contextvars import ContextVar
import uuid
import hashlib
import time

from .prompt_cache import project_turn_data
from .route_receipts import route_tools
from .wardrobe import outfit_tools
from . import turn_audit

MARKER='<!-- BEGIN_DAFEIYU_GENERATION -->'
STRUCTURED_MARKER='<!-- DAFEIYU_STRUCTURED_TASK -->'
CURRENT=ContextVar('dafeiyu_generation_capability',default=None)
STRUCTURED=ContextVar('dafeiyu_structured_task',default=False)
OUTPUT_BUDGET=ContextVar('dafeiyu_output_budget',default=None)
WIRE_SCOPE=ContextVar('dafeiyu_wire_scope',default=(False,False,()))


class WireMessages(list):
    _dafeiyu_protocol_projected=True


class NativeContexts(list):
    """Capability attached by the owned runner, never inferred from chat text."""
    def __init__(self,messages,nonce,first_route_choice=False,outfit_choice=None,voice_tones=()):
        super().__init__(messages);self.nonce=nonce;self.first_route_choice=first_route_choice;self.outfit_choice=outfit_choice
        self.voice_tones=voice_tones

    def __copy__(self):return list(self)

    def __deepcopy__(self,memo):return copy.deepcopy(list(self),memo)


def arm_contract(enabled):
    nonce=uuid.uuid4().hex if enabled else None
    CURRENT.set(nonce)
    return nonce


def contract_request(kwargs):
    contexts=kwargs.get('contexts')
    if isinstance(contexts,NativeContexts):
        nonce=contexts.nonce
        prompts=[m.get('content') if isinstance(m,dict) else getattr(m,'content',None)
            for m in contexts if (m.get('role') if isinstance(m,dict) else getattr(m,'role',None))=='system']
    else:
        nonce=CURRENT.get();prompts=[kwargs.get('system_prompt')]
    return bool(nonce and any(isinstance(p,str) and MARKER in p and '<!-- DAFEIYU_CAP:'+nonce+' -->' in p for p in prompts))


def constrain_payload(payload,allow_contract=False,allow_structured=False,sanitize_messages=None,voice_tones=()):
    messages=payload.get('messages',[])
    if not (allow_contract or allow_structured):
        return
    payload['response_format']={'type':'json_object'}
    if getattr(messages,'_dafeiyu_protocol_projected',False):return
    if allow_contract:
        messages=WireMessages(messages)
        payload['messages']=messages
    for index,message in enumerate(messages if allow_contract else []):
        if message.get('role')!='assistant' or message.get('tool_calls'):continue
        body=message.get('content')
        if isinstance(body,list) and body and all(isinstance(p,dict) and p.get('type')=='text' and isinstance(p.get('text'),str) for p in body):
            body=''.join(p['text'] for p in body)
        if not isinstance(body,str):continue
        # Wire-only examples use the same protocol. Never persist this invented metadata as history or evidence.
        wire=dict(message)
        wire['content']=json.dumps({'focus':['social'],'claims':[],
            'learning':{'privacy':{'action':'none','evidence':'','ids':[],'subject':'speaker','modality':'direct'},'items':[]},
            'expression':'neutral','reply':body},ensure_ascii=False)
        messages[index]=wire
    if allow_contract:
        return project_turn_data(messages, MARKER, sanitize_messages, voice_tones)


def isolate_background(payload):
    # A summarizer can copy the current system message into contexts. That does
    # not make it a chat generation, nor authorize the chat's JSON instructions.
    messages=list(payload.get('messages',[]));changed=False
    for index,message in enumerate(messages):
        body=message.get('content')
        if (message.get('role')!='system' or not isinstance(body,str)
                or MARKER not in body or not re.search(r'<!-- DAFEIYU_CAP:[a-f0-9]{32} -->',body)):continue
        for name in ('GENERATION','REQUEST_DATA','RELATIONSHIP','WORLD_PLAN'):
            body=re.sub(r'\n?<!-- BEGIN_DAFEIYU_'+name+r' -->.*?<!-- END_DAFEIYU_'+name+r' -->','',body,flags=re.S)
        messages[index]=dict(message,content=body);changed=True
    if changed:payload['messages']=messages


class ProviderContracts:
    def __init__(self, is_current=None):
        self.installed = []
        self.runner_adapter = None
        self.closed = False
        self.is_current = is_current

    def install_runner(self):
        if self.closed:
            return
        if self.runner_adapter is None:
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
            from .turn_history import DialogueTurnTruncator

            original=ToolLoopAgentRunner._sanitize_contexts_for_provider
            original_reset=ToolLoopAgentRunner.reset
            owner=self
            @functools.wraps(original)
            def contexts(runner,messages):
                result=original(runner,messages)
                binding=getattr(runner.req,'_dafeiyu_generation_binding',None)
                if not owner.closed and binding and binding[0] is owner:
                    owner.install(runner.provider)
                    from dafeiyu.runtime.outbound import project_history
                    result=project_history(result,binding[2])
                    return NativeContexts(result,binding[1],getattr(runner.req,'_dafeiyu_route_decision',False),
                                          getattr(runner.req,'_dafeiyu_outfit_choice',None),
                                          getattr(runner.req,'_dafeiyu_voice_tones',()))
                return result
            @functools.wraps(original_reset)
            async def reset(runner, *args, **kwargs):
                await original_reset(runner, *args, **kwargs)
                binding = getattr(runner.req, '_dafeiyu_generation_binding', None)
                if not owner.closed and binding and binding[0] is owner:
                    runner.request_context_manager.truncator = DialogueTurnTruncator()

            methods = {
                '_sanitize_contexts_for_provider': (original, contexts),
                'reset': (original_reset, reset),
            }
            for name, (_, wrapper) in methods.items():
                setattr(ToolLoopAgentRunner, name, wrapper)
            self.runner_adapter = (ToolLoopAgentRunner, methods)

    def bind_request(self,request,nonce,outgoing=()):
        if self.closed:
            return
        self.install_runner()
        request._dafeiyu_generation_binding=(self,nonce,copy.deepcopy(list(outgoing)))

    def install(self,provider):
        if self.closed or provider is None:
            return False
        previous = getattr(provider, '_dafeiyu_generation_json_owner', None)
        if previous is self:
            return True
        if previous is not None:
            # After a module reload the old class has the same qualified name but
            # a different Python identity. Only the registered live plugin may
            # retire its own predecessor, and only for a provider it actually owns.
            cls = type(previous)
            if (not self.is_current or not self.is_current() or cls.__module__ != __name__
                    or cls.__name__ != 'ProviderContracts'
                    or not any(item[0] is provider for item in previous.installed)):
                return False
            registered = getattr(previous, 'is_current', None)
            if registered and registered() and not previous.closed:
                return False
            if not hasattr(previous, 'closed'):
                # Pre-retirement versions can reinstall hooks after close() when
                # a companion job resumes with an old controller. Its old
                # metered_provider ignores False, so stop it before a model call.
                def retired_install(*args, **kwargs):
                    raise RuntimeError('Dialogue provider binding has retired')
                previous.install = retired_install
                previous.install_runner = retired_install
            previous.close()
            if getattr(provider, '_dafeiyu_generation_json_owner', None) is not None:
                return False
            from astrbot.api import logger
            logger.info('[dialogue-binding] released a retired dialogue provider owner')
        convert=getattr(provider,'_finally_convert_payload',None)
        chat=getattr(provider,'text_chat',None)
        if not callable(convert) or not callable(chat):return False
        originals={'_finally_convert_payload':convert,'text_chat':chat}
        sanitize_messages = getattr(provider, '_sanitize_assistant_messages', None)

        @functools.wraps(convert)
        def convert_payload(payload):
            convert(payload)
            if self.closed:
                return
            own,structured,voice_tones=WIRE_SCOPE.get()
            if not own:isolate_background(payload)
            dedup = constrain_payload(payload, own, structured, sanitize_messages, voice_tones)
            if dedup and dedup[0]:
                from astrbot.api import logger

                logger.info('[cache-history-refs] refs=%d characters_saved=%d', *dedup)

        @functools.wraps(chat)
        async def text_chat(*args,**kwargs):
            if self.closed:
                return await chat(*args, **kwargs)
            own=contract_request(kwargs)
            first_route=False
            if own:
                scoped=route_tools(kwargs)
                if scoped is kwargs:scoped=outfit_tools(kwargs)
                first_route=scoped is not kwargs
                kwargs=scoped
                kwargs['request_max_retries']=1
            structured=bool(STRUCTURED.get() and STRUCTURED_MARKER in str(kwargs.get('system_prompt') or ''))
            contexts=kwargs.get('contexts')
            voice_tones=contexts.voice_tones if own and isinstance(contexts,NativeContexts) else ()
            token=WIRE_SCOPE.set((own,structured,voice_tones))
            started=time.perf_counter()
            nonce = contexts.nonce if isinstance(contexts, NativeContexts) else None
            try:
                try:
                    with turn_audit.provider_attempt(nonce):
                        result = await chat(*args, **kwargs)
                except Exception as error:
                    message=str(error).lower()
                    if (not first_route or getattr(error,'status_code',None) not in (400,422)
                            or 'tool_choice' not in message or not any(s in message for s in ('not support','unsupported','invalid','not allowed'))):
                        raise
                    with turn_audit.provider_attempt(nonce):
                        result = await chat(*args, **dict(kwargs, tool_choice='auto'))
            finally:WIRE_SCOPE.reset(token)
            if own:
                if getattr(result,'tools_call_name',None):result.completion_text=''
                usage=getattr(result, 'usage', None)
                if usage and usage.input > 0:
                    from astrbot.api import logger

                    contexts=kwargs.get('contexts')
                    nonce=contexts.nonce if isinstance(contexts, NativeContexts) else CURRENT.get()
                    logger.info(
                        '[cache-round] turn=%s input=%d hit=%d miss=%d elapsed_ms=%d tool_calls=%d',
                        hashlib.sha256(nonce.encode()).hexdigest()[:12],
                        usage.input, usage.input_cached, usage.input_other,
                        round((time.perf_counter()-started)*1000), len(getattr(result, 'tools_call_name', None) or ()),
                    )
            return result

        wrappers={'_finally_convert_payload':convert_payload,'text_chat':text_chat}
        overrides=getattr(provider,'_apply_provider_specific_request_overrides',None)
        if callable(overrides):
            originals['_apply_provider_specific_request_overrides']=overrides
            @functools.wraps(overrides)
            def request_overrides(payload,extra_body):
                overrides(payload,extra_body)
                if self.closed:
                    return
                budget=OUTPUT_BUDGET.get()
                if WIRE_SCOPE.get()[1] and type(budget) is int and budget>0:
                    # AstrBot merges custom_extra_body after conversion. Override here,
                    # on this request only; shared provider configuration stays intact.
                    key='max_completion_tokens' if 'max_completion_tokens' in payload or 'max_completion_tokens' in extra_body else 'max_tokens'
                    for name in ('max_tokens','max_completion_tokens'):
                        payload.pop(name,None);extra_body.pop(name,None)
                    payload[key]=budget
                turn_audit.observe_payload(payload, extra_body)
            wrappers['_apply_provider_specific_request_overrides']=request_overrides
        stream=getattr(provider,'text_chat_stream',None)
        if callable(stream) and inspect.isasyncgenfunction(stream):
            originals['text_chat_stream']=stream
            @functools.wraps(stream)
            async def text_chat_stream(*args,**kwargs):
                if self.closed:
                    async for chunk in stream(*args, **kwargs):yield chunk
                    return
                if contract_request(kwargs):
                    # A partial JSON envelope must never become a streaming QQ message.
                    result=await text_chat(*args,**kwargs)
                    result.is_chunk=False
                    yield result
                    return
                token=WIRE_SCOPE.set((False,False,()))
                try:
                    async for chunk in stream(*args,**kwargs):yield chunk
                finally:WIRE_SCOPE.reset(token)
            wrappers['text_chat_stream']=text_chat_stream
        for name,method in wrappers.items():setattr(provider,name,method)
        provider._dafeiyu_generation_json_owner=self
        self.installed.append((provider,originals,wrappers))
        return True

    def close(self):
        self.closed = True
        if self.runner_adapter is not None:
            runner, methods = self.runner_adapter
            for name, (original, wrapper) in methods.items():
                if getattr(runner, name) is wrapper:
                    setattr(runner, name, original)
            self.runner_adapter=None
        for provider,originals,wrappers in self.installed:
            for name,original in originals.items():
                if getattr(provider,name,None) is wrappers[name]:setattr(provider,name,original)
            if getattr(provider,'_dafeiyu_generation_json_owner',None) is self:
                delattr(provider,'_dafeiyu_generation_json_owner')
        self.installed.clear()
