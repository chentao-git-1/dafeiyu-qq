"""Request-local JSON mode for the inspected AstrBot OpenAI-compatible adapter."""
import functools
import inspect
import json
from contextvars import ContextVar
import uuid

MARKER='<!-- BEGIN_DAFEIYU_GENERATION -->'
STRUCTURED_MARKER='<!-- DAFEIYU_STRUCTURED_TASK -->'
CURRENT=ContextVar('dafeiyu_generation_capability',default=None)
STRUCTURED=ContextVar('dafeiyu_structured_task',default=False)


class WireMessages(list):
    _dafeiyu_protocol_projected=True


def arm_contract(enabled):
    nonce=uuid.uuid4().hex if enabled else None
    CURRENT.set(nonce)
    return nonce


def contract_request(kwargs):
    prompt=str(kwargs.get('system_prompt') or '')
    nonce=CURRENT.get()
    return bool(nonce and MARKER in prompt and '<!-- DAFEIYU_CAP:'+nonce+' -->' in prompt)


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


class ProviderContracts:
    def __init__(self):self.installed=[]

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
            systems=[m.get('content','') for m in payload.get('messages',[]) if m.get('role')=='system' and isinstance(m.get('content'),str)]
            own=any(contract_request({'system_prompt':s}) for s in systems)
            structured=STRUCTURED.get() and any(STRUCTURED_MARKER in s for s in systems)
            constrain_payload(payload,own,structured)

        @functools.wraps(chat)
        async def text_chat(*args,**kwargs):
            own=contract_request(kwargs)
            if own:kwargs['request_max_retries']=0
            result=await chat(*args,**kwargs)
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
                async for chunk in stream(*args,**kwargs):yield chunk
            wrappers['text_chat_stream']=text_chat_stream
        for name,method in wrappers.items():setattr(provider,name,method)
        provider._dafeiyu_generation_json_owner=self
        self.installed.append((provider,originals,wrappers))
        return True

    def close(self):
        for provider,originals,wrappers in self.installed:
            for name,original in originals.items():
                if getattr(provider,name,None) is wrappers[name]:setattr(provider,name,original)
            if getattr(provider,'_dafeiyu_generation_json_owner',None) is self:
                delattr(provider,'_dafeiyu_generation_json_owner')
        self.installed.clear()
