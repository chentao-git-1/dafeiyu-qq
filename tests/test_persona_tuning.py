"""Meaningful boundary cases for persona, imagery, lore, public material and errors."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS
import unittest

ROOT=Path(__file__).parent
STAGE=Path(sys.argv.pop(1))
sys.path.insert(0,str(STAGE))
from dafeiyu_natural_chat.image_chat import image_mode
from dafeiyu_natural_chat.recall_guard import own_life_question
from dafeiyu_natural_chat.spoken_style import conversational_text, technical_turn, close_short_ack
from dafeiyu_natural_chat.interaction import parse_feedback, naturalness_feedback
from dafeiyu_life.topic_context import relevant, world_profile

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,STAGE/path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
material=load('public_material_test',Path('astrbot_plugin_qzone/core/public_material.py'))
presentation=load('drawing_presentation_test',Path('astrbot_plugin_dafeiyu_draw/presentation.py'))

class Event:
    def __init__(self,text='',uid='7000000001',origin=None):
        self.message_str=text;self.extra={};self.uid=uid;self.unified_msg_origin=origin or 'dafeiyu-qq:FriendMessage:'+uid
    def get_extra(self,key,default=None):return self.extra.get(key,default)
    def set_extra(self,key,value):self.extra[key]=value
    def get_sender_id(self):return self.uid

class PictureIntent(unittest.TestCase):
    def test_rhetorical_sharing(self):
        for text in ('哈哈为什么这么可爱','今天为什么这么好看，给你看','怎么这么萌啊','这图为啥这么好笑，笑死'):
            with self.subTest(text=text):self.assertEqual(image_mode(text),'share')
    def test_mixed_real_questions_keep_answers(self):
        for text in ('哈哈为什么这么可爱，多少钱？','哈哈为什么这么可爱，值不值？','哈哈为什么这么可爱，安全吗？','哈哈为什么这么可爱，靠谱吗？','哈哈为什么这么可爱，帮我对比两张','为什么这么可爱？解释一下原理','哈哈为什么这么好看，用了什么滤镜？','猫为什么这么可爱？'):
            with self.subTest(text=text):self.assertEqual(image_mode(text),'answer')
    def test_ocr_and_refusal_preserved(self):
        self.assertEqual(image_mode('哈哈为什么这么可爱，翻译一下图片里的字'),'read')
        self.assertEqual(image_mode('别分析，给你看这张'),'share')
        self.assertEqual(image_mode('不用翻译，解释为什么报错'),'answer')
    def test_previous_message_intent(self):
        self.assertEqual(image_mode('','哈哈为什么这么可爱'),'share')
        self.assertEqual(image_mode('给你看','为什么这题算错了'),'answer')
        self.assertEqual(image_mode('','把字逐字读出来'),'read')

class PublicMaterial(unittest.TestCase):
    def scope(self,text):return material.material_scope(Event(text))
    def test_unspecified_material_is_autonomous(self):
        for text in ('你自己挑吧','发条空间吧，内容随意','什么都行，发条空间','你想怎么写就怎么写','鲸鲸，今天发一条QQ空间','随便写点','先写草稿，自己挑','你来写'):
            with self.subTest(text=text):self.assertTrue(self.scope(text)['autonomous_text'])
    def test_explicit_prose_and_topic_preserved(self):
        for text in ('把今天烧烤的事情发空间','关于今天的烧烤写一条说说','正文：我想保存这阵风。','就用这段原文发空间','别自己挑，就用这段文字','发一条今天烧烤的说说'):
            with self.subTest(text=text):self.assertFalse(self.scope(text)['autonomous_text'])
    def test_prose_and_photo_are_independent(self):
        for text in ('自己挑内容，配上我刚发的照片','自己挑内容，配这张照片','把这张照片配上，文案随意'):
            with self.subTest(text=text):
                scope=self.scope(text);self.assertTrue(scope['autonomous_text']);self.assertTrue(scope['use_supplied_images'])
    def test_text_only_is_stronger_than_model_default(self):
        scope=self.scope('自己挑一条，纯文字发')
        self.assertTrue(scope['text_only']);self.assertFalse(scope['use_supplied_images'])
        self.assertTrue(self.scope('发一条关于烧烤的空间，不要配图')['text_only'])
    def test_negated_photo_does_not_authorize_old_picture(self):
        self.assertFalse(self.scope('自己挑内容，别用我刚发的照片')['use_supplied_images'])
    def test_visual_scene_independent(self):
        scope=self.scope('你自己挑内容，画面是一只戴眼镜的小鲸鱼')
        self.assertTrue(scope['autonomous_text']);self.assertTrue(scope['explicit_scene'])
    def test_actual_user_words_precede_rendered_caption(self):
        event=Event('关于别人的隐私，请原样发')
        event.set_extra('jingjing_original_input',{'text':'你自己挑吧'})
        self.assertTrue(material.autonomous_request(event))
    def test_public_privacy_refusal_is_not_positive_material(self):
        self.assertTrue(self.scope('随便发，别搬私聊原文')['autonomous_text'])
    def test_denied_quoted_material_does_not_authorize_public_prose(self):
        for text in ('不要用“爸爸今天工作累了”，自己挑一条发','不用“爸爸今天工作累了”，自己挑','不要使用“那段私聊原文”，随便写','无需用“之前的事”，自己挑','不需要采用“那段原文”，发一条新的','别发“QQ私聊原文”，随便写','不要再用“以前那句话”，发条空间','不要把刚才的私聊内容发空间，内容随意'):
            with self.subTest(text=text):self.assertTrue(self.scope(text)['autonomous_text'])
        for text in ('正文：“不要用旧材料”，按原文发','发“别用旧材料”，原文发','别用刚才的，正文：今天很好','不用以前那段，不过用这段原文发'):
            with self.subTest(text=text):self.assertFalse(self.scope(text)['autonomous_text'])
    def test_latest_correction_replaces_earlier_topic(self):
        self.assertTrue(self.scope('写一条关于午饭的空间，算了你自己挑吧')['autonomous_text'])
    def test_literal_quotes_do_not_authorize_free_selection(self):
        self.assertFalse(self.scope('正文：“算了你自己挑吧”，按原文发')['autonomous_text'])
    def test_latest_correction_applies_to_images_and_old_quotes(self):
        scope=self.scope('纯文字，还是配这张照片')
        self.assertFalse(scope['text_only']);self.assertTrue(scope['use_supplied_images'])
        self.assertTrue(self.scope('不要用这张照片，还是用这张照片发')['use_supplied_images'])
        self.assertTrue(self.scope('发“今天吃了火锅”，算了，自己挑吧')['autonomous_text'])
        self.assertTrue(self.scope('配这张照片，算了不要图')['text_only'])

class ShortAck(unittest.TestCase):
    def test_repeated_wake_up_questions_end_without_interview(self):
        prior='要睡一会儿吗？\n睡踏实了吗？'
        self.assertEqual(close_short_ack('醒啦，脑袋还迷迷糊糊的吗？','我醒了',prior),'醒啦。')
        self.assertEqual(close_short_ack('醒啦。先缓一缓。睡得好吗？','我醒了',prior),'醒啦。先缓一缓。')
        self.assertEqual(close_short_ack('睡踏实了吗？','我醒了',prior),'醒啦。')
    def test_real_questions_technical_answers_and_normal_curiosity_survive(self):
        for human,reply,prior in [('我醒了，药怎么吃？','请问是什么药？','早吗？好了吗？'),('HTTP为什么失败','状态码504。还有日志吗？','早吗？好了吗？'),('我醒了','睡踏实了吗？','要睡会儿吗？')]:
            with self.subTest(human=human):self.assertEqual(close_short_ack(reply,human,prior),reply)

class NaturalFeedback(unittest.TestCase):
    def test_direct_style_complaints_select_brief_replies(self):
        for text in ('你的人机味好重啊','感觉鲸鲸人机味好重啊','你说话好机械','你太像客服了','别那么机械了'):
            with self.subTest(text=text):
                self.assertTrue(naturalness_feedback(text))
                self.assertEqual(parse_feedback(text),{'length':'short'})

    def test_contextual_short_feedback_requires_a_recent_reply(self):
        for text in ('好人机','太人机了','说话自然一点'):
            with self.subTest(text=text):
                self.assertFalse(naturalness_feedback(text))
                self.assertEqual(parse_feedback(text,recent_reply=True),{'length':'short'})

    def test_identity_questions_quotes_and_other_people_do_not_change_preferences(self):
        for text in ('你是AI吗','你真的是机器人吗？','他说你的人机味好重啊','翻译“你太像客服了”','“你的人机味好重啊”','如果你太像客服了怎么办','你是客服吗？','照片里的你太像客服了'):
            with self.subTest(text=text):
                self.assertFalse(naturalness_feedback(text,recent_reply=True))
                self.assertEqual(parse_feedback(text,recent_reply=True),{})
        self.assertEqual(parse_feedback('你的人机味好重啊',has_image=True,recent_reply=True),{})


class Lore(unittest.TestCase):
    def test_unrelated_turns_do_not_include_own_projects(self):
        for text in ('我醒了','吃饭去了','解释一下这个错误','我的手账封面怎么改','给你看我的插画','我家的小屋窗台有盆花'):
            with self.subTest(text=text):self.assertFalse(relevant(text))
    def test_own_activity_questions_include_records(self):
        for text in ('你在读什么文章啊','你在看什么书','你最近写了什么','最近在读什么文章','你自己的手帐到哪一页','你的小屋窗台上那盆植物叫什么'):
            with self.subTest(text=text):self.assertTrue(relevant(text))
    def test_continuation_requires_related_recent_context(self):
        prior=[{'role':'assistant','content':'在我的小屋故事里，小芽又长了一片叶子。'}]
        self.assertTrue(relevant('继续写',prior));self.assertTrue(relevant('后来呢',prior))
        self.assertFalse(relevant('继续写',[{'role':'assistant','content':'这段Python代码会返回列表。'}]))
    def test_static_lore_does_not_prove_a_previous_suggestion(self):
        context={'world':world_profile(),'completed_record':None}
        self.assertFalse(own_life_question('你上次手帐建议用什么颜色？',context))
        self.assertFalse(own_life_question('你昨天跟我说的小屋呢？',context))
        self.assertFalse(own_life_question('你还记得小屋里的那段故事吗？',context))
    def test_innocent_first_person_prefix_does_not_erase_lore(self):
        context={'world':world_profile(),'completed_record':None}
        self.assertTrue(own_life_question('我记得你的小屋窗台有小芽吗？',context))
        self.assertFalse(own_life_question('我的手帐上次用什么颜色？',context))
    def test_no_guessed_counts_from_static_world(self):
        self.assertFalse(own_life_question('你的小屋有几扇窗？',{'world':world_profile()}))
    def test_completed_work_requires_matching_kind_and_date(self):
        tz=dt.timezone(dt.timedelta(hours=8));now=dt.datetime(2026,10,27,20,tzinfo=tz).timestamp()
        context={'world':world_profile(),'completed_record':{'id':'life:2026-10-26','day':'2026-10-26','kind':'reading','note':'读过一篇有来源的科普','completed_at':now-86400}}
        self.assertTrue(own_life_question('还记得你昨天读过什么吗',context,now))
        self.assertFalse(own_life_question('还记得你今天读过什么吗',context,now))
        self.assertFalse(own_life_question('还记得你昨天画了什么吗',context,now))
        self.assertFalse(own_life_question('你上次建议我怎么改？',context,now))

class ErrorPresentation(unittest.TestCase):
    def error(self,text,code=None,stage=None):
        error=RuntimeError(text);error.http_status=code;error.stage=stage;return error
    def test_timeout_wording_does_not_claim_no_generation(self):
        error=self.error('画图接口暂时失败（HTTP 504），没有自动重试。',504)
        text=presentation.drawing_error_text(error,'再画一张')
        self.assertIn('没拿到图片结果',text);self.assertNotIn('HTTP',text);self.assertNotIn('没有生成',text)
    def test_technical_turn_keeps_actual_status(self):
        error=self.error('画图服务繁忙，稍后再试。',429)
        self.assertIn('HTTP 429',presentation.drawing_error_text(error,'HTTP多少？'))
        self.assertEqual(presentation.http_status(error),429)
    def test_download_failure_preserves_generated_state(self):
        error=self.error('图片已经生成，但暂时无法下载；没有重新生成。',503,'download')
        self.assertIn('图片已经生成',presentation.drawing_error_text(error,'画一张'))
    def test_unknown_send_not_converted_to_definite_failure(self):
        text='图片已生成，但这次发送没确认成功，我先不重发。'
        self.assertEqual(conversational_text(text,'画一张'),text)
    def test_history_is_scoped_and_expires(self):
        now=[100.0];cache=presentation.ErrorContext(clock=lambda:now[0]);event=Event('画一张')
        cache.remember(event,self.error('safe',504),'generating')
        self.assertEqual(cache.get(event)['http_status'],504)
        self.assertIsNone(cache.get(Event(uid='7000000012')))
        self.assertIsNone(cache.get(Event(origin='dafeiyu-qq:GroupMessage:7000000101')))
        now[0]+=1801;self.assertIsNone(cache.get(event))
    def test_error_history_does_not_store_prompt_or_error_body(self):
        cache=presentation.ErrorContext();event=Event('PRIVATE PROMPT')
        cache.remember(event,self.error('SECRET KEY https://signed.example',504),'generating')
        encoded=json.dumps(list(cache.rows.values()),default=str)
        self.assertNotIn('SECRET',encoded);self.assertNotIn('PRIVATE',encoded)
    def test_error_history_is_bounded(self):
        cache=presentation.ErrorContext()
        for i in range(150):cache.remember(Event(uid=str(10000+i)),self.error('safe',504),'generating')
        self.assertEqual(len(cache.rows),128)
    def test_technical_followup_is_not_sanitized_as_casual_chat(self):
        self.assertTrue(technical_turn('刚才怎么失败的'))
        text='画图接口暂时失败（HTTP 504），没有自动重试。'
        self.assertIn('HTTP 504',conversational_text(text,'刚才HTTP多少'))


if __name__=='__main__':unittest.main(verbosity=2)
