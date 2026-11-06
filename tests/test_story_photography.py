"""Role photographs retain provenance, delivery scope and continuity; mock image/text providers only."""
import asyncio
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch

import test_nightly_agenda as agenda
from dafeiyu.media.drawing.works import WorkHistory
from dafeiyu.media.drawing.framing import image_facts,image_prompt
from dafeiyu.dialogue.growth import own_experiences
from dafeiyu.dialogue.self_state import self_state
from dafeiyu.life.planning import parse_choice
from data.plugins.astrbot_plugin_dafeiyu_draw import main as adapter
from data.plugins.astrbot_plugin_qzone.core.post_images import prepare_post_images
f=agenda.base.fixture


class PhotoHistory(unittest.TestCase):
    def test_legacy_read_is_read_only_and_migration_preserves_old_artwork(self):
        with tempfile.TemporaryDirectory() as tmp:
            images=Path(tmp)/'images';images.mkdir();old=images/'drawing-old.png';old.write_bytes(b'old artifact')
            path=images.parent/'works.db';sha=hashlib.sha256(old.read_bytes()).hexdigest()
            with closing(sqlite3.connect(path)) as db,db:
                db.execute('CREATE TABLE works(id TEXT PRIMARY KEY,actor TEXT,origin TEXT,artifact TEXT,sha TEXT,description TEXT,completed REAL)')
                db.execute('INSERT INTO works VALUES(?,?,?,?,?,?,?)',('old',f.OWNER,f.ORIGIN,old.name,sha,'旧画作',1000))
            store=WorkHistory(images,clock=lambda:1100);before=path.read_bytes()
            self.assertEqual(store.recent(f.OWNER,f.ORIGIN)[0]['presentation'],'artwork');self.assertEqual(path.read_bytes(),before)
            epoch=store.begin(f.OWNER,f.ORIGIN);new=images/'drawing-new.png';new.write_bytes(b'new artifact')
            ident=store.record(f.OWNER,f.ORIGIN,new,'南桥的街灯',epoch,'story_photo')
            reopened=WorkHistory(images,clock=lambda:1100);rows=reopened.recent(f.OWNER,f.ORIGIN)
            photo=next(r for r in rows if r['id']==ident)
            self.assertEqual(photo['presentation'],'story_photo');self.assertEqual(photo['production'],'generated_image')
            self.assertFalse(photo['physical_camera_used']);self.assertEqual(photo['delivery'],'sent')
            self.assertEqual(next(r for r in rows if r['id']=='old')['presentation'],'artwork')
            self.assertFalse(reopened.recent(f.FRIEND,f.ORIGIN))
            self.assertEqual(reopened.forget(f.OWNER,f.ORIGIN),2)
            self.assertIsNone(reopened.record(f.OWNER,f.ORIGIN,new,'已遗忘的在途照片',epoch,'story_photo'))

    def test_explicit_artwork_is_not_rewritten_and_invalid_modes_are_rejected(self):
        self.assertEqual(image_prompt('一个图表','artwork'),'一个图表')
        self.assertIn('临湾',image_prompt('镜头朝外的街角','story_photo'))
        with self.assertRaises(ValueError):image_facts('real_camera')
        with tempfile.TemporaryDirectory() as tmp:
            store=agenda.LifeStore(Path(tmp));packet=agenda.materials(store,agenda.SelfJournal(store.root),'2026-11-07',9,21,'2.00',now=agenda.stamp())
            value=agenda.choice(kind='reading',presentation='story_photo')
            with self.assertRaises(ValueError):parse_choice(json.dumps(value),packet)


