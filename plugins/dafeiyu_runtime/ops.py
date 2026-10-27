"""Small persistent operation state and deduplicated owner-only failure notices."""
import json,os,time,uuid
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

MESSAGES={
 'QZONE_LOGIN':'QQ空间登录状态失效，需要检查登录。',
 'QZONE_READ':'QQ空间动态暂时读取失败，这轮互动已结束，后续会再检查。',
 'QZONE_WRITE':'QQ空间这次操作没有确认成功；没有自动重复发送，避免重复动态或评论。',
 'QQ_OFFLINE':'QQ连接未通过在线检查，请到管理面板查看；如需扫码由你处理。',
}

def state_path():
    root=Path(get_astrbot_plugin_data_path())/'dafeiyu_runtime'
    root.mkdir(parents=True,exist_ok=True); root.chmod(0o700)
    return root/'status.json'

def load_state():
    try:return json.loads(state_path().read_text(encoding='utf-8'))
    except (OSError,ValueError):return {}

def save_state(state):
    path=state_path(); temporary=path.with_name('status-'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.chmod(0o600); os.replace(temporary,path)

def result(category,status,**details):
    state=load_state()
    previous=state.get(category,{})
    state[category]={**previous,'status':status,'checked_at':int(time.time()),**details}
    save_state(state)

async def problem(client,category,code,owner='7000000001'):
    assert code in MESSAGES
    state=load_state(); now=time.time()
    previous=state.get(category,{})
    notify=previous.get('error_code')!=code or now-previous.get('last_notice',0)>=21600
    state[category]={**previous,'status':'error','error_code':code,'checked_at':int(now)}
    if notify:state[category]['last_notice']=now
    save_state(state)
    if notify and client:
        try:await client.send_private_msg(user_id=int(owner),message=[{'type':'text','data':{'text':MESSAGES[code]}}])
        except Exception:pass
