"""Keep short-lived, object-specific food action evidence for the bound FishWorld chat."""
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import re

from .world_observation import SHANGHAI, binding_scope

TTL = 30 * 60
ITEM = r'[\w·（）() -]{1,36}?'
QUOTED = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|\x60[^\x60]*\x60')
# Read only official event lines and labelled state fields. The list of available
# world_do commands in world_now is not evidence that any command was used.
OUTCOMES = (
    ('store', re.compile(r'(?:你|已)?把[「“]?(?P<item>'+ITEM+r')[」”]?(?:放进|放入|收进)了?冰箱')),
    ('discard', re.compile(r'(?:你|已)?把[「“]?(?P<item>'+ITEM+r')[」”]?(?:扔掉|丢掉)了?')),
    ('remove', re.compile(r'(?:你)?(?:把[「“]?(?P<item>'+ITEM+r')[」”]?从冰箱(?:取|拿)出|从冰箱(?:里)?(?:取出|拿出)了?[「“]?(?P<from_fridge>'+ITEM+r')[」”]?(?=[。！；，]|$))')),
    ('order', re.compile(r'(?:订单已确认|已下单|下单成功|订单已提交|已点餐)[：:]\s*[「“]?(?P<item>'+ITEM+r')[」”]?(?=[。，；！]|$)')),
    ('cancel', re.compile(r'(?:已取消订单|订单已取消)[：:]\s*[「“]?(?P<item>'+ITEM+r')[」”]?(?=[。，；！]|$)')),
)
COMMANDS = (('fridge-', 'store'), ('throw-', 'discard'), ('order-', 'order'))
LABELS = {'store':'放进冰箱','discard':'扔掉','remove':'取出','order':'下单','cancel':'取消订单','operation':'世界操作'}
CLAUSE = re.compile(r'[^，,。！？!?；;\n]+[，,。！？!?；;\n]*|[，,。！？!?；;\n]+')
PREFIX = re.compile(r'^(?:(?:我(?:自己)?|已经|早就|刚才|刚刚|刚|刚把|现在|这次|也|都|就|给你|确实|真的|先)\s*)*')
# Only unambiguously first-person completed reports (including dropped subjects).
FOOD = r'(?:饭|面|汤|粥|粉|饺子|包子|奶茶|咖啡|外卖|夜宵|午餐|晚餐|早餐|披萨|汉堡|便当|米线|馄饨|蛋糕|炒蛋|盒饭)'
ORDERED = re.compile(r'^(?:点好(?:了)?|点完(?:了)?|点了|订(?:好|了)|下单(?:了)?)(?:一?(?:碗|份|杯|单|盒))?(?P<item>[\w·]{0,20}?'+FOOD+r')(?:了|啦|呢)?$')
AWAITING = re.compile(r'^(?:那(?:碗|份|杯))?(?P<item>[\w·]{0,14}?'+FOOD+r')(?:还|也|都)?(?:没到|没送到|还在路上)(?:呢|啊)?$')
IN_FRIDGE = re.compile(r'^(?P<item>'+ITEM+r')(?:现在|已经|还)?在冰箱(?:里|里面)?(?:了|呢)?$')
STORED = re.compile(r'^(?:把|将)?[「“《]?(?P<item>'+ITEM+r')[」”》]?(?:已|已经|都|刚|又|再次|重新)?(?:放进|放入|收进|装进|进)了?冰箱(?:里|里面)?(?:了|啦)?$')
DISCARDED = re.compile(r'^(?:把|将)?[「“《]?(?P<item>'+ITEM+r')[」”》]?(?:已|已经|都)?(?:扔掉|丢掉|扔了|丢了)(?:了|啦)?$')
GUARDED = re.compile(r'如果|假如|假设|要是|比如|举例|台词|故事里|你说|他说|她说|我说过|想|打算|准备|(?:并)?没(?:有)?|不要|别|(?:还)?未|应该|可能|的话|(?:吗|么|吧)[啊呢]?$')
OTHER = re.compile(r'^(?:你|他|她|爸爸|妈妈|店员|顾客|别人|朋友)')
UNKNOWN = {'order':'我还没确认这单有没有点成。','store':'我还没确认这份吃的有没有放进冰箱。','discard':'我还没确认这份吃的有没有扔掉。'}

