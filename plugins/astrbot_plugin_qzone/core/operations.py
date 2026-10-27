"""Read visible friend spaces, skip empty/private spaces, and claim SNS writes once."""
import asyncio
import importlib
import sqlite3
import time
_ops=importlib.import_module(('data.plugins.' if __package__.startswith('data.plugins.') else '')+'dafeiyu_runtime.ops')
problem,result=_ops.problem,_ops.result

def classify(error):
    message=str(error).lower()
    if any(word in message for word in ('登录','cookie','skey','expired')):return 'QZONE_LOGIN'
    if any(word in message for word in ('无权限','私密','forbidden','不可见')):return 'private'
    if any(word in message for word in ('暂无可见','查询结果为空','没有动态')):return 'empty'
    return 'QZONE_READ'

def claim(config,post,action):
    with sqlite3.connect(config.db_path,timeout=5) as connection:
        connection.execute('''CREATE TABLE IF NOT EXISTS social_actions (
            uin INTEGER,tid TEXT,action TEXT,status TEXT,updated_at INTEGER,
            PRIMARY KEY(uin,tid,action))''')
        cursor=connection.execute("INSERT OR IGNORE INTO social_actions VALUES (?,?,?,'started',?)",
            (post.uin,post.tid,action,int(time.time())))
        return cursor.rowcount==1

def finish(config,post,action,status):
    with sqlite3.connect(config.db_path,timeout=5) as connection:
        connection.execute('UPDATE social_actions SET status=?,updated_at=? WHERE uin=? AND tid=? AND action=?',
            (status,int(time.time()),post.uin,post.tid,action))

async def run_interactions(config,service,sender):
    client=config.client
    if client is None:
        await problem(None,'qzone_interactions','QZONE_READ');return
    try:
        friends=await asyncio.wait_for(client.get_friend_list(),20)
        if isinstance(friends,dict):friends=friends.get('friends') or friends.get('data') or []
        own=await service.session.get_uin()
    except Exception as error:
        code=classify(error)
        await problem(client,'qzone_interactions',code if code.startswith('QZONE_') else 'QZONE_READ');return
    candidates={}; checked=0; private=0; failures=[]
    for friend in friends[:30]:
        uid=str(friend.get('user_id') or friend.get('uin') or '')
        if not uid.isdigit() or uid==str(own):continue
        checked+=1
        try:
            posts=await service.query_feeds(target_id=uid,pos=0,num=5,no_self=True,no_commented=True)
            for post in posts:
                if post.tid and post.uin!=own and post.create_time>=int(time.time())-86400:
                    candidates[(post.uin,post.tid)]=post
        except Exception as error:
            code=classify(error)
            if code=='private':private+=1
            elif code!='empty':failures.append(code)
    posts=sorted(candidates.values(),key=lambda post:post.create_time,reverse=True)[:3]
    commented=0; liked=0
    for post in posts:
        for action in ('like','comment'):
            if action=='like' and not config.trigger.like_when_comment:continue
            if not await asyncio.to_thread(claim,config,post,action):continue
            try:
                if action=='like':await service.like_posts(post); liked+=1
                else:await service.comment_posts(post); commented+=1
                await asyncio.to_thread(finish,config,post,action,'succeeded')
            except Exception:
                await asyncio.to_thread(finish,config,post,action,'unknown')
                failures.append('QZONE_WRITE')
    details={'friends_checked':checked,'private_spaces_skipped':private,'fresh_posts':len(candidates),
        'commented':commented,'liked':liked}
    if failures:
        await problem(client,'qzone_interactions','QZONE_LOGIN' if 'QZONE_LOGIN' in failures else failures[0])
        result('qzone_interaction_details','partial_failure',**details)
    else:result('qzone_interactions','completed' if posts else 'no_updates',error_code=None,**details)
    return details
