"""Human wording at the drawing boundary; original safe detail remains available."""
import re
import time
from collections import OrderedDict

TECHNICAL = re.compile(r'HTTP|API|接口|错误码|状态码|日志|调试|技术|什么原因|具体原因|原始错误|原文|(?:为什么|怎么|为啥).{0,10}(?:失败|超时|没画|没出来)|出了什么错',re.I)


def http_status(error):
    value=getattr(error,'http_status',None)
    if isinstance(value,int) and 100<=value<=599:return value
    found=re.search(r'HTTP\s+(\d{3})',str(error))
    return int(found[1]) if found else None


def drawing_error_text(error, human_text=''):
    text=str(error)
    code=http_status(error)
    if TECHNICAL.search(str(human_text)):
        return text if code is None or re.search(r'HTTP\s+\d{3}',text) else text+'（HTTP '+str(code)+'）'
    if '图片已经生成' in text:return text
    if getattr(error,'stage',None)=='download':return '图片已经生成，但这次下载没完成，暂时还没拿到可发送的图片。'
    if code in (408,504) or '等待超时' in text:
        return '这次画图请求超时了，还没拿到图片结果。'
    if code in (401,403):return '画图这边暂时用不了，得先检查一下设置。'
    if code==429:return '这会儿画图有点忙，稍后可以再试。'
    if code:return '这次画图没完成，暂时还没拿到图片。'
    if '暂时连接不上画图服务' in text:return '这次没连上画图，暂时还没拿到图片。'
    if '接口没有返回有效的图片' in text:return '这次拿到的图片不完整，没能发出来。'
    if '画图接口没有返回图片数据' in text:return '这次没拿到图片结果。'
    if '画图接口返回的图片编码无效' in text:return '这次拿到的图片文件不完整，没能发出来。'
    if '图片接口返回内容过大' in text:return '这次图片文件太大，已经停止接收。'
    return text


class ErrorContext:
    """Short-lived numeric/category detail per real sender and session, not prompts."""
    def __init__(self,clock=time.monotonic):
        self.clock=clock;self.rows=OrderedDict()
    def remember(self,event,error,phase):
        key=(event.unified_msg_origin,str(event.get_sender_id()))
        code=http_status(error)
        message=str(error)
        phase=getattr(error,'stage',None) or phase
        category=('download_unavailable' if phase=='download' else 'timeout' if code in (408,504) or '超时' in message else 'authorization' if code in (401,403)
                  else 'busy' if code==429 else 'network' if '连接' in message else 'unconfirmed_send' if phase=='sending' else 'unconfirmed')
        self.rows[key]={'http_status':code,'category':category,'phase':phase,'at_monotonic':self.clock()}
        self.rows.move_to_end(key)
        while len(self.rows)>128:self.rows.popitem(last=False)
    def get(self,event):
        key=(event.unified_msg_origin,str(event.get_sender_id()))
        row=self.rows.get(key)
        if not row:return None
        if not 0<=self.clock()-row['at_monotonic']<=1800:
            self.rows.pop(key,None);return None
        return {k:v for k,v in row.items() if k!='at_monotonic'}
