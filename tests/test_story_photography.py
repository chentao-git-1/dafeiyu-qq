"""Role photographs retain provenance, delivery scope and continuity; mock image/text providers only."""
import asyncio
from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch
from PIL import Image as PillowImage
from mcp.types import CallToolResult, TextContent, Tool

import test_nightly_agenda as agenda
from dafeiyu.media.drawing.works import WorkHistory
from dafeiyu.media.drawing.framing import image_facts,image_prompt
from dafeiyu.media.drawing import framing
from dafeiyu.dialogue.growth import own_experiences
from dafeiyu.dialogue.self_state import self_state
from dafeiyu.life.planning import parse_choice
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.message.components import Image
from data.plugins.astrbot_plugin_dafeiyu_draw import main as adapter
from data.plugins.astrbot_plugin_dafeiyu_draw.photo_reference import FACE_SHEET, photo_input
from data.plugins.astrbot_plugin_fishworld.main import FishWorldPlugin
from data.plugins.astrbot_plugin_qzone.core.editorial import EditorialPolicy
from data.plugins.astrbot_plugin_qzone.core.post_images import prepare_post_images
f=agenda.base.fixture


class PhotoHistory(unittest.TestCase):
    def test_optional_reference_keeps_the_requested_outfit_in_both_modes(self):
        scene = '窗边的姜黄毛衣'
        with patch.object(framing, 'STORY_REFERENCE', None):
            prompt = image_prompt(scene, 'story_photo')
            self.assertIn('二维动画插画', prompt)
            self.assertIn(scene, prompt)
            self.assertNotIn('参考图', prompt)
        with patch.object(framing, 'STORY_REFERENCE', Path('fixture-reference.jpg')):
            prompt = image_prompt(scene, 'story_photo')
            self.assertIn('参考图', prompt)
            self.assertIn(scene, prompt)

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

    def test_face_reference_follows_who_is_in_this_picture(self):
        with tempfile.TemporaryDirectory() as tmp:
            sheet=Path(tmp)/FACE_SHEET
            PillowImage.new('RGB',(12,12),'blue').save(sheet)
            for brief in ('陈听澜的侧脸，神情平静，穿深绿外套','听澜和思思在窗边合影'):
                prompt,ref=photo_input(brief,'story_photo',sheet)
                self.assertEqual(ref,sheet)
                self.assertIn(brief,prompt)
                self.assertIn('同一个陈听澜',prompt)
            for brief in ('思思的自拍','听澜拍下的溪水和石头','只拍背影，不露脸',
                          '上张是听澜的自拍\n本轮完整画面描述：湖面和小船',
                          '动态文案：我的自拍\n爸爸指定的画面要求：一束玫瑰'):
                self.assertIsNone(photo_input(brief,'story_photo',sheet)[1])
            self.assertIsNone(photo_input('自拍，只露手','story_photo',sheet,shows_face=False,portrait=True)[1])
            self.assertEqual(photo_input('平静地望向窗外','story_photo',sheet,portrait=True)[1],sheet)
            self.assertEqual(photo_input('一张风力分布图','artwork',sheet),('一张风力分布图',None))
            prompt,ref=photo_input('听澜的头像，用木刻版画风格','artwork',sheet)
            self.assertEqual(ref,sheet)
            self.assertIn('木刻版画',prompt)
            self.assertNotIn('二维手绘',prompt)


