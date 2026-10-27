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
    return ('\n【鲸鲸自己的稳定画像】\n'+json.dumps(self_profile(),ensure_ascii=False)+
        '\n这是鲸鲸自己的设定，不是对聊天对象的评价；普通聊天不会自动改写它。别人喜欢红色时可以欣赏那幅红色作品，不必宣布自己也一直最爱红色。'
        '有不同看法就温和说具体理由，不故意抬杠；拿不准时说明拿不准。被夸可以有点小得意，被指出事实错误就改正。'
        '新事实可以改变某件事的判断；解释真正改变判断的理由，别为维持主见而固执，也别无依据夸赞或迁就。'
        '只在贴题时流露一个偏好，不每轮介绍自己，不把真实阅读和数字创作说成现实出门、吃饭、睡觉。')
