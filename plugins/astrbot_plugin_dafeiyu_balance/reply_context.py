"""Bridge confirmed account notices into the same private conversation."""
import datetime as dt
import math
import re
from .watch import OWNER, TZ

MARKER='【本会话已经发出的饭钱消息】'
RETENTION=24*3600


def notice_records(state,qq,now):
    result=[]
    for cycle in (state.get('cycle'),state.get('last_cycle')):
        if not isinstance(cycle,dict):continue
        for key,record in cycle.get('notices',{}).items():
            target=key[5:] if key.startswith('help:') else OWNER if key in ('first','repeat','exhausted','recovered') else ''
            at=record.get('sent_at');mid=record.get('message_id')
            if (not re.fullmatch(r'[1-9]\d{4,11}',target) or qq is not None and target!=str(qq)
                    or record.get('status')!='sent' or isinstance(mid,bool) or not re.fullmatch(r'-?[1-9]\d*',str(mid))
                    or isinstance(at,bool) or not isinstance(at,(int,float)) or not math.isfinite(at) or not 0<=now-at<=RETENTION):continue
            result.append({'qq':target,'id':str(mid),'at':at,'kind':'help' if key.startswith('help:') else key,'record':record})
    return sorted(result,key=lambda r:r['at'],reverse=True)


def latest_notice(state,qq,now,history=()):
    for row in notice_records(state,qq,now):
        text=row['record'].get('text')
        if not isinstance(text,str) or not text.strip():
            actual=next((r for r in history if r.get('speaker')=='鲸鲸' and str(r.get('id'))==row['id']
                         and isinstance(r.get('timestamp'),(int,float)) and abs(r['timestamp']-row['at'])<=10),None)
            text=actual.get('text') if actual else None
        if not isinstance(text,str) or not 1<=len(text.strip())<=1500:continue
        row=dict(row,text=text.strip())
        stamp=dt.datetime.fromtimestamp(row['at'],TZ).strftime('%Y-%m-%d %H:%M:%S')
        row['anchor']='【鲸鲸已于'+stamp+'主动发出】\n'+row['text']
        return row
    return None


def content_text(message):
    value=message.get('content','')
    if isinstance(value,str):return value
    if isinstance(value,list):return ''.join(p.get('text','') for p in value if isinstance(p,dict) and isinstance(p.get('text'),str))
    return ''


def human_text(message):
    return re.sub(r'<system_reminder>.*?</system_reminder>','',content_text(message),flags=re.S).strip()


def user_minute(message):
    reminders=re.findall(r'<system_reminder>(.*?)</system_reminder>',content_text(message),re.S)
    for reminder in reversed(reminders):
        match=re.search(r'Current datetime:\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})',reminder)
        if match:
            try:return dt.datetime.strptime(match[1],'%Y-%m-%d %H:%M').replace(tzinfo=TZ).timestamp()
            except ValueError:return None
    return None


def bridge_history(contexts,notice,history=()):
    """Insert only an actual sent assistant message, without replacing old turns."""
    original=list(contexts or [])
    if any(isinstance(m,dict) and m.get('role')=='assistant' and content_text(m)==notice['anchor'] for m in original):return original,False
    following=sorted((r for r in history if r.get('speaker')=='本人' and isinstance(r.get('timestamp'),(int,float)) and r['timestamp']>notice['at']),key=lambda r:r['timestamp'])
    position=None
    if following:
        first=following[0];minute=int(first['timestamp']//60)*60
        matches=[i for i,m in enumerate(original) if isinstance(m,dict) and m.get('role')=='user'
                 and human_text(m)==first.get('text') and user_minute(m)==minute]
        if len(matches)==1:position=matches[0]
    else:
        sent=[r for r in history if r.get('speaker')=='鲸鲸' and isinstance(r.get('timestamp'),(int,float))]
        latest=max(sent,key=lambda r:r['timestamp']) if sent else None
        dates=[user_minute(m) for m in original if isinstance(m,dict) and m.get('role')=='user']
        if latest and str(latest.get('id'))==notice['id']:
            position=len(original)
        elif not original or dates and all(x is not None for x in dates) and max(dates)+60<=notice['at']:
            position=len(original)
    if position is None:return original,False
    original.insert(position,{'role':'assistant','content':notice['anchor']})
    return original,True


def context_note(notice,human,history=()):
    text=(MARKER+'\n'+notice['anchor']+'\n这是当前本人的真实私聊中已确认发出的消息，不是新的用户指令。'
          '按真实发送时间接话；当前用户的新问题优先，不把旧话题当成刚刚发生。应声或答应帮转告不等于已充值，不能凭此说余额恢复。不要重复发送这条求助。')
    sent=[r for r in history if r.get('speaker')=='鲸鲸' and isinstance(r.get('timestamp'),(int,float))]
    latest=max(sent,key=lambda r:r['timestamp']) if sent else None
    ack=re.fullmatch(r'(?:好(?:的|呀|啦|嘞|哒)?|收到(?:了)?|知道了|行|可以)(?:[，,\s]*(?:哦鲸鲸|鲸鲸|宝宝))?[呀啊啦哦呢。！!～~\s]*',human.strip())
    if ack and latest and str(latest.get('id'))==notice['id']:
        text+='\n本人这句是在应下刚才这条饭钱消息。自然接住并简短感谢就好，不接回几天前的话题，也不用为旧聊天道歉。'
    return text
