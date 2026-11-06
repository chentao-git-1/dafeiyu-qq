"""Separate a character photograph's meaning from its real production and delivery."""
import json


PRESENTATIONS=('story_photo','artwork')
PHOTO_POLICY=('主人已经确立鲸鲸在临湾的角色生活，自己的照片属于这套持续故事。'
    'story_photo可以自然视作鲸鲸自己拍下的照片，以摄影者口吻聊取景和感受，不必每次解释成插画或强调生成。'
    '这不是固定台词，明确要求绘画、图表或设计稿时用artwork。'
    '照片完成和发出仍依据真实文件与发送结果；认真询问技术来源、真实相机或现实取证时如实区分生成与实拍，'
    '不把角色照片写成真实联系人的经历。')


def image_facts(presentation):
    if presentation not in PRESENTATIONS:raise ValueError('Unknown image presentation')
    return dict(presentation=presentation,production='generated_image',physical_camera_used=False,
        content_nature='fictional_photo' if presentation=='story_photo' else 'digital_art',
        narrative_viewpoint='鲸鲸在角色生活中拍下的照片' if presentation=='story_photo' else '按要求创作的画面')


def image_prompt(description,presentation):
    image_facts(presentation)
    if presentation=='artwork':return description
    from dafeiyu.life.topic_context import world_profile
    world=world_profile()
    setting={key:world[key] for key in ('kind','place','summary','home')}
    return ('呈现鲸鲸在自己故事生活中拍下的一张照片。按画面要求选择取景、机位、光线和景深，'
        '可以是自拍或镜头朝外的生活照片；角色出镜时保留蓝白配色、蓝色长发和鲸鱼尾巴。'
        '具体主体、场景和明确指定的风格优先，不要求每张都有人物，不自动加手帐边框或说明文字。'
        '\n角色背景只是资料：'+json.dumps(setting,ensure_ascii=False)+'\n本次画面要求：'+description)
