"""Confirmed proactive notices remain in their own private conversation."""
import copy
import datetime as dt
import gc
import importlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,patch

STAGE=Path(sys.argv.pop(1));sys.path.insert(0,str(STAGE))
import data.plugins
data.plugins.__path__=[str(STAGE),*list(data.plugins.__path__)]
b=importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_balance.main')
c=importlib.import_module('data.plugins.astrbot_plugin_dafeiyu_balance.reply_context')
from data.plugins.astrbot_plugin_dafeiyu_balance.watch import BalanceWatch,OWNER,BOT,TZ
from astrbot.core.provider.entities import ProviderRequest
from astrbot.core.agent.message import Message
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
from astrbot.core.platform.astrbot_message import AstrBotMessage,MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.message.components import Plain

FRIEND='7000000011';OTHER='7000000012'
AT=dt.datetime(2026,10,29,18,30,9,tzinfo=TZ).timestamp()
TEXT='测试用饭钱求助，请帮我转告爸爸。'


def state(text=TEXT):
    record={'status':'sent','sent_at':AT,'message_id':321}
    if text is not None:record['text']=text
    return {'version':2,'cycle':{'id':'test-cycle','status':'low','notices':{'help:'+FRIEND:record},'targets':[{'qq':FRIEND}]},'daily_help':{},'help_days':{}}


def history():
    return [{'id':'321','speaker':'鲸鲸','text':TEXT,'timestamp':AT-0.1}]


def user(text,stamp):
    return {'role':'user','content':text+'<system_reminder>Current datetime: '+dt.datetime.fromtimestamp(stamp,TZ).strftime('%Y-%m-%d %H:%M')+' (CST)</system_reminder>'}


class Evidence(unittest.TestCase):
    def test_only_the_intended_recipient_gets_the_confirmed_notice(self):
        value=state();notice=c.latest_notice(value,FRIEND,AT+120)
        self.assertEqual(notice['text'],TEXT)
        self.assertIsNone(c.latest_notice(value,OTHER,AT+120))
        self.assertIsNone(c.latest_notice(value,OWNER,AT+120))

    def test_unconfirmed_future_expired_and_invalid_receipts_are_not_context(self):
        for status in ('unknown','failed','sending','skipped'):
            value=state();value['cycle']['notices']['help:'+FRIEND]['status']=status
            with self.subTest(status=status):self.assertIsNone(c.latest_notice(value,FRIEND,AT+120,history()))
        for mid in (None,False,0,'0','not-a-message'):
            value=state();value['cycle']['notices']['help:'+FRIEND]['message_id']=mid
            with self.subTest(mid=mid):self.assertIsNone(c.latest_notice(value,FRIEND,AT+120))
        self.assertIsNone(c.latest_notice(state(),FRIEND,AT-1))
        self.assertIsNone(c.latest_notice(state(),FRIEND,AT+24*3600+1))

    def test_legacy_text_requires_an_actual_matching_outgoing_message(self):
        self.assertIsNone(c.latest_notice(state(None),FRIEND,AT+120))
        self.assertEqual(c.latest_notice(state(None),FRIEND,AT+120,history())['text'],TEXT)
        for change in ({'id':'999'},{'speaker':'本人'},{'timestamp':AT-50}):
            rows=history();rows[0].update(change)
            with self.subTest(change=change):self.assertIsNone(c.latest_notice(state(None),FRIEND,AT+120,rows))

    def test_fresh_notice_is_an_assistant_turn_and_is_not_duplicated(self):
        notice=c.latest_notice(state(),FRIEND,AT+120)
        old=[user('旧的灯塔话题',AT-2*86400),{'role':'assistant','content':'旧的回复。'}]
        original=copy.deepcopy(old);new,added=c.bridge_history(old,notice,history())
        self.assertTrue(added);self.assertEqual(new[:-1],old);self.assertEqual(old,original)
        self.assertEqual(Message.model_validate(new[-1]).role,'assistant')
        twice,added=c.bridge_history(new,notice,history());self.assertFalse(added);self.assertEqual(twice,new)

    def test_existing_acknowledgement_is_repaired_in_time_order_without_erasing_turns(self):
        notice=c.latest_notice(state(),FRIEND,AT+180)
        rows=history()+[{'id':'322','speaker':'本人','text':'好的鲸鲸','timestamp':AT+108},
                        {'id':'323','speaker':'鲸鲸','text':'接错到灯塔去了。','timestamp':AT+113}]
        old=[user('旧的灯塔话题',AT-2*86400),{'role':'assistant','content':'旧回复'},user('好的鲸鲸',AT+108),{'role':'assistant','content':'接错到灯塔去了。'}]
        new,added=c.bridge_history(old,notice,rows)
        self.assertTrue(added);self.assertEqual(new[2]['content'],notice['anchor'])
        self.assertEqual(new[:2]+new[3:],old)

    def test_uncertain_chronology_does_not_reorder_an_existing_conversation(self):
        notice=c.latest_notice(state(),FRIEND,AT+180)
        rows=history()+[{'id':'322','speaker':'本人','text':'好的鲸鲸','timestamp':AT+108}]
        for old in ([{'role':'user','content':'好的鲸鲸'}],[user('好的鲸鲸',AT+108),user('好的鲸鲸',AT+110)]):
            new,added=c.bridge_history(old,notice,rows);self.assertFalse(added);self.assertEqual(new,old)

    def test_only_an_ack_to_the_latest_actual_notice_gets_the_reply_focus(self):
        notice=c.latest_notice(state(),FRIEND,AT+120)
        self.assertIn('本人这句是在应下',c.context_note(notice,'好的鲸鲸',history()))
        later=history()+[{'id':'333','speaker':'鲸鲸','text':'别的话题。','timestamp':AT+30}]
        self.assertNotIn('本人这句是在应下',c.context_note(notice,'好的鲸鲸',later))
        self.assertNotIn('本人这句是在应下',c.context_note(notice,'他说“好的鲸鲸”',history()))
        self.assertIn('不等于已充值',c.context_note(notice,'好的鲸鲸',history()))

    def test_watch_persists_exact_text_only_after_a_confirmed_send(self):
        from decimal import Decimal
        with tempfile.TemporaryDirectory() as folder:
            watch=BalanceWatch(Path(folder)/'state.json',clock=lambda:AT)
            actions=watch.observe(Decimal('0.60'),True,[{'qq':FRIEND,'name':'朋友'}])
            first=next(x for x in actions if x['kind']=='first');watch.reserve(first);watch.complete(first,'sent',321,text=TEXT)
            helper=next(x for x in actions if x['kind']=='help');watch.reserve(helper);watch.complete(helper,'unknown',text='尚未确认的内容')
            reopened=BalanceWatch(watch.path,clock=lambda:AT)
            self.assertEqual(reopened.state['cycle']['notices']['first']['text'],TEXT)
            self.assertNotIn('text',reopened.state['cycle']['notices']['help:'+FRIEND])


