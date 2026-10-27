"""Small keyword catalog and persisted per-conversation recent image selection."""
from pathlib import Path
import json
import random
import re
import sqlite3
import time

ALIASES={
 '摸头':['摸摸头','摸头','乖','安慰','辛苦了'], '加油':['加油','鼓励','能做到','努力'],
 '爱心':['喜欢','爱你','想你','爱心','亲近'], '礼物':['礼物','送你','惊喜'],
 '自我安慰':['安慰','难过','委屈'], '害羞':['害羞','脸红','不好意思','夸我'],
 '睡觉':['晚安','睡觉','休息','睡吧','好梦'], '小睡':['困了','午安','小睡','打盹'],
 '疲倦':['累了','疲倦','困了'], '庆祝':['庆祝','成功','恭喜','完成了','太棒了'],
 '点赞':['点赞','真棒','厉害','好棒','赞同'], '点头':['好呀','好的','同意','可以','明白'],
 '笑':['开心','好笑','哈哈','笑了'], '期待':['期待','等你','盼着'],
 '干杯':['干杯','庆祝','碰杯'], '喝':['喝水','饮料','口渴','喝点'],
 '吃':['吃饭','好吃','零食','吃点'], '西瓜':['西瓜','吃瓜','围观'],
 '蛋糕':['蛋糕','生日','甜点'], '馋':['馋','饿了','开饭'],
 '冒泡':['冒泡','来了','在呢','出来啦'], '打招呼':['你好','早安','见面','回来啦'],
 '到达':['到啦','来了','回来啦'], '思考':['思考','想想','研究','考虑'],
 '问号':['不懂','疑惑','什么意思','问号'], '哭':['难过','委屈','哭了','伤心'],
 '唱歌':['唱歌','听歌','音乐'], '吉他':['吉他','音乐','弹琴'],
 '打字':['打字','工作','写东西'], '记录':['记下来','记录','笔记'],
 '红包':['红包','过年','恭喜发财'], '刷卡':['付款','买单','花钱'],
 '紧张':['紧张','担心'], '害怕':['害怕','吓到'],
}

def tags_for_name(filename):
    title=Path(filename).stem.split('_',1)[-1]
    tags={title}
    for key,values in ALIASES.items():
        if key in title:tags.update(values); tags.add(key)
    return sorted(tags)

class KeywordMemePicker:
    def __init__(self,root):
        self.root=Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.db=self.root/'keyword_usage.db'
        with sqlite3.connect(self.db) as connection:
            connection.execute('CREATE TABLE IF NOT EXISTS recent_images (conversation TEXT,path TEXT,last_used REAL,PRIMARY KEY(conversation,path))')
        self.db.chmod(0o600)

    @staticmethod
    def _score(tags,response,query):
        score=0
        for tag in tags:
            if len(tag)<2:continue
            if re.search(r'(?:不要|不用|别|不想|不需要).{0,3}'+re.escape(tag),query):
                score-=100
            if tag in response:score+=6
            if tag in query:score+=2
        return score

    def choose(self,event,emotion,files):
        files=[Path(path) for path in files if Path(path).is_file()]
        if not files:raise ValueError('No available meme')
        query=str(getattr(event,'message_str','') or '')[-700:]
        response=str(event.get_extra('dafeiyu_meme_response','') or '')[-700:]
        conversation=str(event.unified_msg_origin)
        catalog_path=self.root/'keyword_catalog.json'
        try:catalog=json.loads(catalog_path.read_text(encoding='utf-8')).get('images',{})
        except (OSError,ValueError):catalog={}
        def tags(path):
            relative='/'.join(path.parts[-3:])
            return catalog.get(relative,{}).get('tags') or tags_for_name(path.name)
        scored={str(path):self._score(tags(path),response,query) for path in files}
        best=max(scored.values())
        pool=[path for path in files if scored[str(path)]==best]
        now=time.time()
        with sqlite3.connect(self.db,timeout=2) as connection:
            recent=dict(connection.execute('SELECT path,last_used FROM recent_images WHERE conversation=? ORDER BY last_used DESC LIMIT 5',(conversation,)).fetchall())
            fresh=[path for path in pool if str(path) not in recent]
            if fresh:chosen=random.choice(fresh)
            else:chosen=min(pool,key=lambda path:recent.get(str(path),0))
            connection.execute('INSERT OR REPLACE INTO recent_images(conversation,path,last_used) VALUES (?,?,?)',(conversation,str(chosen),now))
            connection.execute('DELETE FROM recent_images WHERE rowid NOT IN (SELECT rowid FROM recent_images ORDER BY last_used DESC LIMIT 2048)')
        return str(chosen)
