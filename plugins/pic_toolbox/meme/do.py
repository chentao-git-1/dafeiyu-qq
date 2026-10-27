"""撅表情包生成器 — 3帧 GIF。

左球 = 被 @ 人的头像（user_head）
右球 = 指令发送者的头像（self_head）

参考原始 Rust 实现：meme-generator/memes/do_
"""

import os
from PIL import Image, ImageDraw

_FRAMES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resource", "do_frames")
_FRAME_COUNT = 3

# 右球（指令者 self_head）各帧坐标 → 原 Rust self_locs
_SELF_LOCS = [(116, -8), (109, 3), (130, -10)]
_SELF_SIZE = (122, 122)
_SELF_ROTATE = -15.0

# 左球（被 @ 人 user_head）各帧坐标 → 原 Rust user_locs
_USER_LOCS = [(2, 177), (12, 172), (6, 158)]
_USER_SIZE = (112, 112)
_USER_ROTATE = 90.0


def _make_circle(img: Image.Image) -> Image.Image:
    """将正方形图片裁剪为圆形。"""
    size = img.size[0]
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, size, size), fill=255)
    result = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    result.paste(img, (0, 0), mask)
    return result


def generate_do(
    commander_path: str,
    target_path: str,
    output_path: str,
    fps: int = 20,
) -> str:
    """生成撅 GIF。

    commander_path — 指令发送者头像（右球）
    target_path    — 被 @ 人头像（左球）
    output_path    — 输出 GIF 路径
    fps            — 帧率，默认 20
    """
    # 加载并处理右球（指令者）
    commander = Image.open(commander_path).convert("RGBA")
    commander = commander.resize(_SELF_SIZE, Image.LANCZOS)
    commander = _make_circle(commander)
    commander = commander.rotate(_SELF_ROTATE, expand=False, resample=Image.BICUBIC)

    # 加载并处理左球（被 @ 人）
    target = Image.open(target_path).convert("RGBA")
    target = target.resize(_USER_SIZE, Image.LANCZOS)
    target = _make_circle(target)
    target = target.rotate(_USER_ROTATE, expand=False, resample=Image.BICUBIC)

    duration_ms = int(1000.0 / fps)
    frames = []

    for i in range(_FRAME_COUNT):
        overlay = Image.open(os.path.join(_FRAMES_DIR, f"{i}.png")).convert("RGBA")
        canvas = Image.new("RGBA", overlay.size, (0, 0, 0, 0))

        # 先贴背景帧
        canvas.paste(overlay, (0, 0), overlay)

        # 贴右球（指令者 self_head）
        canvas.paste(commander, _SELF_LOCS[i], commander)

        # 贴左球（被 @ 人 user_head）
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
