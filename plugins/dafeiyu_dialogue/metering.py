"""Numeric provider accounting for every new auxiliary/background request."""
import time
from .provider_contract import STRUCTURED, STRUCTURED_MARKER


class MeteredProvider:
    def __init__(self,provider,statistics,purpose,structured=True):
        self.provider,self.statistics,self.purpose,self.structured=provider,statistics,purpose,structured

    def __getattr__(self,name):return getattr(self.provider,name)

    async def text_chat(self,**kwargs):
        purpose=self.purpose;stats=self.statistics;stats[purpose+'_calls']+=1
        started=time.perf_counter();token=None
        if self.structured:
            kwargs['system_prompt']=str(kwargs.get('system_prompt') or '')+'\n'+STRUCTURED_MARKER
            kwargs['request_max_retries']=1
            token=STRUCTURED.set(True)
        try:
            response=await self.provider.text_chat(**kwargs)
            usage=getattr(response,'usage',None)
            if usage is None:stats[purpose+'_usage_unavailable']+=1
            else:
                for name in ('input_other','input_cached','output'):
                    value=getattr(usage,name,None)
                    if type(value) is int and value>=0:stats[purpose+'_'+name]+=value
            return response
        except BaseException:
            stats[purpose+'_failed']+=1
            raise
        finally:
            if token is not None:STRUCTURED.reset(token)
            stats[purpose+'_elapsed_ms']+=round((time.perf_counter()-started)*1000)
