"""鞭笞表情包生成器 — 9帧 GIF。

左下方球 = 被 @ 人的头像（user_head）
右上方球 = 指令发送者的头像（self_head）

参考原始 Rust 实现：meme-generator/memes/lash
"""

import os
from PIL import Image, ImageDraw

_FRAMES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resource", "lash_frames")
_FRAME_COUNT = 9

# 右上方球（指令者 self_head）各帧坐标 → 原 Rust self_locs
_SELF_LOCS = [
    (84, 25), (87, 23), (87, 27),
    (86, 28), (62, 26), (59, 28),
    (76, 20), (85, 24), (80, 23),
]
_SELF_SIZE = (22, 22)

# 左下方球（被 @ 人 user_head）各帧坐标 → 原 Rust user_locs
_USER_LOCS = [
    (12, 69), (15, 66), (14, 67),
    (15, 66), (17, 67), (14, 63),
    (21, 56), (15, 62), (17, 69),
]
_USER_SIZE = (22, 22)
_USER_ROTATE = 30.0


def _make_circle(img: Image.Image) -> Image.Image:
    size = img.size[0]
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask)
    return result


def generate_lash(
    commander_path: str,
    target_path: str,
    output_path: str,
    fps: int = 20,
) -> str:
    """生成鞭笞 GIF。

    commander_path — 指令发送者头像（右上方球）
    target_path    — 被 @ 人头像（左下方球）
    output_path    — 输出 GIF 路径
    fps            — 帧率，默认 20
    """
    commander = Image.open(commander_path).convert("RGBA")
    commander = commander.resize(_SELF_SIZE, Image.LANCZOS)
    commander = _make_circle(commander)

    target = Image.open(target_path).convert("RGBA")
    target = target.resize(_USER_SIZE, Image.LANCZOS)
    target = _make_circle(target)
    # rotate_crop: 中心旋转后裁剪回原尺寸（匹配 Rust rotate_crop 行为）
    rotated = target.rotate(_USER_ROTATE, expand=True, resample=Image.BICUBIC)
    left = (rotated.width - _USER_SIZE[0]) // 2
    top = (rotated.height - _USER_SIZE[1]) // 2
    target = rotated.crop((left, top, left + _USER_SIZE[0], top + _USER_SIZE[1]))

    duration_ms = int(1000.0 / fps)
    frames = []

    for i in range(_FRAME_COUNT):
        overlay = Image.open(os.path.join(_FRAMES_DIR, f"{i}.png")).convert("RGBA")
        canvas = Image.new("RGBA", overlay.size, (0, 0, 0, 0))
        canvas.paste(overlay, (0, 0), overlay)

        # 贴右上方球（指令者 self_head）
        canvas.paste(commander, _SELF_LOCS[i], commander)

        # 贴左下方球（被 @ 人 user_head）
        canvas.paste(target, _USER_LOCS[i], target)

        frames.append(canvas)

    frames[0].save(
        output_path,
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=duration_ms,
        loop=0,
        disposal=2,
    )
    return output_path
