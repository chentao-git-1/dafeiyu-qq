"""Actual image bytes, fake vision responses, confirmed publication and durable recall."""
import asyncio
import gc
import io
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch

from PIL import Image
import test_public_memory_bridge as f
from dafeiyu.media.photo_memory import image_material,parse_caption
from dafeiyu.dialogue.catalog import MemoryCatalog
from dafeiyu.dialogue.contracts import TurnContext
from dafeiyu.dialogue.retrieval import lexical_rank
from data.plugins.astrbot_plugin_qzone.core.db import PostDB
from data.plugins.astrbot_plugin_qzone.core.model import Post
from data.plugins.astrbot_plugin_qzone.core.photo_memory import PhotoMemory,BOT
from data.plugins.astrbot_plugin_qzone.core.own_space import OwnSpace
from data.plugins.astrbot_plugin_qzone.core.service import PostService
from data.plugins.astrbot_plugin_qzone.core.qzone.api import QzoneAPI
from data.plugins.astrbot_plugin_qzone.core.qzone.model import ApiResponse

CAPTION=dict(summary='窗边摆着一只蓝色杯子，后方有黄白相间的格子窗帘。',subjects=['蓝色杯子'],
    scene='室内近景，左侧窗户透进柔和光线。',details=['杯柄朝右','窗帘有黄白格纹'],
    visible_text=[],uncertain='远处文字太小，无法辨认。')


def picture(color='blue'):
    output=io.BytesIO();Image.new('RGB',(64,48),color).save(output,format='PNG');return output.getvalue()