@dataclass(frozen=True)
class Receipt:
    action: str
    item: str
    status: str
    observed: float
    when: str
    command: str = ''

def _state_inventory(line):
    if not line.startswith('冰箱（') or '）：' not in line:
        return []
    values = line.partition('）：')[2].partition('。')[0]
    return re.findall(r'(?:^|[、，,])\s*([\w·]{1,32})\s+(dish-[\w-]+)\b', values)

def _claim(clause, human):
    text = clause.strip().rstrip('，,。！？!?；;\n').strip()
    if not text or clause.rstrip().endswith(('?', '？')) or OTHER.search(text):
        return None
    text = PREFIX.sub('', text)
    # A delivery still on its way presupposes an order, even with 没到.
    match = AWAITING.fullmatch(text)
    if match and not GUARDED.search(match['item']):
        return 'order', match['item']
    if GUARDED.search(text):
        return None
    for action, rule in (('order', ORDERED), ('store', STORED), ('stored', IN_FRIDGE), ('discard', DISCARDED)):
        match = rule.fullmatch(text)
        if match and not OTHER.search(match['item']):
            return action, re.sub(r'(?:又|再次|重新|再一次)$','',match['item'])
    # “放好了” can answer many everyday questions. Constrain only when the
    # current speaker explicitly asked to put a named item into the fridge.
    if text in ('放好了','收好了'):
        match = re.search(r'(?:把|将)?([\w·]{1,24}?)(?:放进|放到|放|收进)冰箱',human)
        if match:
            return 'store',re.sub(r'^(?:请|帮我|你|把|将)+','',match[1])
    if text in ('点好了','订好了','下好了','下单了'):
        item=r'(?:一|这|那)?(?:碗|份|杯|盒)?(?P<item>[\w·]{0,20}?'+FOOD+r')'
        question=re.sub(r'^(?:请|帮我|那你|你|已经|刚才)+','',human.strip())
        match=re.match(item+r'(?:点|订|下单)',question) or re.search(
            r'(?:点|订|下单)(?:好|完)?(?:了)?'+item+r'(?:[了吧吗没呢嘛？?。！!\s]|$)',question)
        if match:
            return 'order',match['item']
    if text in ('扔掉了','丢掉了','扔了','丢了'):
        match=re.search(r'(?:把|将)[「“《]?(?P<item>'+ITEM+r')[」”》]?(?:扔|丢)(?:掉|了|吧)',human)
        if match:
            return 'discard',match['item']
    return None

def _matches(action, item, recorded):
    if item == recorded:
        return True
    return bool(action == 'order' and recorded and
                (item == '外卖' or recorded.endswith(item) and re.fullmatch(FOOD,item)))

def _confirmed_line(name, line):
    """Read result prose, never instructions, conversation or menu entries."""
    if len(line)>145 or re.search(r'world_|[【#@]|https?://|私[聊信]|留言|说[：:]|可以|如果|还没|无法|失败|尚未|不能|需要|打算|将要|计划|请先',line):
        return ''
    timed=re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d\s+(.+)',line)
    if timed:
        detail=timed[1]
        if re.match(r'完成[：:]|做好了[：:]|已下单[：:]|订单已确认[：:]|把[「“]|买了|卖了|换了衣服[：:]',detail):
            return re.sub(r'^完成[：:]','',detail).split('。')[0].strip()
    elif name=='world_do' and re.match(r'^(?:好的[，,]\s*你|你(?:已经|成功))',line):
        detail=re.sub(r'^好的[，,]\s*','',line).split('。')[0].strip()
        if '了' in detail or re.search(r'已经|成功|完成',detail):
            return detail
    return ''

def _action_key(text):
    # Preserve the order of action and object; grammatical particles and a
    # first-person/second-person change are natural when reporting a receipt.
    text=re.sub(r'^(?:好的[，,]|我|你|已经|刚刚|刚才|刚|确实|成功|已)+','',text.strip())
    return re.sub(r'[了\s。！!「」“”]','',text)

