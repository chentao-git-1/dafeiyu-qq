"""An explicit question about shared history needs evidence beyond persona examples."""
import hashlib
import re
import time
from .exchanges import terms
from .retrieval import FILLER, concepts, time_window


def asks_for_recall(text):
    text = re.sub(r'```.*?```|`[^`]*`|“[^”]*”|「[^」]*」|"[^"]*"', '', str(text), flags=re.S)
    if re.search(r'帮我写|写个|续写|编一个|扮演|假如|假设|小说里|翻译|台词', text):
        return False
    if re.search(r'提醒我|叫我|喊我|定时|帮我设置', text) and not re.search(r'上次|当时|之前', text):
        return False
    if not re.search(r'我|咱们|上次|那次|之前|以前|当时|刚才|昨天|前天|上周|那(?:个|张|条|段|句|件|本|首|天)',text):
        return False  # Remembering a public fact is not a claim about shared personal history.
    return bool(re.search(r'还记得|记不记得|回忆一下|记得.{0,60}(?:吗|[?？])', text)
                or re.search(r'上次|那次|之前|以前|当时|刚才|昨天|前天|上周', text)
                and re.search(r'聊|说|建议|提过|问过', text)
                and re.search(r'[?？]|什么|怎么|哪|是否|有没有', text))


def related(query, text):
    keys = terms(FILLER.sub('', query))
    return bool(concepts(query) & concepts(text) or len(keys & terms(text)) >= 2)


def history_evidence(query, history, now=None):
    window = time_window(query, time.time() if now is None else now)
    rows = [r for r in history if isinstance(r.get('text'), str) and
            (window is None or isinstance(r.get('timestamp'), (int,float)) and window[0] <= r['timestamp'] < window[1])]
    matched = [r for r in rows if related(query, r['text'])]
    if not matched and re.search(r'(?:刚才|上一句|前一句|昨天|前天).{0,12}(?:说|聊).{0,6}(?:什么|啥)', query):
        matched = rows[-3:]
    return [{k: r[k] for k in ('speaker','text','timestamp') if k in r} for r in matched[-3:]]


def known_current_fact(query, names, uid):
    own = r'你(?:自己|平时|最)?(?:喜欢|偏好)|你(?:叫什么|叫啥|是谁)|你的(?:名字|性格|喜好|爸爸|家人)|你(?:爸爸|爸)是谁'
    return bool(re.search(own, query) or str(uid) in names and re.search(r'我是谁|我叫什么|叫我什么|怎么称呼我', query))


def long_term_evidence(query, prompt):
    # Preserve the existing memory plugin's per-user automatic recall path.
    return any(related(query, pack) for pack in re.findall(r'<long_term_memory>(.*?)</long_term_memory>', prompt, re.S))


def own_life_question(query, context, now=None):
    if not isinstance(context,dict):return False
    query=re.sub(r'^(?:我记得|我想知道|我想问问|我想问)[，,\s]*','',query)
    if re.search(r'我|我们|咱们|建议|答应|说过|聊过',query):return False
    record=context.get('completed_record')
    if isinstance(record,dict) and record.get('id') and record.get('day') and isinstance(record.get('completed_at'),(int,float)):
        current=time.time() if now is None else now
        window=time_window('还记得'+query,current)
        in_range=window is None or window[0]<=record['completed_at']<window[1]
        recent=not re.search(r'刚才|刚刚',query) or 0<=current-record['completed_at']<=300
        own=bool(re.search(r'你|鲸鲸|小屋|小芽',query))
        activity={'reading':r'读|看.*(?:书|文章|科普)','writing':r'写|手[帐账]','drawing':r'画|插画'}.get(record.get('kind'),r'(?!)')
        if own and in_range and recent and record.get('note') and re.search(activity,query):return True
    if re.search(r'那次|上次|昨天|以前|当时|前天|今天|刚才|刚刚',query):return False
    world=context.get('world')
    if not isinstance(world,dict) or world.get('kind')!='fictional_world':return False
    return bool(re.search(r'小屋.{0,12}(?:叫什么|名字|有什么|窗台|在哪|是什么)|(?:植物|盆栽|小芽).{0,8}(?:名字|叫什|叫啥|在哪|是什么)|小屋.{0,8}(?:有|叫).{0,8}小芽',query))


def uncertain_reply(uid, mid):
    choices = ('那句我这次没对上，你提醒我一下？', '具体那句我记不准了，你给我接一句。', '我怕把那次记岔了，你提醒我一下是哪一段？')
    index = int(hashlib.sha256((str(uid)+':'+str(mid)).encode()).hexdigest()[:8],16) % len(choices)
    return choices[index]
