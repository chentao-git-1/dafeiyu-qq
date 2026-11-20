"""Offline Fish World regressions with fictional wardrobe, shop and delivery fixtures."""
import asyncio
from collections import Counter
from datetime import datetime,timedelta
import json
from pathlib import Path
import sys
import tempfile
import time
from types import MethodType, ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock,patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'framework')]
# AstrBot loads third-party plugins under data.plugins in an installed system.
# Point that namespace at the checkout rather than an existing bot's plugins.
for name, directory in (('data', ROOT), ('data.plugins', ROOT / 'plugins')):
    module = sys.modules.get(name)
    if module is None:
        module = ModuleType(name)
        sys.modules[name] = module
    module.__path__ = [str(directory)]
from data.plugins.astrbot_plugin_fishworld.main import FishWorldPlugin
from data.plugins.astrbot_plugin_fishworld.core import StateStore,wake_prompt
from data.plugins.astrbot_plugin_fishworld.followthrough import Followthrough
from data.plugins.dafeiyu_dialogue import followthrough
from data.plugins.dafeiyu_dialogue.main import Dialogue,ACTIVE_KEY
from data.plugins.dafeiyu_dialogue.world_observation import SHANGHAI,WorldObservation
from data.plugins.dafeiyu_dialogue.world_activity import FLOW,EXT,proposed_share
from data.plugins.dafeiyu_dialogue.world_outing import estimate
from data.plugins.dafeiyu_dialogue.wardrobe import guard_draw,finish_draw,outfit_tools,outfit_wording
from data.plugins.dafeiyu_dialogue.provider_contract import NativeContexts
from data.plugins.astrbot_plugin_dafeiyu_draw import main as drawing
from dafeiyu.dialogue.generation import empty_learning
from astrbot.core.agent.tool import ToolSet,FunctionTool
from astrbot.core.agent.mcp_client import MCPTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.provider.entities import LLMResponse
from mcp.types import Tool,CallToolResult,TextContent

GOLD='wear-test-gold'
GREEN='wear-test-green'
NAMES={GOLD:'姜黄粗针毛衣',GREEN:'青绿薄毛衣'}
LOOKS={GOLD:'姜黄粗针毛衣配深色长裤',GREEN:'青绿色圆领薄毛衣配白色长裤'}
ROUTE='· 车行 bike-shop（老城区 · 修车）步行 35 分钟；打车 15 分钟 19 贝；推着车走 35 分钟，营业到 19:00'

def result(t,err=False):
    return CallToolResult(isError=err,content=[TextContent(type='text',text=t)])

class World:
    def __init__(self):
        self.worn=GREEN;self.changing='';self.tasks=[];self.log=[];self.report=ROUTE;self.history='没有符合的记录。';self.fails=set()
    def view(self):
        return {'time':datetime.now(SHANGHAI).isoformat(),'where':{'kind':'home','home':True,'text':'家'},
                'outfit':{'id':self.worn,'name':NAMES[self.worn],'look':LOOKS[self.worn]},
                'doing':self.tasks[0] if self.tasks else None,'side':[]}
    def now(self):
        return (datetime.now(SHANGHAI).strftime('%m-%d 周五 %H:%M')+' · 晴 11°C\n你在：家\n穿着：'
                +LOOKS[self.worn]+'\n自行车：停在家楼下（后胎没气）\n今天到现在：到过 家、示例服装店、车行；买过 1 套姜黄粗针毛衣、1 套青绿薄毛衣\n'
                +'衣柜（world_update 填 outfit 加衣服 id 换上）：\n'+'\n'.join('· '+val+' '+key+' —— 厚，休闲'+('（正穿着）' if self.worn==key else '') for key,val in NAMES.items()))
    async def call_tool_with_reconnect(self,tool_name,arguments,**kwargs):
        self.log.append((tool_name,dict(arguments)))
        if tool_name in self.fails:return result('工具暂时不可用',True)
        if tool_name=='world_now':return result(self.now())
        if tool_name=='world_find':return result(self.report)
        if tool_name=='world_history':return result(self.history)
        if tool_name=='world_update':
            if arguments.get('outfit') not in NAMES:return result('没有这套衣服',True)
            self.changing=arguments['outfit']
            return result('开始换上'+NAMES[self.changing]+'，2 分钟后换好。')
        return result('没有执行')
    def get(self,url,**kw):
        assert url=='https://api.yuyuworld.app/v1/state' and not kw['allow_redirects']
        class Read:
            status=200
            async def __aenter__(slf):return slf
            async def __aexit__(slf,*args):return False
            async def json(slf,**kw):return self.view()
        return Read()

