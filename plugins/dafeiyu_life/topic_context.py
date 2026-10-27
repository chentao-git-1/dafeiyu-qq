"""Bring completed work and fictional lore into relevant conversations only."""
import json
from pathlib import Path
import re

TOPIC = re.compile(r'小屋|小芽|鲸鲸日常|(?:你|鲸鲸)(?:的|最近的|自己)?[的最近]*.{0,8}(?:手[帐账]|作品|插画|近况|计划|生活)|你.{0,10}(?:在干[嘛吗]|在做什么|最近.{0,4}忙什么|今天.{0,6}(?:做了|干了|忙了)|最近.{0,6}(?:读了|看了|写了|画了)|(?:在|最近在|今天在)(?:读|看|写|画|做).{0,5}(?:什么|啥|哪))|^(?:最近|今天)?在(?:读|看|写|画|做).{0,5}(?:什么|啥|哪)')
CONTINUE = re.compile(r'^(?:嗯[，, ]*)?(?:继续(?:说|讲|写)?|接着(?:说|讲|写)?|后来呢|然后呢|下一(?:页|篇|段)|那(?:小芽|小屋|盆栽)呢|第[几\d一二三四五六七八九十]+页呢)[。！!？?\s]*$')


def relevant(text, contexts=()):
    text=str(text or '')
    if re.search(r'我(?:的|家|刚.{0,3}做的).{0,6}(?:小屋|手[帐账]|插画)',text) and not re.search(r'(?:你|鲸鲸)(?:的|自己)',text):return False
    if TOPIC.search(text):
        return True
    if not CONTINUE.fullmatch(str(text or '').strip()):
        return False
    recent=[]
    for message in list(contexts or [])[-6:]:
        if not isinstance(message,dict):continue
        value=message.get('content','')
        if isinstance(value,list):value=' '.join(str(p.get('text','')) for p in value if isinstance(p,dict) and p.get('type')=='text')
        if isinstance(value,str):recent.append(value[:1000])
    return bool(TOPIC.search('\n'.join(recent)))


def world_profile():
    value=json.loads(Path(__file__).with_name('world_profile.json').read_text(encoding='utf-8'))
    if value.get('kind')!='fictional_world':raise ValueError('World is fictional data only')
    return value