def _current_place(line):
    match = re.fullmatch(r'(?:[—\s]*现在\s+\d\d?:\d\d\s*·\s*在|(?:当前)?位置[：:]\s*在?)([^，。；·\n]{1,30})', line.strip())
    return '在' + match[1].strip() if match else ''


def unsupported_world_spans(generated, sources):
    unsupported=[]
    for claim in generated.claims:
        if claim['kind']!='external_action':
            continue
        span=claim['span'].strip()
        if (not re.search(r'了|已(?:经)?|做完|办完|完成',span) or OTHER.match(span) or
                GUARDED.search(span) or QUOTED.search(span) or
                re.match(r'^(?:我)?(?:刚|已经|刚刚)?(?:查|看)(?:到|过|了)',span)):
            continue
        if _claim(span,''):
            continue  # Food actions have more precise object, time and state checks.
        rows=[sources[key]['data'] for key in claim['evidence_ids'] if key in sources]
        world=[row for row in rows if row.get('tool') in ('world_now','world_history','world_do')
               or row.get('id','').startswith('world_action:')]
        fish_ref=any(k.startswith(('tool:world_now','tool:world_history','tool:world_do','world_action:')) for k in claim['evidence_ids'])
        if (not world and not fish_ref) or any(row not in world and row.get('tool')!='send_message_to_user' for row in rows):
            continue
        key=_action_key(span)
        if not key:
            continue
        snippets=[]
        for row in world:
            if row.get('id','').startswith('world_action:') and row.get('status')=='confirmed':
                snippets.append(row['item'])
            for line in str(row.get('result') or '').splitlines():
                if row.get('tool') == 'world_now' and (place := _current_place(line)):
                    snippets.append(place)
                fact=_confirmed_line(row.get('tool'),line.strip())
                if fact: snippets.append(fact)
        if not any(key==_action_key(line) for line in snippets):
            unsupported.append(span)
    return unsupported

def ledger_for(plugin):
    ledger = getattr(plugin,'world_receipts',None)
    if ledger is None:
        ledger = plugin.world_receipts = WorldReceipts()
    return ledger