def toolset(world):
    fs=ToolSet()
    for n,keys,desc in [('world_update',['outfit'],'换衣服：outfit 填衣柜中的衣服 id。要两分钟。'),
                         ('world_now',[],'查看鱼此刻状态和衣柜。'),('world_find',['query'],'查询目的地与营业时间。'),
                         ('world_history',['date','query'],'查带日期的历史。'),('world_do',['id'],'按世界列出的行动 id 做事。')]:
        spec=Tool(name=n,description=desc,inputSchema={'type':'object','properties':{k:{'type':'string'} for k in keys}})
        fs.add_tool(MCPTool(spec,world,'fish-world'))
    fs.add_tool(FunctionTool(name='dafeiyu_draw',description='生成并发送听澜的照片，presentation=story_photo。',
              parameters={'type':'object','properties':{'prompt':{'type':'string'},'presentation':{'type':'string'}},'required':['prompt']}))
    return fs

class Event:
    unified_msg_origin='dafeiyu-qq:FriendMessage:11'
    seq=0
    def __init__(self,human,auto=False,manual=False):
        Event.seq+=1
        self.d={ACTIVE_KEY:True,'dafeiyu_generation_contract':True,'dafeiyu_provider_json_mode':True,'human':human}
        self.message_str=human;self.message_obj=NS(message_id=('fishworld_' if auto else 'qq_')+str(Event.seq))
        self.sent=[]
        if auto:self.d.update(fishworld_wake_id='wake',fishworld_prefetch_world_now=True,fishworld_manual_wake=manual)
    def get_extra(self,k,v=None):return self.d.get(k,v)
    def set_extra(self,k,v):self.d[k]=v
    def get_sender_id(self):return '11'
    def is_private_chat(self):return True
    def get_group_id(self):return ''
    async def send(self,chain):self.sent.append(chain)

class Fish(Followthrough):
    def __init__(self,directory):
        self.path=directory/'state.json';self.store=StateStore(self.path,time.time());self.config={'enabled':True,'watch_key':'fwr_fixture'}
        self.storage_error=False;self.lock=asyncio.Lock()
        if not self.store.data['binding']:
            self.store.data.update(key_hash='fixture',binding=dict(umo=Event.unified_msg_origin,group_id='',sender_id='11',
                platform_id='dafeiyu-qq',message_type='FriendMessage',self_id='22',session_id='11'))
            self._save()
    def _save(self):self.store.save()

def app(directory):
    w=World();fish=Fish(directory);fish.session=w
    s=NS(config={},review_stats=Counter(),feature=lambda _:False,model_verification_enabled=lambda:False,
         world_observation=WorldObservation(),identity=lambda e:NS(origin=e.unified_msg_origin,actor='11',is_group=False,observed_at=time.time()),
         catalog=lambda **kw:NS(read=lambda *a:NS(items=[])),own_activity=AsyncMock(return_value=(None,None)),
         conversation_works=AsyncMock(return_value=[]),own_thoughts=AsyncMock(return_value={}),prepare_public_sharing=AsyncMock(),
         prepare_relationship_update=lambda *a:None,commit_learning=AsyncMock(),
         natural=lambda:NS(input_snapshot=lambda e:{'text':e.get_extra('human')}))
    s.context=NS(get_registered_star=lambda n:NS(activated=True,star_cls=fish if n=='astrbot_plugin_fishworld' else s))
    for n in ('photo_before_draw','photo_after_draw','verified_portrait_prompt','collect_tool_evidence','prefetch_world_now','grounded_wording','purchase_wording','parse_generation_response','finalize_generation'):
        setattr(s,n,MethodType(getattr(Dialogue,n),s))
    return s,fish,w

async def prepare(s,w,h,auto=False,manual=False,signals=()):
    e=Event(h,auto,manual);e.set_extra('fishworld_completion_signals',list(signals))
    req=NS(func_tool=toolset(w),system_prompt='',contexts=[],_dafeiyu_outfit_choice=None)
    await followthrough.begin(s,e,req,{'text':h},[],s.identity(e))
    await s.prefetch_world_now(e,req);await followthrough.attach(s,e,req)
    return e,req

