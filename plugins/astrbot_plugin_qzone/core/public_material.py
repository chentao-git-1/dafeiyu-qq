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


LAYOUT = re.compile(
    r'(?P<label>配图|构图|画面|图片|照片)?(?:请|就)?(?:要)?(?:用|做成|做|画)?(?:一张)?'
    r'(?:(?P<collage>拼成一(?:张|幅)(?:图|照片)?|拼在一起|(?:[两三四234]格)?拼图)'
    r'|(?P<separate>分开(?:来)?(?:发(?:送)?|画|配图|出图)|分图发|分(?:成|为)(?:两|几|多|[23])张(?:图片|照片|图)?'
    r'|多张(?:图片|照片|单图|分图|图)))'
)
LAYOUT_ATTACHMENT = re.compile(r'[这那](?:些|[一两三几23]?张)(?:图|照片)|我(?:刚)?(?:发|传)(?:来|过)?的(?:图|照片)')


def layout_request(text):
    """Separate the owner's layout directions from captions and image subjects."""
    masked = QUOTES.sub(lambda m: ' ' * len(m[0]), text)
    layout = 'auto'
    material = list(text)
    for match in LAYOUT.finditer(masked):
        start, end = match.span()
        if match[0] == '拼图' and (re.search(r'\w$', masked[:start].rstrip()) or re.match(r'\w', masked[end:].lstrip())):
            continue
        if not re.search(r'(?:不要|不用|不想|不必|无需|别|不)(?:再)?$', masked[:start].rstrip()):
            layout = 'collage' if match['collage'] else 'separate'
        material[start:end] = ' ' * (end - start)
    return layout, ''.join(material)


CAMERA = re.compile(r'(?:从|以|用|换成|改用)?(?:正面|侧后方|侧后|侧面|背面|背后|侧脸|侧身|上方|低机位|高机位|'
                    r'侧拍|侧视|俯拍|仰拍|平视|俯视|仰视|特写|近景|中景|远景|广角|第一人称|'
                    r'拉近|推近|拉远|推远)'
                    r'(?:的)?(?:角度|方向|机位|视角|镜头)?')
COSTUME = re.compile(r'(?:把|将)?(?:她的|听澜的)?(?:衣服|服装|衣着|穿搭|造型)(?:改为|改成|换成|换为)|'
                     r'(?:让她|给她|请她|让听澜|给听澜)(?:改穿|换上|穿上|穿)|(?:改穿|换上)|'
                     r'(?:换成|改成)(?=[^，,。；;]{0,16}(?:衣|裙|衫|外套|裤|夹克|背心|制服|西装|睡袍))')


def requested_image_direction(text):
    """Extract instructions for this image from the owner's actual message."""
    def visual_quote(match):
        label = r'(?:画面|配图|镜头|构图|场景)(?:要求)?(?:是|为|要|用|按|[：:])\s*$'
        return match[0][1:-1] if re.search(label, text[:match.start()]) else ''

    material = re.sub(QUOTES.pattern + r"|‘[^’]*’", visual_quote, text)
    while True:
        cleaned = re.sub(r'（[^（）]*(?:）|$)|\([^()]*(?:\)|$)', '', material)
        if cleaned == material:
            break
        material = cleaned
    chosen = []
    for clause in re.split(r'[，,。；;！!\n]', material):
        if re.search(r'私聊|私信|聊天记录|私人信息|隐私|不公开|不得公开|'
                     r'(?:请勿|不要|别|不许|禁止).{0,55}(?:公开|画进|写进|透露|放进|提起)', clause):
            continue
        marker = SCENE.search(clause)
        camera = CAMERA.search(clause)
        wardrobe = COSTUME.search(clause)
        if camera and (chosen or re.search(r'画|拍|取景|镜头|角度|机位|视角', clause)):
            start = min(camera.start(), marker.start()) if marker else camera.start()
            # Keep the polarity when a direction says “别从正面拍”.
            denied = re.search(r'(?:不要|不用|避免|别|不)\s*$', clause[:start])
            chosen.append(clause[denied.start() if denied else start:].strip())
        elif marker and clause[marker.end():].strip():
            chosen.append(clause[marker.start():].strip())
        elif wardrobe and (chosen or re.search(r'画|拍|配图', clause)):
            denied = re.search(r'(?:不要|不用|避免|别|不)\s*$', clause[:wardrobe.start()])
            chosen.append(clause[denied.start() if denied else wardrobe.start():].strip())
    return '，'.join(chosen)[:300]


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
    layout, material = layout_request(text)
    scope = _material_scope(material)
    direction = requested_image_direction(material) if not scope['text_only'] else ''
    scope.update(image_layout=layout, request_text=text, image_direction=direction,
                 explicit_scene=bool(direction))
    if layout != 'auto' and not scope['reject_supplied_images']:
        request = QUOTES.sub('', text)
        attachment = LAYOUT_ATTACHMENT.search(request)
        if attachment:
            denied = bool(re.search(r'(?:别|不要|不用|无需|不需要|不能|不想|不许|禁止)[^，,。；;\n]{0,12}$', request[:attachment.start()]))
            scope.update(use_supplied_images=not denied, reject_supplied_images=denied)
    return scope


def _material_scope(text):
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
        return ('【自主选题的公开素材范围】可以自由选题、表达观点或创作明确的虚构内容，所给公开资料可用也可不用。'
                '陈述真实往事或已经完成的活动时依据提供的来源，不把想象写成已发生的事实。'
                '不引用私聊、共享记忆、联系人活动或人格示例充当公开素材；人物、动作、数量不能靠想象补成实事。'
                '指定附图不等于可以另摘私聊写文案。故事要自然标明想象，已授权发布不再索要确认。')
    return ('【主人指定素材】按本条明确指定的主题、正文或附图写作；只使用授权范围内的材料，'
            '不补入其他私聊或联系人的活动，不把对方给的图说成自己现实参与，也不猜额外人物或数量。')
