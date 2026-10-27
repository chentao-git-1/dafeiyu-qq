"""Small, conservative query expansion; no model calls or new indexes."""
import datetime as dt
import math
import re
from collections import Counter

TZ = dt.timezone(dt.timedelta(hours=8))
# Only closely related expressions. Broad words such as 工作 must not imply 面试.
CONCEPTS = {
    '面试': ('面试', '应聘面谈', '求职面谈', '应聘'),
    '考试': ('考试', '测验', '期中考', '期末考'),
    '手帐': ('手帐', '手账'),
    '插画': ('插画', '插图'),
    '失眠': ('失眠', '睡不着'),
    '冷知识': ('冷知识', '小知识'),
    '小屋': ('小屋', '小房子'),
}
RECALL = re.compile(r'记得|回忆|聊过|说过|提过|问过|上次|那次|当时|(?:昨天|前天|上周|上星期).{0,8}(?:聊|说|提|问)')
FILLER = re.compile(r'你还记得|还记得|记不记得|记得|我们之前|我们上次|我们|我跟你|我和你|跟你|之前|上次|那次|当时|聊过|说过|提过|问过|聊了什么|说了什么|聊了啥|说了啥|说什么|是什么|有没有|那件事|这件事|的事情|的事|吗|呢|呀|啊')
NUM = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}


def concepts(text):
    return {key for key, words in CONCEPTS.items() if any(word in text for word in words)}


def time_window(query, now):
    """Filter archived conversation timestamps only on explicit recall requests."""
    if not RECALL.search(query):
        return None
    today = dt.datetime.fromtimestamp(now, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end = None
    day = re.search(r'大前天|前天|昨天|今天', query)
    if day:
        offset = {'今天': 0, '昨天': 1, '前天': 2, '大前天': 3}[day[0]]
        start = today - dt.timedelta(days=offset)
        end = start + dt.timedelta(days=1)
    elif re.search(r'上周|上星期|这周|本周|这星期', query):
        start = today - dt.timedelta(days=today.weekday())
        if re.search(r'上周|上星期', query):
            start -= dt.timedelta(days=7)
        end = start + dt.timedelta(days=7)
    else:
        days = re.search(r'(最近|近|过去)(\d{1,2}|[一二两三四五六七八九十])天', query)
        ago = re.search(r'(\d{1,2}|[一二两三四五六七八九十])天前', query)
        date = re.search(r'(?:(20\d{2})[年/-])?(\d{1,2})[月/-](\d{1,2})(?:[日号]|(?!\d))', query)
        if days or ago:
            raw = (days or ago)[2 if days else 1]
            count = int(raw) if raw.isdigit() else NUM[raw]
            if not 1 <= count <= 30:
                return None
            start = today - dt.timedelta(days=count - 1 if days else count)
            end = today + dt.timedelta(days=1) if days else start + dt.timedelta(days=1)
        elif date:
            try:
                start = today.replace(year=int(date[1] or today.year), month=int(date[2]), day=int(date[3]))
                end = start + dt.timedelta(days=1)
            except ValueError:
                return None
    return (start.timestamp(), end.timestamp()) if start is not None else None


def rank(rows, query, now, terms, special, exclude=(), diagnostics=None):
    query = str(query)[:1200].lower()
    window = time_window(query, now)
    clean = FILLER.sub('', query)
    if window:
        clean = re.sub(r'大前天|前天|昨天|今天|上周|上星期|这周|本周|这星期|(?:最近|近|过去)?[\d一二两三四五六七八九十]{1,2}天前?|(?:(?:20\d{2})[年/-])?\d{1,2}[月/-]\d{1,2}[日号]?', '', clean)
    keys = terms(clean)
    intent = concepts(query)
    excluded = {str(x).strip() for x in exclude}
    candidates = [dict(r) for r in rows if r['reply'].strip() not in excluded
                  and (window is None or window[0] <= r['at'] < window[1])]
    docs = [(r, terms(r['human']), terms(r['reply']), concepts(r['human'] + ' ' + r['reply'])) for r in candidates]
    df = Counter(key for _, h, a, _ in docs for key in h | a)
    ranked = []
    synonym_matches = 0
    temporal_only = window is not None and not keys and not intent
    for row, human, reply, tags in docs:
        common = keys & (human | reply)
        matched = intent & tags
        if not (temporal_only or matched or len(common) >= 2 or any(t in common for t in special)
                or (RECALL.search(query) and common and len(keys) == 1)):
            continue
        synonym_matches += bool(matched)
        score = sum(math.log1p((len(docs) + 1) / (df[k] + .5)) * (1.5 if k in human else .65) for k in common)
        score += 4 * len(matched)
        if len(clean.strip('，。？！? ')) >= 3 and clean.strip('，。？！? ') in row['human'].lower():
            score += 4
        # Relevance leads; recency only breaks ties. A newer unrelated reply must not win.
        ranked.append((score, row['at'], row))
    ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
    result, seen = [], set()
    for _, _, row in ranked:
        signature = re.sub(r'[\W_]+', '', row['human'] + '\n' + row['reply']).lower()
        if signature in seen:
            continue
        seen.add(signature)
        result.append({'at': row['at'], 'human': row['human'][:450], 'reply': row['reply'][:450]})
        if len(result) == 3:
            break
    if diagnostics is not None:
        diagnostics.update(candidates=len(rows), in_time_range=len(candidates), selected=len(result),
                           synonym_matches=synonym_matches, time_filter=window is not None)
    return result