class VisualMemory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.path=self.root/'space.db';self.now=time.time()
        self.provider=NS(text_chat=AsyncMock(return_value=NS(completion_text=json.dumps(CAPTION,ensure_ascii=False))))
        self.context=NS(get_config=lambda:{'provider_settings':{'default_image_caption_provider_id':'vision'}},
            get_provider_by_id=lambda name:self.provider if name=='vision' else None)
        self.cfg=NS(db_path=self.path,context=self.context)
        self.db=PostDB(self.cfg);await self.db.initialize()
        self.memory=PhotoMemory(self.cfg);await self.memory.initialize()
        self.post=Post(tid='confirmed',uin=BOT,text='文案不能代替照片画面',images=['https://m.qpic.cn/photo/0'],create_time=int(self.now)-10)
        await self.db.save(self.post)
        self.turn=TurnContext(f.OWNER,f.ORIGIN,'dafeiyu-qq','photo-test',self.now)
        self.catalog=MemoryCatalog(self.root,public_space=dict(path=self.path,uin=BOT,platform='dafeiyu-qq',comments_owner=f.OWNER))

    async def asyncTearDown(self):
        await self.memory.terminate();gc.collect();self.tmp.cleanup()

    def photos(self):return [r for r in self.catalog.read(self.turn,time.time()).items if r.kind=='publication_photo']

    async def test_real_pixels_are_sent_once_and_description_survives_without_image_file(self):
        raw=picture();one=await self.memory.describe(self.post.tid,0,raw,'published_upload')
        two=await self.memory.describe(self.post.tid,0,raw,'own_space_read')
        self.assertFalse(one['cached']);self.assertTrue(two['cached']);self.provider.text_chat.assert_awaited_once()
        request=self.provider.text_chat.await_args.kwargs
        self.assertTrue(request['image_urls'][0].startswith('data:image/jpeg;base64,'))
        self.assertNotIn(self.post.text,json.dumps(request,ensure_ascii=False))
        self.assertEqual(request['contexts'],[]);self.assertIsNone(request['func_tool']);self.assertEqual(request['request_max_retries'],1)
        fresh=PhotoMemory(self.cfg);saved=await fresh.lookup(self.post.tid)
        self.assertEqual(saved['description'],CAPTION)
        photos=self.photos();self.assertEqual(len(photos),1);data=json.loads(photos[0].text)
        self.assertEqual(data['description']['details'],CAPTION['details'])
        self.assertEqual(data['description_source'],'actual_image_observation')
        self.assertNotIn('generation_request',photos[0].text)
        self.assertEqual(lexical_rank(photos,'黄白格纹窗帘')[0].id,photos[0].id)
        self.assertEqual(lexical_rank(photos,'你好呀'),[])

    async def test_same_photo_across_posts_reuses_observation_and_keeps_attribution(self):
        await self.memory.describe(self.post.tid,0,picture(),'published_upload')
        other=Post(tid='second',uin=BOT,text='另一条动态',images=['https://m.qpic.cn/second/0'])
        await self.db.save(other);await self.memory.describe(other.tid,0,picture(),'published_upload')
        self.provider.text_chat.assert_awaited_once();self.assertEqual(len(self.photos()),2)
        await self.db.delete(self.post.id)
        self.assertEqual(len(self.photos()),1);self.assertIsNotNone(await self.memory.lookup('second'))
        await self.db.delete(other.id);self.assertEqual(self.photos(),[])
        self.assertEqual(await asyncio.to_thread(self.memory.store.status),{})

    async def test_unconfirmed_other_account_and_invalid_image_number_cannot_create_visual_memory(self):
        for post in (Post(tid='draft',uin=BOT,status='pending',images=['x']),Post(tid='other',uin=7000000011,images=['x'])):
            await self.db.save(post)
            self.assertIsNone(await self.memory.describe(post.tid,0,picture(),'own_space_read'))
        self.assertIsNone(await self.memory.describe('confirmed',1,picture(),'own_space_read'))
        self.provider.text_chat.assert_not_awaited();self.assertEqual(self.photos(),[])

    async def test_failed_description_is_not_memory_and_is_not_automatically_retried(self):
        self.provider.text_chat.side_effect=RuntimeError('fake provider unavailable')
        self.assertIsNone(await self.memory.describe('confirmed',0,picture(),'published_upload'))
        self.assertIsNone(await self.memory.describe('confirmed',0,picture(),'own_space_read'))
        self.provider.text_chat.assert_awaited_once();self.assertEqual(self.photos(),[])
        self.assertEqual((await self.memory.status())['observations'],{'failed':1})

    async def test_delete_during_vision_does_not_resurrect_content(self):
        async def respond(**kwargs):
            await self.db.delete(self.post.id);return NS(completion_text=json.dumps(CAPTION))
        self.provider.text_chat.side_effect=respond
        self.assertIsNone(await self.memory.describe('confirmed',0,picture(),'published_upload'))
        self.assertEqual(self.photos(),[]);self.assertIsNone(await self.memory.lookup('confirmed'))
        self.assertEqual((await self.memory.status())['observations'],{})

    async def test_multiple_images_have_separate_content_and_new_observation_replaces_same_slot(self):
        self.post.images.append('https://m.qpic.cn/photo-two/0');await self.db.save(self.post)
        await self.memory.describe('confirmed',0,picture('blue'),'published_upload')
        await self.memory.describe('confirmed',1,picture('red'),'published_upload')
        self.assertEqual([json.loads(r.text)['image_number'] for r in self.photos()],[1,2])
        self.provider.text_chat.return_value=NS(completion_text=json.dumps(dict(CAPTION,summary='一张绿色的照片')))
        await self.memory.describe('confirmed',0,picture('green'),'own_space_read')
        self.assertEqual(len(self.photos()),2);self.assertIn('绿色',self.photos()[0].text)

    async def test_old_picture_view_is_saved_then_reused_without_download_or_second_caption(self):
        plugin=NS(cfg=self.cfg,db=self.db,photo_memory=self.memory)
        space=OwnSpace(plugin);space.cache[(0,3,False)]=dict(source='actual_own_qzone',live_verified=True,
            checked_at=time.time(),posts=[space.view(self.post)],from_cache=False)
        with patch('data.plugins.astrbot_plugin_qzone.core.own_space.fetch_image',AsyncMock(return_value=picture())) as fetch:
            one,attached=await space.read(include_images=True)
            self.assertIsNone(attached);self.assertEqual(one['posts'][0]['photo_memory']['description'],CAPTION)
            two,attached=await space.read(include_images=True)
            fetch.assert_awaited_once();self.provider.text_chat.assert_awaited_once()
            self.assertTrue(two['posts'][0]['photo_memory']['cached']);self.assertIsNone(attached)
        self.assertEqual(len(self.photos()),1)

    async def test_cached_photo_is_available_when_live_space_is_unavailable(self):
        await self.memory.describe('confirmed',0,picture(),'published_upload')
        plugin=NS(cfg=self.cfg,db=self.db,photo_memory=self.memory,session=NS(get_uin=AsyncMock(side_effect=RuntimeError())))
        payload,attached=await OwnSpace(plugin).read(include_images=True)
        self.assertFalse(payload['live_verified']);self.assertIn('photo_memory',payload['posts'][0])
        self.assertIsNone(attached);self.provider.text_chat.assert_awaited_once()

    async def test_selected_second_photo_has_its_own_memory_and_deleted_cached_content_is_not_reused(self):
        self.post.images.append('https://m.qpic.cn/second/0');await self.db.save(self.post)
        space=OwnSpace(NS(cfg=self.cfg,db=self.db,photo_memory=self.memory))
        space.cache[(0,3,False)]=dict(source='actual_own_qzone',live_verified=True,checked_at=time.time(),posts=[space.view(self.post)])
        with patch('data.plugins.astrbot_plugin_qzone.core.own_space.fetch_image',AsyncMock(return_value=picture())) as fetch:
            payload,_=await space.read(include_images=True,image_index=1)
            self.assertEqual(payload['posts'][0]['photo_memory']['image_number'],2)
            fetch.assert_awaited_once_with(self.post.images[1])
            await self.db.delete(self.post.id)
            payload,_=await space.read(include_images=True,image_index=1)
            self.assertNotIn('photo_memory',payload['posts'][0])
        self.provider.text_chat.assert_awaited_once()

    async def test_vision_failure_does_not_trigger_another_implicit_caption(self):
        self.provider.text_chat.side_effect=RuntimeError()
        space=OwnSpace(NS(cfg=self.cfg,db=self.db,photo_memory=self.memory))
        space.cache[(0,3,False)]=dict(source='actual_own_qzone',live_verified=True,checked_at=time.time(),posts=[space.view(self.post)])
        with patch('data.plugins.astrbot_plugin_qzone.core.own_space.fetch_image',AsyncMock(return_value=picture())):
            payload,attached=await space.read(include_images=True)
        self.assertTrue(payload['photo_memory_unavailable']);self.assertIsNone(attached);self.assertFalse(payload['image_attached'])

    async def test_public_photo_recall_is_read_only_and_uses_existing_public_acl(self):
        await self.memory.describe('confirmed',0,picture(),'published_upload')
        before=self.path.read_bytes();self.catalog.read(self.turn,time.time());self.assertEqual(before,self.path.read_bytes())
        group=TurnContext(f.OWNER,'dafeiyu-qq:GroupMessage:7000000101','dafeiyu-qq','group',time.time())
        self.assertTrue(any(r.kind=='publication_photo' for r in self.catalog.read(group,time.time()).items))
        other=TurnContext(f.OWNER,'other:FriendMessage:'+f.OWNER,'other','other',time.time())
        self.assertEqual(self.catalog.read(other,time.time()).items,())

    async def test_confirmed_publish_queues_observation_without_waiting_for_vision(self):
        entered=asyncio.Event();release=asyncio.Event()
        async def vision(**kwargs):
            entered.set();await release.wait();return NS(completion_text=json.dumps(CAPTION))
        self.provider.text_chat.side_effect=vision
        async def publish(post):
            post._published_images=[picture()]
            return ApiResponse(True,0,None,dict(tid='new-post',now=int(time.time())),{})
        service=PostService(NS(publish=AsyncMock(side_effect=publish)),
            NS(get_uin=AsyncMock(return_value=BOT),get_nickname=AsyncMock(return_value='synthetic')),self.db,NS(editorial=None))
        service.photo_memory=self.memory
        post=await service.publish_post(text='成功发出的照片',images=['synthetic.png'])
        self.assertEqual(post.tid,'new-post');self.assertEqual(post._published_images,[])
        await asyncio.wait_for(entered.wait(),3);self.assertFalse(release.is_set())
        release.set();await asyncio.gather(*self.memory.tasks)
        self.assertIsNotNone(await self.memory.lookup('new-post'))

    async def test_failed_publish_never_queues_photo_work(self):
        service=PostService(NS(publish=AsyncMock(return_value=ApiResponse(False,1,'failed',{},{}))),
            NS(get_uin=AsyncMock(return_value=BOT),get_nickname=AsyncMock(return_value='synthetic')),self.db,NS(editorial=None))
        service.photo_memory=self.memory
        with self.assertRaises(RuntimeError):await service.publish_post(text='未发出',images=['synthetic.png'])
        self.assertFalse(self.memory.tasks);self.provider.text_chat.assert_not_awaited()


