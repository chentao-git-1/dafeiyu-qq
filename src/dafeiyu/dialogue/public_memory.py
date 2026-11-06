"""Read confirmed own publications as durable evidence, without copying or summarizing."""
import datetime as dt
import hashlib
import json
import math

from .contracts import MemoryItem

TZ=dt.timezone(dt.timedelta(hours=8))


def timestamp(value, now):
    try:
        value=float(value)
        return value if math.isfinite(value) and 0<value<=now+5 else None
    except (ValueError,TypeError):return None


def array(raw):
    try:
        value=json.loads(raw or '[]')
        return value if isinstance(value,list) else []
    except (TypeError,ValueError):return []


def own_space_items(read, config, turn, now):
    if not config or config.get('platform')!=turn.platform:return []
    path,uin=config['path'],str(config['uin'])
    rows=read(path,"SELECT tid,text,images,create_time,comments FROM posts WHERE uin=? AND status='approved' "
        "AND tid IS NOT NULL AND tid<>'' ORDER BY create_time DESC,tid",(uin,))
    items=[];posts={};own_replies=set()
    comments_allowed=not turn.is_group and turn.actor==str(config.get('comments_owner',''))
    for row in rows:
        at=timestamp(row['create_time'],now)
        if at is None:continue
        tid=str(row['tid']);posts[tid]=row
        body=str(row['text'] or '')
        pieces=[body[i:i+1400] for i in range(0,len(body),1400)] or ['']
        for i,piece in enumerate(pieces):
            data=dict(channel='QQ空间',kind='自己发布的动态',tid=tid,text=piece,
                published_at=dt.datetime.fromtimestamp(at,TZ).isoformat(),image_count=len(array(row['images'])),
                visual_content='not_inspected',live_visibility='not_checked',part=i+1,parts=len(pieces))
            items.append(MemoryItem('qzone:post:'+tid+':'+str(i),uin,turn.platform+':Qzone:'+uin,
                'public','publication',json.dumps(data,ensure_ascii=False),at,'confirmed_own_publication',
                turn.platform+':public',occurred_at=at,status='published'))
        if not comments_allowed:continue
        for comment in array(row['comments']):
            if not isinstance(comment,dict):continue
            ct=timestamp(comment.get('create_time'),now);text=comment.get('content')
            author=str(comment.get('uin') or '')
            if not ct or not isinstance(text,str) or not text or not author.isdigit():continue
            sent=author==uin
            data=dict(channel='QQ空间评论',post_tid=tid,post_excerpt=body[:200],comment_id=comment.get('tid'),
                parent_id=comment.get('parent_tid'),speaker='self' if sent else 'commenter',author=author,
                text=text[:1400],at=dt.datetime.fromtimestamp(ct,TZ).isoformat(),status='observed_on_post')
            key=hashlib.sha256(json.dumps(data,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            items.append(MemoryItem('qzone:comment:'+key,turn.actor,turn.origin,'session','public_interaction',
                json.dumps(data,ensure_ascii=False),ct,'confirmed_space_interaction',occurred_at=ct,status='observed'))
            if sent:own_replies.add((tid,text))
    if comments_allowed and read(path,"SELECT 1 FROM sqlite_master WHERE name='qzone_auto_reply_comments'",()):
        for row in read(path,"SELECT * FROM qzone_auto_reply_comments WHERE status='sent' AND reply<>'' ORDER BY updated_at DESC",()):
            tid=str(row['post_tid']);at=timestamp(row['updated_at'],now)
            if tid not in posts or not at or (tid,row['reply']) in own_replies:continue
            data=dict(channel='QQ空间评论',post_tid=tid,post_excerpt=str(posts[tid]['text'] or '')[:200],
                in_reply_to=row['cid'],parent_id=row['parent_id'],recipient=str(row['author']),speaker='self',
                text=str(row['reply'])[:1400],sent_at=dt.datetime.fromtimestamp(at,TZ).isoformat(),status='sent')
            items.append(MemoryItem('qzone:reply:'+str(row['key']),turn.actor,turn.origin,'session','public_interaction',
                json.dumps(data,ensure_ascii=False),at,'confirmed_space_interaction',occurred_at=at,status='sent'))
    return items