class WorldReceipts:
    def __init__(self):
        self.scope = None
        self.rows = deque(maxlen=24)

    def _bound(self, context, event, now):
        scope = binding_scope(context, event)
        if not scope:
            return False
        identity = (*scope,event.get_extra('dafeiyu_context_epoch',None))
        if self.scope != identity:
            self.scope = identity
            self.rows.clear()
        while self.rows and self.rows[0].observed < now - TTL:
            self.rows.popleft()
        return True

    def _add(self, action, item, status, now, when, command=''):
        entry = Receipt(action,item,status,now,when,command)
        if self.rows and self.rows[-1] == entry:
            return
        self.rows.append(entry)

    def pending(self, context, event, tool, args, now):
        if getattr(tool,'name',None) != 'world_do' or getattr(tool,'mcp_server_name',None) != 'fish-world':
            return
        if not self._bound(context,event,now):
            return
        event.set_extra('dafeiyu_world_action_scope', self.scope)
        command = str((args or {}).get('id') or '')[:80]
        for prefix,action in COMMANDS:
            if command.startswith(prefix):
                item = next((r.item for r in reversed(self.rows) if r.command==command and r.item),'')
                self._add(action,item,'unknown',now,'',command)
                break

    def observe(self, context, event, tool, args, text, failed, now):
        name = getattr(tool,'name','')
        if getattr(tool,'mcp_server_name',None) != 'fish-world' or name not in ('world_do','world_history','world_now'):
            return
        if not self._bound(context,event,now):
            return
        if event.get_extra('dafeiyu_world_action_scope',self.scope) != self.scope:
            return
        if failed or not text or text.lstrip().lower().startswith(('error:', '错误：')):
            if name=='world_do':
                self.pending(context,event,tool,args,now)
            return
        local = datetime.fromtimestamp(now,SHANGHAI)
        day = str((args or {}).get('date') or local.date().isoformat()) if name=='world_history' else local.date().isoformat()
        if day != local.date().isoformat():
            return
        command = str((args or {}).get('id') or '')[:80] if name=='world_do' else ''
        for line in text[:18000].splitlines():
            line=line.strip()
            if name=='world_now':
                for item,item_id in _state_inventory(line):
                    self._add('store',item,'observed',now,local.isoformat(timespec='minutes')[:16],'fridge-'+item_id)
            timed=re.fullmatch(r'((?:[01]\d|2[0-3]):[0-5]\d)\s+(.+)',line)
            if name!='world_do' and not timed:
                # Accept a labelled order confirmation as an explicit world_now field.
                if name!='world_now' or not line.startswith(('已下单：','订单已确认：','订单已提交：','订单已取消：')):
                    continue
            detail=timed[2] if timed else line
            detail=re.sub(r'^(?:(?:好的|成功)[，,]\s*|完成[：:]\s*)','',detail)
            when=(day+'T'+timed[1]) if timed else local.isoformat(timespec='minutes')[:16]
            if re.search(r'未成功|失败|未执行|没有执行|未完成|不能|无法|请先|如果|可用操作', detail):
                continue
            for action,pattern in OUTCOMES:
                match=pattern.match(detail)
                if match:
                    item=(match.groupdict().get('item') or match.groupdict().get('from_fridge') or '').strip()
                    if item:
                        self._add(action,item,'confirmed',now,when,command)
                    break
            else:
                fact=_confirmed_line(name,line)
                if fact:self._add('operation',fact,'confirmed',now,when,command)

    def facts(self, context, event, now):
        if not self._bound(context,event,now):
            return []
        facts=[]
        for r in list(self.rows)[-6:]:
            identity=sha256(repr((self.scope,r)).encode()).hexdigest()[:16]
            facts.append(dict(id='world_action:'+identity,action=LABELS[r.action],item=r.item,status=r.status,at=r.when))
        return facts

    def _supports(self, action, item, clause, now):
        # A later query can return events older than a cancellation or removal.
        # Compare occurrence time, not the order in which pages were read.
        current_state = action == 'stored'
        action = 'store' if current_state else action
        relevant = {'store': {'store','discard','remove'}, 'order': {'order','cancel'}, 'discard': {'discard'}}[action]
        fresh=bool(re.search(r'刚刚|刚才|我刚|刚把|刚点|刚放|又放|又点|重新|再次',clause))
        repeated=bool(re.search(r'又放|又点|重新|再次',clause))
        personal = not current_state and (fresh or action == 'store' and re.search(r'我|亲手|亲自',clause))
        confirmations=set()
        best = None
        for seq, row in enumerate(self.rows):
            if row.action not in relevant or not (_matches(action,item,row.item) or
                    (not row.item and row.action==action and row.status=='unknown')):
                continue
            if personal and row.status == 'observed':
                continue
            stamp = row.observed
            if row.when:
                minute = datetime.fromisoformat(row.when).replace(tzinfo=SHANGHAI).timestamp()
                # History records are minute-precision; direct responses are ordered to the second.
                stamp = minute if not row.command else row.observed
            if row.action==action and row.status=='confirmed':
                confirmations.add(row.when)
            if best is None or (stamp,seq)>best[:2]:
                best = (stamp,seq,row)
        return bool(best and best[2].action==action and best[2].status in ('confirmed','observed')
                    and (not fresh or 0<=now-best[0]<=120)
                    and (not repeated or len(confirmations)>=2))

    def wording(self, context, event, text, human, now):
        if not self._bound(context,event,now) or not isinstance(text,str):
            return text
        if re.search(r'写(?:一|个|段|篇|句)?.{0,5}(?:故事|小说|台词)|角色扮演|造句|模拟对话',human):
            return text
        masked=QUOTED.sub(lambda m:' ' * len(m[0]),text)
        pieces=[]
        conditional=False
        for part in CLAUSE.finditer(masked):
            clause=text[part.start():part.end()]
            conditional = conditional or bool(re.search(r'如果|假如|假设|举个例子|比方说',part[0]))
            claim=None if conditional else _claim(clause,human)
            if re.search(r'[。！？!?；;\n]',part[0]):
                conditional=False
            if claim is None or self._supports(*claim,clause,now):
                pieces.append(clause)
                continue
            fallback=UNKNOWN['store' if claim[0]=='stored' else claim[0]]
            if not pieces or pieces[-1]!=fallback:
                pieces.append(fallback)
        return ''.join(pieces)
