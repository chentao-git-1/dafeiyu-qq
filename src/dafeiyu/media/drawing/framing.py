"""Separate a character photograph's meaning from its real production and delivery."""
from pathlib import Path


PRESENTATIONS=('story_photo','artwork')
_reference_path = Path(__file__).with_name('tinglan-reference.jpg')
STORY_REFERENCE = _reference_path if _reference_path.is_file() else None
PHOTO_POLICY=('主人已经确立听澜在临湾的角色生活，自己的照片属于这套持续故事。'
    'story_photo可以自然视作听澜自己拍下的照片，以摄影者口吻聊取景和感受，不必每次解释成插画或强调生成。'
    '这不是固定台词，明确要求绘画、图表或设计稿时用artwork。'
    '照片完成和发出仍依据真实文件与发送结果；认真询问技术来源、真实相机或现实取证时如实区分生成与实拍，'
    '不把角色照片写成真实联系人的经历。')


def image_facts(presentation):
    if presentation not in PRESENTATIONS:raise ValueError('Unknown image presentation')
    return dict(presentation=presentation,production='generated_image',physical_camera_used=False,
        content_nature='fictional_photo' if presentation=='story_photo' else 'digital_art',
        narrative_viewpoint='听澜在角色生活中拍下的照片' if presentation=='story_photo' else '按要求创作的画面')


def image_prompt(description, presentation):
    image_facts(presentation)
    if presentation == 'artwork':
        return description
    from dafeiyu.life.topic_context import world_profile

    city = world_profile()['city']['name']
    if STORY_REFERENCE is None:
        return (
            f'画一幅全新的二维动画插画，呈现陈听澜在{city}生活里想记录的一刻。'
            '用手绘线条、平面色块和柔和的明暗。听澜出镜时，保留蓝色长发和蓝白鲸尾。'
            '按本次场景确定人物衣着、动作与构图。\n本次画面要求：' + description
        )
    return (
        f'画一幅全新的二维动画插画，呈现陈听澜在{city}生活里想记录的一刻。'
        '参考图用来识别听澜，并参考它的手绘线条、平面色块、柔和透光的层次。'
        '由本次题材决定主体、视角、地点和动作。她看见的东西本身也可以占满画面。'
        '听澜出镜时沿用参考中的脸型、眼形、蓝色长发和蓝白鲸尾，衣着和动作用本次场景里的。'
        '构图从头设计，参考里的姿势、家具和光照只属于原来那一幕。'
        '自拍或摄影角度也用二维笔触表现。保留手绘五官和画出来的明暗，不画成真人写真。'
        '\n本次画面要求：' + description
    )
