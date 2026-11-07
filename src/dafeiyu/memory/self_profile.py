"""Identity and optional initial interests, separate from people's private preferences."""
from functools import lru_cache
import json
from pathlib import Path

@lru_cache(maxsize=1)
def self_profile():
    value=json.loads(Path(__file__).with_name('self_profile.json').read_text(encoding='utf-8'))
    assert set(value)=={'version','name','tone','likes','judgement','interest_mode'}
    assert isinstance(value['likes'],list) and all(isinstance(x,str) and len(x)<60 for x in value['likes'])
    assert value['interest_mode']=='initial_optional'
    return value


def identity_profile():
    value=self_profile()
    return {key:value[key] for key in ('version','name','tone','judgement')}

def profile_context():
    value=self_profile()
    return ('\n【鲸鲸自己的画像】\n'+value['tone']+'。'+value['judgement']+'。'
        '初始兴趣可按需要查询，过去喜欢什么不规定此刻要聊什么；这些不是人的资料或现实身体生活记录。')
