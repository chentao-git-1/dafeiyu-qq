"""Resolve text, supplied-image and plain-text permissions independently."""
import re

SELF_CHOICE = re.compile(r'自己(?:挑|选|定|写)|随便|随意|都行|随你|自由发挥|你来(?:挑|选|定|写)|你想.{0,12}就.{0,8}(?:写|发)|你看着')
TEXT_SOURCE = re.compile(r'(?:正文|文案|内容)(?:是|为|[：:])|(?:用|发|照着|按)(?:这|那)(?:段|条)(?:话|文字|内容|原文)?|原文|关于|围绕|以.{1,50}为(?:题|主题)|把.{1,100}(?:写|发|晒)|(?:写|发)(?:一|这|那)?(?:条|篇|段)?.{2,60}的(?:说说|动态|空间)|(?:讲讲|写写|记录).{2,60}')
IMAGE_REQUEST = re.compile(r'(?:配|用|附|带|加|放|贴|发|晒).{0,16}(?:照片|图片|这张图|那张图|刚发的图|我发的图|附图)|(?:这张|那张|我.{0,8}发的)(?:照片|图片|图).{0,8}(?:配|用|附|发|放上)')
IMAGE_ONLY = re.compile(r'(?:把|用|配|附|带|加|放|贴|发|晒).{0,12}(?:照片|图片|这张图|那张图|刚发的图|我发的图|附图).{0,8}(?:配上|放上|发出去|发空间|发说说|配图)?')
TEXT_ONLY = re.compile(r'纯文字|纯文本|(?:不|别|不用|不要|无需|不需要)\s*(?:任何)?(?:配图|附图|带图|加图|图片|画图|生成图片|图)(?:了)?')
NO_PHOTO = re.compile(r'(?:不|别|不用|不要|无需|不需要).{0,8}(?:照片|这张图|那张图|我发的图)')
SCENE = re.compile(r'(?:配图|画面|构图|场景)(?:是|为|要|[：:])|画一(?:张|只|幅)|画个')
QUOTES = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"')


def effective_request(text):
    masked=QUOTES.sub(lambda match:' '*len(match[0]),text)
    corrections=list(re.finditer(r'(?:^|[，,。；;])(?:算了|不对|改成|改为|还是)',masked))
    return text[corrections[-1].end():] if corrections else text


def decision_text(text):
    text=QUOTES.sub('',text)
    denied=r'(?:不要|别|不许|禁止|不用).{0,8}(?:公开|发|摘|搬|泄露|引用|使用|用).{0,12}(?:私聊(?:原文|内容|记录)?|隐私|聊天记录|原文)'
    text=re.sub(denied,'',text)
    return text


def original_text(event):
    if event is None:return ''
    cached=event.get_extra('jingjing_original_input',None)
    if isinstance(cached,dict) and isinstance(cached.get('text'),str):return cached['text'][:1200]
    raw=getattr(getattr(event,'message_obj',None),'raw_message',None)
    if isinstance(raw,dict) and isinstance(raw.get('message'),list):
        text=''.join(str(part.get('data',{}).get('text','')) for part in raw['message'] if isinstance(part,dict) and part.get('type')=='text')
        if text:return text[:1200]
    return str(getattr(event,'message_str','') or '')[:1200]


def positive_material_text(text):
    """A quoted example inside 'do not use ...' is not selected public prose."""
    masked=QUOTES.sub(lambda match:' '*len(match[0]),text)
    bounds=[0]+[m.end() for m in re.finditer(r'[，,。；;！!\n]|但是|不过|而是',masked)]+[len(text)]
    kept=[]
    for start,end in zip(bounds,bounds[1:]):
        clause=masked[start:end]
        denied=re.search(r'(?:不要|别|不|不许|禁止|不能|不想|不必|无需|不需要|没必要)(?:再|再去|去)?\s*(?:把|将|使用|采用|用|发|写|摘|搬|公开|分享|提|说|透露|引用)',clause)
        explicit=TEXT_SOURCE.search(clause)
        if denied and (not explicit or denied.start()<explicit.start()):continue
        kept.append(text[start:end])
    return ''.join(kept)


def material_scope(event):
    text=effective_request(original_text(event))
    no_images=bool(TEXT_ONLY.search(text))
    supplied=bool(IMAGE_REQUEST.search(text)) and not (no_images or NO_PHOTO.search(text))
    selected=positive_material_text(text)
    prose=IMAGE_ONLY.sub('',decision_text(selected))
    literal=bool(re.search(r'(?:发|写|用|正文[：:]).{0,3}[“「"『]',selected))
    if SELF_CHOICE.search(prose):
        specified=bool(re.search(r'(?:正文|文案|内容)(?:是|为|[：:])|(?:用|发|照着)(?:这|那)段|原文|关于|围绕|以.{1,50}为(?:题|主题)',prose))
    else:
        specified=bool(TEXT_SOURCE.search(prose))
    return {'autonomous_text':not (specified or literal),'use_supplied_images':supplied,'reject_supplied_images':no_images or bool(NO_PHOTO.search(text)),'text_only':no_images,
            'explicit_scene':bool(SCENE.search(text)) and not no_images,'request_text':text}


def autonomous_request(event):
    return material_scope(event)['autonomous_text']


def source_policy(autonomous):
    if autonomous:
        return ('【自主选题的公开素材范围】只从本轮明确提供的鲸鲸数字作品、标为虚构的小屋故事、已公开动态或核验去身份活动中选。'
                '未提供具体记录时写一个当下小想法，不补写吃饭、合照、一起在场等经历。'
                '不引用私聊、共享记忆、联系人活动或人格示例充当公开素材；人物、动作、数量不能靠想象补成实事。'
                '指定附图不等于可以另摘私聊写文案。故事要自然标明想象，已授权发布不再索要确认。')
    return ('【主人指定素材】按本条明确指定的主题、正文或附图写作；只使用授权范围内的材料，'
            '不补入其他私聊或联系人的活动，不把对方给的图说成自己现实参与，也不猜额外人物或数量。')