class PhotoTool(unittest.IsolatedAsyncioTestCase):
    def drawing(self,root):
        draw=adapter.DafeiyuDraw.__new__(adapter.DafeiyuDraw)
        draw.config={'enabled':True,'api_key':'synthetic','cooldown_seconds':0};draw.lock=asyncio.Lock();draw.last={}
        draw.image_dir=root/'images';draw.image_dir.mkdir();draw.works=WorkHistory(draw.image_dir)
        draw._allowed=lambda event:True;draw._prune=lambda:None;draw.intent_context=adapter.IntentContext()
        draw.context=NS(get_config=lambda *args:{'platform_settings':{}})
        return draw

    async def test_photo_is_sent_once_and_its_meaning_survives_a_later_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            draw=self.drawing(Path(tmp));event=f.event('拍一张你的房间')
            with patch.object(adapter,'generate',AsyncMock(return_value=(b'synthetic photo','.png',(1024,1024)))) as generate:
                result=json.loads(await draw.draw_tool(event,'从窗边看向临湾的街道'))
            generate.assert_awaited_once();event.send.assert_awaited_once()
            self.assertIn('拍下的一张照片',generate.await_args.args[1])
            self.assertEqual(result['presentation'],'story_photo');self.assertEqual(result['delivery'],'sent')
            self.assertTrue(result['generated']);self.assertFalse(result['physical_camera_used'])
            rows=draw.conversation_works(f.OWNER,f.ORIGIN)
            self.assertEqual(rows[0]['content_nature'],'fictional_photo')
            self.assertTrue(rows[0]['description_is_generation_request_not_visual_inspection'])
            self.assertFalse(draw.conversation_works(f.FRIEND,f.ORIGIN))
            self.assertFalse(any(isinstance(p,adapter.Plain) for p in event.send.await_args.args[0].chain))

    async def test_artwork_continuation_inherits_mode_and_explicit_photo_can_change_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            draw=self.drawing(Path(tmp))
            with patch.object(adapter,'generate',AsyncMock(return_value=(b'synthetic work','.png',(1024,1024)))) as generate:
                first=json.loads(await draw.draw_tool(f.event('画个城市标志'),'临湾的标志设计','artwork'))
                second=json.loads(await draw.draw_tool(f.event('换成蓝色'),'把临湾标志换成蓝色'))
                third=json.loads(await draw.draw_tool(f.event('换成照片'),'拍下门口挂着的临湾标志','story_photo'))
            self.assertEqual([first['presentation'],second['presentation'],third['presentation']],['artwork','artwork','story_photo'])
            self.assertEqual(generate.await_count,3)
            self.assertNotIn('拍下的一张照片',generate.await_args_list[1].args[1])
            self.assertIn('拍下的一张照片',generate.await_args_list[2].args[1])

    async def test_unconfirmed_send_does_not_create_a_delivered_photo_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            draw=self.drawing(Path(tmp));event=f.event('拍张照片');event.send=AsyncMock(side_effect=TimeoutError())
            with patch.object(adapter,'generate',AsyncMock(return_value=(b'synthetic photo','.png',(1024,1024)))) as generate:
                result=await draw.draw_tool(event,'临湾的街道','story_photo')
            self.assertIn('发送未确认',result);self.assertFalse(draw.conversation_works(f.OWNER,f.ORIGIN))
            self.assertIsNone(event.get_extra('dafeiyu_image_result',None));generate.assert_awaited_once()


class PhotoLife(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_photo_uses_one_existing_image_call_and_remembers_story_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            now=[agenda.stamp()];value=agenda.day_plan(1)
            value['items'][0]['activity']=agenda.choice(kind='drawing',presentation='story_photo',title='南桥街角',question='拍下雨后街灯照在路面上的一刻')
            provider=NS(text_chat=AsyncMock(side_effect=[agenda.response(value),agenda.response(agenda.generated())]))
            with patch('data.plugins.dafeiyu_life.life.time.time',side_effect=lambda:now[0]):
                life,engine=agenda.fixture(Path(tmp),now,provider)
                art=Path(tmp)/'fake-photo.jpg';art.write_bytes(b'isolated photo artifact'*100)
                drawing=NS(generate_for_qzone=AsyncMock(return_value=str(art)));lookup=engine.context.get_registered_star
                engine.context.get_registered_star=lambda name:NS(activated=True,star_cls=drawing) if name=='astrbot_plugin_dafeiyu_draw' else lookup(name)
                await engine.plan_next_day();now[0]=agenda.stamp(7,9)
                self.assertTrue((await engine.advance())['advanced']);drawing.generate_for_qzone.assert_awaited_once()
                self.assertEqual(provider.text_chat.await_count,2)
                work=life.public_context();self.assertEqual(work['presentation'],'story_photo')
                self.assertEqual(self_state(work)['verified_completed']['content_nature'],'fictional_photo')
                self.assertEqual(life.public_context_for_day(now[0])['production'],'generated_image')
                self.assertEqual(own_experiences(life,now=now[0])[0]['payload']['activity_context'],'character_photograph_with_generated_image_artifact')

    async def test_qzone_uses_the_same_photo_view_without_a_second_generation(self):
        drawing=NS(generate_for_qzone=AsyncMock(return_value='/synthetic/photo.jpg'))
        context=NS(get_registered_star=lambda name:NS(activated=True,star_cls=drawing))
        result=await prepare_post_images(context,'角色生活里的雨后街角')
        self.assertEqual(result,['/synthetic/photo.jpg']);drawing.generate_for_qzone.assert_awaited_once()
        self.assertIn('拍下的一张照片',drawing.generate_for_qzone.await_args.args[0])


if __name__=='__main__':unittest.main()