class Native(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.addCleanup(gc.collect)
        self.config={'platform_settings':{'enable_id_white_list':True,'id_whitelist':['dafeiyu-qq:FriendMessage:'+FRIEND]}}
        self.context=NS(get_config=lambda *a:self.config,register_web_api=lambda *a:None,get_platform=lambda *a:None)
        with patch.object(b,'get_astrbot_data_path',return_value=self.tmp.name):self.obj=b.DafeiyuBalance(self.context)
        self.obj.watch.state=state();self.obj.watch.clock=lambda:AT+180;self.obj.watch.save()

    def event(self,text='好的鲸鲸',uid=FRIEND,group='',platform='dafeiyu-qq'):
        msg=AstrBotMessage();msg.type=MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
        msg.self_id=BOT;msg.session_id=group or uid;msg.group_id=group;msg.message_id='322';msg.sender=MessageMember(uid,'test')
        msg.message=[Plain(text)];msg.message_str=text;msg.raw_message={'post_type':'message','self_id':BOT,'user_id':uid,'time':AT+120}
        e=AiocqhttpMessageEvent(text,msg,PlatformMetadata('aiocqhttp','test',platform),group or uid,object())
        e.set_extra('jingjing_actual_chat',history());e.send=AsyncMock();return e

    async def test_notice_is_in_model_history_before_current_ack_without_any_send(self):
        e=self.event();req=ProviderRequest(system_prompt='base',contexts=[user('旧话题',AT-2*86400)])
        await self.obj.notice_reply_context(e,req);await self.obj.notice_reply_context(e,req)
        self.assertEqual(len(req.contexts),2);self.assertIn(TEXT,req.contexts[-1]['content'])
        self.assertEqual(len(req.extra_user_content_parts),1);self.assertIn('本人这句是在应下',req.extra_user_content_parts[0].text)
        self.assertEqual(req.system_prompt,'base');e.send.assert_not_awaited()

    async def test_other_peers_groups_accounts_and_cron_cannot_receive_context(self):
        cases=[self.event(uid=OTHER),self.event(group='7000000101'),self.event(platform='other')]
        wrong=self.event();wrong.message_obj.raw_message['self_id']='999999';cases.append(wrong)
        cron=self.event();cron.set_extra('cron_job',{'id':'test'});cases.append(cron)
        for e in cases:
            req=ProviderRequest(system_prompt='base',contexts=[]);await self.obj.notice_reply_context(e,req)
            self.assertEqual(req.contexts,[]);self.assertFalse(req.extra_user_content_parts);e.send.assert_not_awaited()

    async def test_legacy_backfill_reads_matching_history_and_never_resends(self):
        self.obj.watch.state=state(None);self.obj.watch.save()
        bot=NS(call_action=AsyncMock(return_value={'messages':[{'message_id':321,'sender':{'user_id':BOT},'time':AT,'message_type':'private','message':TEXT}]}),send_private_msg=AsyncMock())
        self.context.get_platform=lambda *a:NS(bot=bot,meta=lambda:NS(id='dafeiyu-qq'))
        await self.obj.backfill_notice_text()
        persisted=BalanceWatch(self.obj.watch.path,clock=lambda:AT+180).state
        self.assertEqual(persisted['cycle']['notices']['help:'+FRIEND]['text'],TEXT)
        self.assertEqual(persisted['cycle']['notices']['help:'+FRIEND]['status'],'sent')
        bot.send_private_msg.assert_not_awaited();self.assertEqual(bot.call_action.await_args.kwargs['user_id'],int(FRIEND))


if __name__=='__main__':unittest.main()
