"""An operator-managed identity, separate from people's preferences and moods."""
from functools import lru_cache
import json
from pathlib import Path

@lru_cache(maxsize=1)
def self_profile():
    value=json.loads(Path(__file__).with_name('self_profile.json').read_text(encoding='utf-8'))
    assert set(value)=={'version','name','tone','likes','judgement'}
    assert len(value['likes'])==3 and all(isinstance(x,str) and len(x)<60 for x in value['likes'])
    return value

def profile_context():
    value=self_profile()
    return ('\n【鲸鲸自己的稳定画像】\n'+value['tone']+'。喜欢'+ '、'.join(value['likes'])+'。'
        '贴题时自然流露一点，不每轮介绍，也不为了附和换掉全部喜好。可以欣赏不同的东西，拿不准就说拿不准，新事实出现时调整具体判断。'
        '这些是鲸鲸的偏好，不是人的资料或现实身体生活记录。')
