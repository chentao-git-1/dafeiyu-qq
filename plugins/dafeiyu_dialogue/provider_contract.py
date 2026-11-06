"""Request-local JSON mode for the inspected AstrBot OpenAI-compatible adapter."""
import functools
import inspect
import json
import re
import copy
from contextvars import ContextVar
import uuid

MARKER='<!-- BEGIN_DAFEIYU_GENERATION -->'
STRUCTURED_MARKER='<!-- DAFEIYU_STRUCTURED_TASK -->'
CURRENT=ContextVar('dafeiyu_generation_capability',default=None)
STRUCTURED=ContextVar('dafeiyu_structured_task',default=False)
WIRE_SCOPE=ContextVar('dafeiyu_wire_scope',default=(False,False))


class WireMessages(list):
    _dafeiyu_protocol_projected=True


class NativeContexts(list):
    """Capability attached by the owned runner, never inferred from chat text."""
    def __init__(self,messages,nonce):
        super().__init__(messages);self.nonce=nonce

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


def constrain_payload(payload,allow_contract=False,allow_structured=False):
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


def isolate_background(payload):
    # A summarizer can copy the current system message into contexts. That does
    # not make it a chat generation, nor authorize the chat's JSON instructions.
    messages=list(payload.get('messages',[]));changed=False
    for index,message in enumerate(messages):
        body=message.get('content')
        if (message.get('role')!='system' or not isinstance(body,str)
                or MARKER not in body or not re.search(r'<!-- DAFEIYU_CAP:[a-f0-9]{32} -->',body)):continue
        for name in ('GENERATION','REQUEST_DATA'):
            body=re.sub(r'\n?<!-- BEGIN_DAFEIYU_'+name+r' -->.*?<!-- END_DAFEIYU_'+name+r' -->','',body,flags=re.S)
        messages[index]=dict(message,content=body);changed=True
    if changed:payload['messages']=messages


class ProviderContracts:
    def __init__(self):self.installed=[];self.runner_adapter=None

    def install_runner(self):
        if self.runner_adapter is None:
            from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
            original=ToolLoopAgentRunner._sanitize_contexts_for_provider
            owner=self
            @functools.wraps(original)
            def contexts(runner,messages):
                result=original(runner,messages)
                binding=getattr(runner.req,'_dafeiyu_generation_binding',None)
                if binding and binding[0] is owner:
                    owner.install(runner.provider)
                    from dafeiyu.runtime.outbound import project_history
                    result=project_history(result,binding[2])
                    return NativeContexts(result,binding[1])
                return result
            ToolLoopAgentRunner._sanitize_contexts_for_provider=contexts
            self.runner_adapter=(ToolLoopAgentRunner,original,contexts)

    def bind_request(self,request,nonce,outgoing=()):
        self.install_runner()
        request._dafeiyu_generation_binding=(self,nonce,copy.deepcopy(list(outgoing)))

    def install(self,provider):
        if provider is None:return False
        if getattr(provider,'_dafeiyu_generation_json_owner',None) is self:return True
        if getattr(provider,'_dafeiyu_generation_json_owner',None) is not None:return False
        convert=getattr(provider,'_finally_convert_payload',None)
        chat=getattr(provider,'text_chat',None)
        if not callable(convert) or not callable(chat):return False
        originals={'_finally_convert_payload':convert,'text_chat':chat}

        @functools.wraps(convert)
        def convert_payload(payload):
            convert(payload)
            own,structured=WIRE_SCOPE.get()
            if not own:isolate_background(payload)
            constrain_payload(payload,own,structured)

        @functools.wraps(chat)
        async def text_chat(*args,**kwargs):
            own=contract_request(kwargs)
            if own:kwargs['request_max_retries']=1
            structured=bool(STRUCTURED.get() and STRUCTURED_MARKER in str(kwargs.get('system_prompt') or ''))
            token=WIRE_SCOPE.set((own,structured))
            try:result=await chat(*args,**kwargs)
            finally:WIRE_SCOPE.reset(token)
            if own:
                if getattr(result,'tools_call_name',None):result.completion_text=''
            return result

        wrappers={'_finally_convert_payload':convert_payload,'text_chat':text_chat}
        stream=getattr(provider,'text_chat_stream',None)
        if callable(stream) and inspect.isasyncgenfunction(stream):
            originals['text_chat_stream']=stream
            @functools.wraps(stream)
            async def text_chat_stream(*args,**kwargs):
                if contract_request(kwargs):
                    # A partial JSON envelope must never become a streaming QQ message.
                    result=await text_chat(*args,**kwargs)
                    result.is_chunk=False
                    yield result
                    return
                token=WIRE_SCOPE.set((False,False))
                try:
                    async for chunk in stream(*args,**kwargs):yield chunk
                finally:WIRE_SCOPE.reset(token)
            wrappers['text_chat_stream']=text_chat_stream
        for name,method in wrappers.items():setattr(provider,name,method)
        provider._dafeiyu_generation_json_owner=self
        self.installed.append((provider,originals,wrappers))
        return True

    def close(self):
        if self.runner_adapter is not None:
            runner,original,wrapper=self.runner_adapter
            if runner._sanitize_contexts_for_provider is wrapper:runner._sanitize_contexts_for_provider=original
            self.runner_adapter=None
        for provider,originals,wrappers in self.installed:
            for name,original in originals.items():
                if getattr(provider,name,None) is wrappers[name]:setattr(provider,name,original)
            if getattr(provider,'_dafeiyu_generation_json_owner',None) is self:
                delattr(provider,'_dafeiyu_generation_json_owner')
        self.installed.clear()
