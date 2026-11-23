"""Select the private face reference only for an image depicting Tinglan."""
import re
from pathlib import Path

from dafeiyu.life.topic_context import world_profile
from dafeiyu.media.drawing import framing


FACE_SHEET = "tinglan-face.jpg"
OFF_CAMERA = re.compile(r"不露脸|不露出脸|只(?:拍|有|见)背影|纯风景|空镜头|听澜(?:本人)?不(?:出|入)镜|"
                        r"画面(?:中|里)?(?:无人|没有人物)|(?:不拍|不要|不需要)(?:自拍|人像|人物)")
ON_CAMERA = re.compile(r"^(?:一张|随手|日常|对镜)?(?:自拍|自画像)|"
                       r"(?:听澜|我自己|我)(?:的|今天的|刚拍的)?(?:自拍|自画像)|"
                       r"(?:听澜|我自己)(?:的)?(?:脸|正面|侧颜|侧脸|近照|头像|肖像|半身|全身)|"
                       r"(?:听澜|蓝(?:紫)?色?长发的?(?:女孩|少女))[^。！？\n]{0,28}(?:出镜|入镜|站在|坐在|回头|看向镜头|合影)|"
                       r"(?:拍|画|看)(?:一张|张)?(?:听澜|我自己)(?:的样子|本人)|"
                       r"\b(?:portrait|selfie|profile) of (?:Chen )?Tinglan\b", re.I)


def photo_input(description: str, presentation: str, face_sheet: Path, *,
               shows_face: bool | None = None, portrait: bool = False) -> tuple[str, Path | None]:
    """Return the prompt and its matching attachment for one generation."""
    framing.image_facts(presentation)
    if not face_sheet.is_file():
        reference = framing.STORY_REFERENCE if presentation == "story_photo" else None
        return framing.image_prompt(description, presentation), reference

    if type(shows_face) is not bool:
        # Older callers provide only a description. Prefer the current brief to
        # the previous work, the post caption or the history quoted alongside it.
        brief = description.partition("以这些听澜自己的作品为灵感：")[0]
        for marker in ("\n爸爸指定的画面要求：", "\n本轮完整画面描述："):
            _, found, current = brief.rpartition(marker)
            if found and current.strip():
                brief = current
        shows_face = not OFF_CAMERA.search(brief) and (portrait or bool(ON_CAMERA.search(brief)))

    if presentation == "artwork" and not shows_face:
        return description, None
    style = ""
    if presentation == "story_photo":
        city = world_profile()["city"]["name"]
        style = f"用细腻的二维手绘线条和柔和的明暗，画出{city}生活里的这一刻。"
    if shows_face:
        identity = (
            "参考页中的四张头像是同一个陈听澜。以上排睁眼正面和侧脸确定脸型、蓝紫色眼睛、"
            "眉眼间距、鼻唇和下颌轮廓。下排的笑容与眨眼展示五官随表情的变化。"
            "右下的小图补充整体轮廓和蓝白鲸尾。合影中只有听澜使用这组五官。"
            "成图是一幅完整画面。本次描述决定表情、视角、服装、姿势、光线和背景。"
        )
        return style + identity + "\n本次画面要求：" + description, face_sheet
    return (style + "把镜头对准描述中的景物、物件或其他人物。拍摄者在镜头后，"
            "描述点明的手或背影可以自然入画。\n本次画面要求：" + description), None