class PhotoTool(unittest.IsolatedAsyncioTestCase):
    def drawing(self,root):
        draw=adapter.DafeiyuDraw.__new__(adapter.DafeiyuDraw)
        draw.config={'enabled':True,'api_key':'synthetic','cooldown_seconds':0};draw.lock=asyncio.Lock();draw.last={}
        draw.image_dir=root/'images';draw.image_dir.mkdir();draw.works=WorkHistory(draw.image_dir)
        draw._allowed=lambda event:True;draw._prune=lambda:None;draw.intent_context=adapter.IntentContext()
        draw.context=NS(get_config=lambda *args:{'platform_settings':{}})
        return draw

    async def test_private_and_qzone_select_one_reference_and_send_only_the_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); draw=self.drawing(root)
            directory=root/'references';directory.mkdir()
            sheet=directory/FACE_SHEET
            PillowImage.new('RGB',(12,12),'blue').save(sheet)
            buf=io.BytesIO()
            PillowImage.new('RGB',(18,18),'green').save(buf,format='PNG')
            rendered=buf.getvalue()
            event=f.event('想看看你')
            covers=NS(get_registered_star=lambda _:NS(activated=True,star_cls=draw))
            with patch.object(adapter,'generate',AsyncMock(return_value=(rendered,'.png',(18,18)))) as generate:
                result=await draw.draw_tool(event,'平静地朝镜头看','story_photo',shows_face=True)
                self.assertEqual(json.loads(result)['delivery'],'sent')
                self.assertEqual(generate.await_args.kwargs['reference'],sheet)
                event.send.assert_awaited_once()
                self.assertEqual(next(draw.image_dir.glob('drawing-*.png')).read_bytes(),rendered)
                paths=await prepare_post_images(covers,'桥边留念',image_prompt='转头微笑',shows_face=True)
                self.assertEqual(generate.await_args.kwargs['reference'],sheet)
                self.assertNotEqual(Path(paths[0]).read_bytes(),sheet.read_bytes())
                await prepare_post_images(covers,'我的自拍',image_prompt='只拍溪水',shows_face=False)
                self.assertIsNone(generate.await_args.kwargs['reference'])
                self.assertEqual(generate.await_count,3)

    async def test_photo_is_sent_once_and_its_meaning_survives_a_later_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            draw=self.drawing(Path(tmp));event=f.event('拍一张你的房间')
            with patch.object(adapter,'generate',AsyncMock(return_value=(b'synthetic photo','.png',(1024,1024)))) as generate:
                result=json.loads(await draw.draw_tool(event,'从窗边看向临湾的街道'))
            generate.assert_awaited_once();event.send.assert_awaited_once()
            self.assertEqual(image_prompt('从窗边看向临湾的街道','story_photo'),generate.await_args.args[1])
            self.assertIn('二维动画插画',generate.await_args.args[1])
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
            self.assertNotIn('二维动画插画',generate.await_args_list[1].args[1])
            self.assertIn('二维动画插画',generate.await_args_list[2].args[1])

    async def test_unconfirmed_send_does_not_create_a_delivered_photo_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            draw=self.drawing(Path(tmp));event=f.event('拍张照片');event.send=AsyncMock(side_effect=TimeoutError())
            with patch.object(adapter,'generate',AsyncMock(return_value=(b'synthetic photo','.png',(1024,1024)))) as generate:
                result=await draw.draw_tool(event,'临湾的街道','story_photo')
            self.assertIn('发送未确认',result);self.assertFalse(draw.conversation_works(f.OWNER,f.ORIGIN))
            self.assertIsNone(event.get_extra('dafeiyu_image_result',None));generate.assert_awaited_once()


