"""Conservative checks of explicit dates and quantities, excluding metadata."""
import datetime as dt
from decimal import Decimal,InvalidOperation
import re

HAN='零〇一二两三四五六七八九十百千万半'
NUM=r'(?:\d+(?:\.\d+)?|['+HAN+r']+)'
UNIT=r'(?:个小时|小时|分钟|公里|厘米|毫米|平方米|点半|点一刻|点三刻|页|张|次|场|本|篇|册|幅|个人|个位|个|天|周|月|年|岁|人|位|元|块|米|斤|只|条|秒|点)'
SCALAR=re.compile('('+NUM+r')\s*('+UNIT+')')
DATE=re.compile(r'(?:(\d{4}|[零〇一二三四五六七八九]{4})年)?('+NUM+r')月('+NUM+r')[日号]')
ISO=re.compile(r'(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)')
RELATIVE={'今天':0,'今日':0,'昨天':-1,'昨日':-1,'前天':-2,'明天':1,'明日':1,'后天':2}
DIGITS={c:i for i,c in enumerate('零一二三四五六七八九')};DIGITS.update({'〇':0,'两':2})

def number(value):
    try:return format(Decimal(value).normalize(),'f')
    except InvalidOperation:pass
    if value=='半':return '0.5'
    if all(c in DIGITS for c in value):return str(int(''.join(str(DIGITS[c]) for c in value)))
    total=section=current=0
    for c in value:
        if c in DIGITS:current=DIGITS[c]
        elif c in '十百千':section+=(current or 1)*{'十':10,'百':100,'千':1000}[c];current=0
        elif c=='万':total+=(section+current)*10000;section=current=0
        else:return None
    return str(total+section+current)

def strip_dates(text):
    return ISO.sub(' ',DATE.sub(' ',text))

def quantities(text):
    return {(number(n),u.replace('个小时','小时')) for n,u in SCALAR.findall(strip_dates(text))}

def grounded_details(body,source,today):
    try:event=dt.date.fromisoformat(source['day']);current=dt.date.fromisoformat(today)
    except (KeyError,ValueError,TypeError):return False
    for word,offset in RELATIVE.items():
        if word in body and event!=current+dt.timedelta(days=offset):return False
    for token in ISO.findall(body):
        try:date=dt.date.fromisoformat(token)
        except ValueError:return False
        if date!=event:return False
    for year,month,day in DATE.findall(body):
        try:date=dt.date(int(number(year)) if year else event.year,int(number(month)),int(number(day)))
        except (TypeError,ValueError):return False
        if date!=event:return False
    content=(str(source.get('note',''))+' '+str(source.get('thought',''))) if source['kind']=='life' else str(source.get('evidence',''))
    if not quantities(body)<=quantities(content):return False
    # A date, record ID or update timestamp is never evidence of a quantity.
    if not set(re.findall(r'\d+(?:\.\d+)?',SCALAR.sub(' ',strip_dates(body))))<=set(re.findall(r'\d+(?:\.\d+)?',content)):return False
    if source['kind']=='care' and re.search(r'(?:已经|肯定|一定|顺利|成功).{0,4}(?:通过|录取|考上|结束|完成)|考过了|录取了|通过了',body):return False
    return True