async def final(s,e,reply,choice=None):
    obj={'focus':['self'],'claims':[],'learning':empty_learning(),'expression':'chat','reply':reply}
    if choice is not None:obj['fishworld']=choice
    resp=LLMResponse(role='assistant',completion_text=json.dumps(obj,ensure_ascii=False))
    await Dialogue.review_response(s,e,resp)
    assert e.get_extra('dafeiyu_generation_valid'),e.get_extra('dafeiyu_generation_failure_kind')
    return resp.completion_text

async def call(s,e,req,n,args):
    t=req.func_tool.get_tool(n)
    await Dialogue.review_tool_message(s,e,t,args)
    out=await t.call(ContextWrapper(context=None,tool_call_timeout=4),**args)
    await s.collect_tool_evidence(e,t,args,out)
    return out

def draw_fixture(s,folder):
    a=NS(context=s.context,config={'enabled':True,'api_key':'fixture','cooldown_seconds':0},image_dir=folder,
         works=NS(begin=lambda *a:1,record=lambda *a:None),_allowed=lambda e:True,_intent=lambda e:{'mode':'new'},
         intent_context=NS(remember=lambda *a:None),_prune=lambda:None,lock=asyncio.Lock(),last={},
         _error_context=lambda:NS(remember=lambda *a:None))
    a._draw=MethodType(drawing.DafeiyuDraw._draw,a)
    return a