class ImageAndTransport(unittest.IsolatedAsyncioTestCase):
    def test_image_identity_uses_pixels_and_description_requires_real_content(self):
        self.assertEqual(image_material(picture())[0],image_material(picture())[0])
        self.assertNotEqual(image_material(picture())[0],image_material(picture('red'))[0])
        with self.assertRaises(Exception):image_material(b'not an image')
        for raw in ('{}',json.dumps(dict(CAPTION,summary='')),json.dumps(dict(CAPTION,subjects=['x']*7))):
            with self.assertRaises(ValueError):parse_caption(raw)

    async def test_upload_transfers_only_successfully_published_actual_bytes_to_memory(self):
        api=QzoneAPI.__new__(QzoneAPI);api.session=NS(get_ctx=AsyncMock(return_value=NS(uin=BOT,gtk2='fake')))
        api._upload_image=AsyncMock(return_value=ApiResponse(True,0,None,{},{}))
        api.request=AsyncMock(return_value=dict(code=0,tid='posted'))
        post=Post(uin=BOT,text='published',images=['synthetic.png']);raw=picture()
        with patch('data.plugins.astrbot_plugin_qzone.core.qzone.api.normalize_images',AsyncMock(return_value=[raw])),\
                patch('data.plugins.astrbot_plugin_qzone.core.qzone.api.QzoneParser.parse_upload_result',return_value=('fake-bo','fake-richval')):
            response=await api.publish(post);self.assertTrue(response.ok);self.assertEqual(post._published_images,[raw])
            self.assertNotIn('_published_images',post.model_dump())
            api.request.return_value=dict(code=1)
            response=await api.publish(post);self.assertFalse(response.ok);self.assertEqual(post._published_images,[])


if __name__=='__main__':unittest.main()