class WorldDiaryCover(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.editorial = EditorialPolicy(root / 'posts.db')
        self.draw = PhotoTool.drawing(self, root)
        self.context = NS(get_registered_star=lambda _: NS(activated=True, star_cls=self.draw))
        self.sheet = root / 'references' / FACE_SHEET
        self.sheet.parent.mkdir()
        PillowImage.new('RGB', (12, 12), 'blue').save(self.sheet)
        output = io.BytesIO()
        PillowImage.new('RGB', (18, 18), 'green').save(output, format='PNG')
        transport = patch.object(adapter, 'generate', new=AsyncMock(
            return_value=(output.getvalue(), '.png', (18, 18))))
        self.generate = transport.start()
        self.addCleanup(transport.stop)

    async def candidate(self, text, timeline):
        await self.editorial.remember_candidate(text, {
            'diary': True, 'kind': '鱼世界生活',
            'world_life': {'date': '2026-01-16', 'source': 'world_history', 'timeline': timeline},
        })

    async def test_diary_with_several_own_actions_sends_one_scene_and_the_face_sheet(self):
        text = '上午在陶坊给白瓷杯画了一圈波纹。后来到钟楼给铜铃换了红绳。晚上回家补好了纸鸢的竹骨。'
        await self.candidate(text, [
            '09:15  完成：在陶坊给白瓷杯画了一圈波纹',
            '15:40  完成：到钟楼给铜铃换了红绳',
            '19:25  完成：回家补好了纸鸢的竹骨',
            '20:00  完成：整理邮票册',
        ])
        paths = await prepare_post_images(self.context, text, editorial=self.editorial)
        self.generate.assert_awaited_once()
        prompt = self.generate.await_args.args[1]
        self.assertEqual(sum(item in prompt for item in ('白瓷杯', '铜铃', '纸鸢')), 1, prompt)
        self.assertNotIn('邮票册', prompt)
        self.assertEqual(self.generate.await_args.kwargs['reference'], self.sheet)
        self.assertEqual(len(paths), 1)
        self.assertNotEqual(Path(paths[0]).read_bytes(), self.sheet.read_bytes())

    async def test_scenery_does_not_attach_the_photographer_face(self):
        text = '旧船坞的紫色浮标在水面摇晃。'
        self.editorial.world_cover_outfit = AsyncMock(side_effect=AssertionError('scenery requested wardrobe history'))
        for entry in ('13:05  事件：旧船坞的紫色浮标在水面摇晃', '13:05  到达：旧船坞的紫色浮标旁'):
            with self.subTest(entry=entry):
                self.generate.reset_mock()
                await self.candidate(text, [entry])
                await prepare_post_images(self.context, text, editorial=self.editorial)
                self.generate.assert_awaited_once()
                self.assertIn('紫色浮标', self.generate.await_args.args[1])
                self.assertIsNone(self.generate.await_args.kwargs['reference'])
        self.editorial.world_cover_outfit.assert_not_awaited()

    async def test_character_cover_uses_the_last_change_before_the_selected_event(self):
        text = '上午在陶坊给白瓷杯画了一圈波纹。晚上在书桌旁折纸。'
        await self.candidate(text, [
            '07:10  换了衣服：丁香色棉衫',
            '08:50  换了衣服：赭石色工装外套',
            '09:15  完成：在陶坊给白瓷杯画了一圈波纹',
            '09:15  换了衣服：橘色雨披',
            '18:40  换了衣服：松绿色绒衣',
            '19:30  完成：在书桌旁折纸',
        ])
        await prepare_post_images(self.context, text, editorial=self.editorial)
        self.generate.assert_awaited_once()
        prompt = self.generate.await_args.args[1]
        self.assertIn('赭石色工装外套', prompt)
        for other in ('丁香色棉衫', '橘色雨披', '松绿色绒衣', '折纸'):
            self.assertNotIn(other, prompt)

    async def test_character_without_verified_clothes_never_borrows_a_later_outfit(self):
        text = '我坐在陶坊给杯子画格纹。'
        selected = '09:15  完成：坐在陶坊给杯子画格纹'
        await self.candidate(text, [selected, '19:00  换了衣服：紫红色绒衣'])
        await prepare_post_images(self.context, text, editorial=self.editorial)
        self.generate.assert_awaited_once()
        prompt = self.generate.await_args.args[1]
        self.assertNotIn('紫红色绒衣', prompt)
        self.assertIn('格纹', prompt)
        self.assertIn('衣着留在取景范围外', prompt)
        self.assertEqual(self.generate.await_args.kwargs['reference'], self.sheet)

    async def test_diary_uses_older_history_page_and_skips_an_unfinished_change(self):
        text = '我坐在陶坊给杯子画格纹。'
        selected = '09:15  完成：坐在陶坊给杯子画格纹'
        await self.candidate(text, [selected, '18:40  换了衣服：松绿色绒衣'])
        pages = {
            None: [selected, '09:15  换了衣服：橘色雨披', '18:40  换了衣服：松绿色绒衣',
                   '—— 更早的：before=earlier-fixture'],
            'earlier-fixture': ['07:20  换了衣服：丁香色棉衫', '08:50  换了衣服：赭石色工装外套',
                                '09:10  开始：试穿紫色斗篷'],
        }

        async def read_page(*, tool_name, arguments, **kwargs):
            self.assertEqual(tool_name, 'world_history')
            self.assertEqual(arguments['date'], '2026-01-16')
            self.assertEqual(arguments['limit'], 30)
            lines = pages[arguments.get('before')]
            return CallToolResult(content=[TextContent(type='text', text='\n'.join(lines))])

        read = AsyncMock(side_effect=read_page)
        tool = MCPTool(Tool(name='world_history', inputSchema={'type': 'object'}),
                       NS(call_tool_with_reconnect=read), 'fish-world')
        manager = NS(get_func=lambda name: tool if name == 'world_history' else None)
        fish = FishWorldPlugin(NS(get_llm_tool_manager=lambda: manager), {'enabled': True})
        plugins = {'astrbot_plugin_fishworld': fish, 'astrbot_plugin_dafeiyu_draw': self.draw}
        self.context.get_registered_star = lambda name: NS(activated=True, star_cls=plugins[name])
        self.editorial.context = self.context
        await prepare_post_images(self.context, text, editorial=self.editorial)
        self.assertEqual([call.kwargs['arguments'].get('before') for call in read.await_args_list],
                         [None, 'earlier-fixture'])
        self.generate.assert_awaited_once()
        prompt = self.generate.await_args.args[1]
        self.assertIn('赭石色工装外套', prompt)
        for other in ('橘色雨披', '松绿色绒衣', '丁香色棉衫', '紫色斗篷'):
            self.assertNotIn(other, prompt)

    async def test_missing_face_sheet_stops_a_character_cover_before_generation(self):
        text = '我坐在陶坊里给白瓷杯画了一圈波纹。'
        await self.candidate(text, ['09:15  完成：坐在陶坊里给白瓷杯画了一圈波纹'])
        self.sheet.unlink()
        with self.assertRaises(adapter.DrawingError):
            await prepare_post_images(self.context, text, editorial=self.editorial)
        self.generate.assert_not_awaited()
        self.assertEqual(list(self.draw.image_dir.iterdir()), [])

    async def test_attached_photo_takes_precedence_over_the_diary(self):
        text = '我坐在陶坊里给白瓷杯画了一圈波纹。'
        await self.candidate(text, ['09:15  完成：坐在陶坊里给白瓷杯画了一圈波纹'])
        url = 'https://example.invalid/my-cup.jpg'
        event = NS(message_str='配我发的这张图', get_messages=lambda: [Image.fromURL(url)])
        paths = await prepare_post_images(
            self.context, text, event=event, editorial=self.editorial, require_supplied=True)
        self.assertEqual(paths, [url])
        self.generate.assert_not_awaited()


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
                self.assertEqual(drawing.generate_for_qzone.await_args.kwargs['presentation'],'story_photo')
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
        self.assertIn('角色生活里的雨后街角',drawing.generate_for_qzone.await_args.args[0])
        self.assertNotIn('二维动画插画',drawing.generate_for_qzone.await_args.args[0])
        self.assertEqual(drawing.generate_for_qzone.await_args.kwargs['presentation'],'story_photo')


if __name__=='__main__':unittest.main()
