"""Describe actual published photos once; failures never block or repeat publication."""
import asyncio
import base64
import contextlib

from astrbot.api import logger
from dafeiyu.media.photo_memory import PhotoMemoryStore,CAPTION_POLICY,image_material,parse_caption

BOT=7000000002


class PhotoMemory:
    def __init__(self,config):
        self.cfg=config;self.store=PhotoMemoryStore(config.db_path,BOT)
        self.lock=asyncio.Lock();self.tasks=set();self.closed=False

    def provider(self):
        settings=self.cfg.context.get_config().get('provider_settings',{})
        ident=settings.get('default_image_caption_provider_id','')
        provider=self.cfg.context.get_provider_by_id(ident) if ident else None
        return ident,provider

    async def initialize(self):await asyncio.to_thread(self.store.initialize)

    async def lookup(self,tid,ordinal=0):return await asyncio.to_thread(self.store.for_post,tid,ordinal)

    async def describe(self,tid,ordinal,raw,kind):
        async with self.lock:
            if self.closed:return None
            sha,picture,size=await asyncio.to_thread(image_material,raw)
            if not await asyncio.to_thread(self.store.bind,tid,ordinal,sha,kind):return None
            cached=await asyncio.to_thread(self.store.get,sha)
            if cached:return dict(image_sha256=sha,description=cached,source='visual_observation',cached=True)
            ident,provider=self.provider()
            if provider is None:return None
            if not await asyncio.to_thread(self.store.claim,sha,ident,size):return None
            try:
                response=await asyncio.wait_for(provider.text_chat(prompt='请记录这张照片里的具体内容。',
                    system_prompt=CAPTION_POLICY,image_urls=['data:image/jpeg;base64,'+base64.b64encode(picture).decode()],
                    contexts=[],func_tool=None,request_max_retries=1),timeout=40)
                caption=parse_caption(response.completion_text)
                if await asyncio.to_thread(self.store.finish,sha,caption):
                    return dict(image_sha256=sha,description=caption,source='visual_observation',cached=False)
            except asyncio.CancelledError:
                await asyncio.to_thread(self.store.finish,sha,None,'Interrupted');raise
            except Exception as error:
                await asyncio.to_thread(self.store.finish,sha,None,type(error).__name__)
                logger.warning('Photo memory unavailable: %s',type(error).__name__)
            return None

    def published(self,post,pictures):
        if self.closed or int(post.uin)!=BOT or not post.tid or not pictures:return
        async def remember():
            for index,raw in enumerate(pictures[:9]):
                try:await self.describe(str(post.tid),index,raw,'published_upload')
                except asyncio.CancelledError:raise
                except Exception as error:logger.warning('Published photo memory unavailable: %s',type(error).__name__)
        task=asyncio.create_task(remember());self.tasks.add(task);task.add_done_callback(self.tasks.discard)

    async def status(self):
        ident,provider=self.provider()
        return dict(enabled=True,provider_configured=bool(provider),provider_id=ident,
            pending_jobs=len(self.tasks),observations=await asyncio.to_thread(self.store.status),
            content_addressed_cache=True,automatic_attempts_per_image=1,blocks_publication=False,
            existing_photos='on_view',repeat_recognition_on_chat=False)

    async def terminate(self):
        self.closed=True
        for task in tuple(self.tasks):task.cancel()
        for task in tuple(self.tasks):
            with contextlib.suppress(asyncio.CancelledError):await task
