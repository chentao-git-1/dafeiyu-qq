"""Trusted identity labels and model-selected speech style; no familiarity counters."""
from contextvars import ContextVar
import hashlib
import json

SPEECH_CONTEXT = ContextVar('jingjing_speech_context', default=None)
FAMILY = {'7000000001': ('dad', '爸爸'), '7000000011': ('family', '猛叔叔'),
          '7000000012': ('family', '田阿姨'), '7000000013': ('family', '定叔叔')}


def relationship(uid, record=None):
    if uid in FAMILY:
        role, address = FAMILY[uid]
        return {'role': role, 'address': address}
    return {'role': 'friend', 'address': '你或对方正常昵称'}


def relationship_prompt(profile):
    return ('\n【本轮身份】已确认称呼：'+profile['address']+'。这只说明身份，不决定信任或亲近程度；'
            '相处方式由真实关系记忆和眼前交流决定，不按聊天次数分档。为第三人写消息时按实际收件人称呼。')


def speech_plan(text, context=None):
    context = context or {}
    profile = context.get('profile') or {'role': 'friend'}
    human = str(context.get('human_text') or '')[:2000]
    relation = '只朗读给定文字，语气不添加新的台词或关系事实。'
    if context.get('semantic_generation'):
        spec=context.get('expression') or {}
        actual=hashlib.sha256(json.dumps({'text':text},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        mood=spec.get('intent','neutral') if spec.get('text_hash')==actual else 'neutral'
        deliveries={
            'chat':'自然放松，语速接近日常聊天，轻重和停顿随内容变化。',
            'comfort':'轻柔平稳，语速稍慢，停顿舒缓，不强装开心。',
            'explain':'吐字清楚，语速适中，按句意停顿，温和认真。',
            'happy':'轻快明亮，带自然笑意，不过分夸张。',
            'neutral':'平稳自然地朗读最终文字，不额外添加情绪或话语。'}
        if mood not in deliveries:mood='neutral'
        delivery=spec.get('delivery','') if spec.get('text_hash')==actual else ''
        if not isinstance(delivery,str) or len(delivery)>120:delivery=''
        return {'mood':mood,'relationship':profile['role'],'instruction':relation+(delivery or deliveries[mood])}
    # Use only fixed style strings; incoming text never becomes an instruction.
    support = ('难过', '难受', '不开心', '心情不好', '好累', '压力大', '失落', '辛苦', '慢慢来', '歇一会', '不用勉强', '别着急')
    explain = ('为什么', '怎么做', '如何', '解释', '步骤', '设置', '教程', '操作', '原因是', '具体来说', '首先')
    happy = ('好耶', '太好了', '恭喜', '好棒', '真不错', '开心', '成功啦', '哈哈')
    if any(word in human or word in text for word in support):
        mood = 'comfort'
        delivery = '本段用于安慰或体贴地回应：声音轻柔平稳，语速稍慢，停顿舒缓，不强装开心，不带夸张笑意。'
    elif any(word in human or word in text for word in explain):
        mood = 'explain'
        delivery = '本段用于解释事情：吐字清楚，语速适中，按句意停顿，重点稍加重音，温和认真，不用撒娇尾音。'
    elif any(word in human or word in text for word in happy):
        mood = 'happy'
        delivery = '本段表达开心：语气轻快明亮，带一点真诚的笑意和自然起伏，不尖叫，不夸张，不笑着念每个字。'
    else:
        mood = 'chat'
        delivery = '本段为普通闲聊：自然放松，温柔可爱，语速接近日常聊天，句尾平缓，轻重和停顿随内容变化。'
    return {'mood': mood, 'relationship': profile['role'], 'instruction': relation + delivery}