async def regression(folder):
    s,fish,w=app(folder)
    e,r=await prepare(s,w,'换上今天新买的毛衣，拍一张给我看看')
    assert r._dafeiyu_outfit_choice==('change',(GOLD,)),r._dafeiyu_outfit_choice
    kwargs={'func_tool':r.func_tool,'contexts':NativeContexts([{'role':'user','content':'换件新衣'}],'n',False,r._dafeiyu_outfit_choice)}
    selected=outfit_tools(kwargs)
    assert selected['tool_choice']=='required' and selected['func_tool'].get_tool('world_update').parameters['properties']['outfit']['enum']==[GOLD]
    assert 'enum' not in r.func_tool.get_tool('world_update').parameters['properties']['outfit']
    bad='今天没买新衣服。我只补了后胎。青绿毛衣和加绒深灰裤子就是我今天穿的，没换新的。'
    corrected=await final(s,e,bad)
    assert '没买新衣' not in corrected and '我只补了' not in corrected and '深灰' not in corrected and '姜黄' in corrected,corrected
    e,r=await prepare(s,w,'换上今天新买的毛衣，拍一张给我看看')
    await final(s,e,'吃完饭我就换姜黄那套拍给你看。',{'photo':{'op':'defer','after':'meal','outfit':GOLD}})
    saved=fish.store.data['outfit_photo'];assert saved['target']==GOLD and saved['wait']=='meal'
    fish.store=StateStore(fish.path,time.time())
    assert fish.store.data['outfit_photo']==saved
    def signal(text):return {'kind':'done','text':text,'t':int(time.time()*1000)}
    cook,r=await prepare(s,w,'自动',True,signals=[signal('做好了：蔬菜粥')]);assert not r._dafeiyu_outfit_choice
    assert await final(s,cook,'反正还是吃完饭再拍。')==''
    w.tasks=[{'name':'吃蔬菜粥','since':datetime.now(SHANGHAI).isoformat(),'until':(datetime.now(SHANGHAI)+timedelta(minutes=14)).isoformat()}]
    during,r=await prepare(s,w,'自动',True);assert not r._dafeiyu_outfit_choice and fish.store.data['outfit_photo']['meal']=='吃蔬菜粥'
    w.tasks=[]
    e,r=await prepare(s,w,'自动',True,signals=[signal('吃蔬菜粥：吃好了')])
    assert r._dafeiyu_outfit_choice==('change',(GOLD,))
    await call(s,e,r,'world_update',{'outfit':GOLD});assert w.changing==GOLD and w.worn==GREEN
    drawer=draw_fixture(s,folder)
    sent_prompts=[]
    async def generate(key,prompt,timeout,**kw):
        sent_prompts.append(prompt);return b'local-test-image','.png',(20,20)
    with patch.object(drawing,'generate',generate):
        blocked=await drawing.DafeiyuDraw.draw_tool(drawer,e,'在家看新毛衣','story_photo')
        assert '还没核实' in blocked and not sent_prompts and not e.sent
        underway=await s.grounded_wording(e,'我刚去换上了姜黄毛衣，穿好就拍。',final=True)
        assert '还没换好' in underway and '去换上了' not in underway,underway
        w.worn=GOLD;w.changing=''
        ready,req=await prepare(s,w,'自动',True,signals=[signal('换衣服：换好了')])
        assert req._dafeiyu_outfit_choice[0]=='draw'
        out=await drawing.DafeiyuDraw.draw_tool(drawer,ready,'站在窗边看新衣，穿着青绿薄毛衣和白裤子','story_photo')
        assert json.loads(out)['delivery']=='sent' and len(ready.sent)==1 and LOOKS[GOLD] in sent_prompts[0] and '青绿薄毛衣' not in sent_prompts[0]
        assert fish.store.data['outfit_photo']['stage']=='sent'
        assert await final(s,ready,'照片发好了。',{'share':'progress','topic':'发新衣照'})==''
        again,_=await prepare(s,w,'自动',True);assert not again.get_extra(FLOW)['resume'] and len(sent_prompts)==1
    e,r=await prepare(s,w,'看看你在家的样子');worn,why=await guard_draw(s,e);assert worn and not why
    duplicate=await guard_draw(s,e);assert not duplicate[0]
    cancel,_=await prepare(s,w,'不用拍了');assert fish.store.data['outfit_photo']['stage']=='cancelled'
    assert (await guard_draw(s,e,sending=True))[1] and (await guard_draw(s,cancel))[1]
    unrelated,_=await prepare(s,w,'帮我画一张海报给我看看');assert not unrelated.get_extra(FLOW)['resume'] and not unrelated.get_extra(FLOW)['intent']
    assert await guard_draw(s,unrelated)==(None,'')
    retry,_=await prepare(s,w,'那张照片重新拍一次');assert retry.get_extra(FLOW)['resume']
    worn,why=await guard_draw(s,retry);assert worn and not why
    await finish_draw(s,retry,False)
    fish.store=StateStore(fish.path,time.time())
    assert fish.store.data['outfit_photo']['stage']=='held'
    tick,rr=await prepare(s,w,'自动',True);assert not rr._dafeiyu_outfit_choice and not tick.get_extra(FLOW)['resume']
    e,r=await prepare(s,w,'看看你在家的样子')
    worn,why=await guard_draw(s,e);assert worn and not why
    fish.store.data['key_hash']='different-fish'
    assert (await guard_draw(s,e,sending=True))[1]
    fish.store.data['key_hash']='fixture'
    await finish_draw(s,e,False)
    # Quiet is the default only for autonomous background work. Fresh progress or a personal subject can still be shared.
    f1,_=await prepare(s,w,'自动',True)
    msg='我刚到家，还要推车去补胎。'
    assert await final(s,f1,msg,{'share':'progress','topic':'到家'})==msg
    f1.set_extra('dafeiyu_confirmed_delivery',[{'kind':'reply','text':msg}]);await followthrough.delivered(s,f1)
    f2,_=await prepare(s,w,'自动',True)
    assert await final(s,f2,'我还在家，过会去补胎。',{'share':'progress','topic':'等饭'})==''
    silent,rr=await prepare(s,w,'自动',True)
    fish._active_plan_offer=None;fish.config['plan_wakes']=True;fish.store.data['cursor']='fixture-cursor'
    await FishWorldPlugin.offer_plans(fish,silent,rr)
    assert silent.get_extra('fishworld_plan_scope')
    now=datetime.now(SHANGHAI)
    plan={'op':'save','title':'去海边散步','intent':'想看看海边的晚霞',
          'start':(now+timedelta(minutes=8)).strftime('%Y-%m-%d %H:%M'),
          'until':(now+timedelta(minutes=40)).strftime('%Y-%m-%d %H:%M')}
    payload={'focus':['self'],'claims':[],'learning':empty_learning(),'expression':'chat','reply':'',
             'fishworld':{'share':'quiet'},'fishworld_plan':plan}
    response=LLMResponse(role='assistant',completion_text=json.dumps(payload,ensure_ascii=False))
    await Dialogue.review_response(s,silent,response)
    assert not response.completion_text and silent.get_extra('fishworld_plan_change')
    await FishWorldPlugin.store_plan_response(fish,silent,response)
    assert any(p['title']==plan['title'] for p in fish.store.data['world_plans'])
    new,_=await prepare(s,w,'自动',True)
    fresh='我突然特别想跟你聊聊，为什么下雪后街上会变安静。'
    assert await final(s,new,fresh,{'share':'chat','topic':'雪天的声音'})==fresh
    human,_=await prepare(s,w,'你醒着吗？');assert await final(s,human,'醒着呢。',{'share':'quiet'})=='醒着呢。'
    manual,_=await prepare(s,w,'请汇报',True,True);assert await final(s,manual,'我在家。',{'share':'quiet'})=='我在家。'
    stamp=datetime(2026,6,8,18,25,tzinfo=SHANGHAI)
    sub={'query':'补胎','name':'车行','mode':'推着车走','after_meal':True}
    view={'activities':[]}
    assert estimate(ROUTE,sub,view,stamp.timestamp())['状态']=='正好关门'
    assert estimate(ROUTE,sub,view,(stamp+timedelta(minutes=1)).timestamp())['状态']=='到时已关门'
    assert estimate(ROUTE,sub,view,(stamp-timedelta(minutes=30)).timestamp())['余量分钟']==30
    view['activities']=[{'name':'吃蔬菜粥','until':(stamp+timedelta(minutes=15)).isoformat()}]
    assert estimate(ROUTE,sub,view,stamp.timestamp())['最早到店']=='19:15'
    assert estimate(ROUTE.replace('推着车走 35 分钟','骑车 9 分钟'),sub,view,stamp.timestamp())['状态']=='待核实'
    e,r=await prepare(s,w,'今晚推车去车行补胎吧')
    with patch('data.plugins.dafeiyu_dialogue.world_outing.time.time',return_value=stamp.timestamp()):
        bad=await final(s,e,'吃完饭再拍照，今晚一定补好。')
        assert '35分钟' in bad and '19:00' in bad and '一定' not in bad,bad
        natural='赶不上，车行十九点关门。明天我早点儿出门吧。'
        assert await final(s,e,'赶不上，车行十九点关门。今晚肯定来得及办完。明天我早点儿出门吧。')==natural
    w.worn=GREEN
    e,r=await prepare(s,w,'换上今天的新衣服拍给我看看')
    w.fails.add('world_update')
    await call(s,e,r,'world_update',{'outfit':GOLD})
    assert fish.store.data['outfit_photo']['stage']=='held' and not w.changing
    missing=dict(e.get_extra(FLOW),view=None,facts={})
    assert '我还没确认换好了' in outfit_wording('我已经换上了。',missing,e)
    w.fails.clear()
    e,r=await prepare(s,w,'换上今天的新衣服拍给我看看')
    await final(s,e,'吃完就换好拍给你。',{'photo':{'op':'defer','after':'meal','outfit':GOLD}})
    row=fish.store.data['outfit_photo']
    done=datetime.now(SHANGHAI).replace(second=42,microsecond=0)-timedelta(minutes=2)
    fish.store.data['outfit_photo']=dict(row,created=done.timestamp()-600,not_before=done.timestamp()-600,
                                        meal='吃蔬菜粥',meal_until=done.timestamp())
    fish._save()
    w.tasks=[{'name':'看书','since':datetime.now(SHANGHAI).isoformat(),'until':(done+timedelta(minutes=20)).isoformat()}]
    w.history=done.strftime('%H:%M')+' 吃蔬菜粥结束了'
    after,r=await prepare(s,w,'自动',True);assert r._dafeiyu_outfit_choice==('change',(GOLD,)),after.get_extra(FLOW)['pending']
    print('QA passed: purchase/repair/wear contradictions; durable meal -> change -> verified photo; no premature or duplicate image; cancellation; optional QQ/manual; push/closing boundaries',flush=True)

async def main():
    with tempfile.TemporaryDirectory(prefix='fishworld-example-') as root:
        await regression(Path(root))


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(), 90))
