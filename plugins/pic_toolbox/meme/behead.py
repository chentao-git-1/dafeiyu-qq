"""砍头表情包生成器 — 21帧 GIF。单头像模式，仅使用被 @ 用户头像。"""

import os
from PIL import Image

_FRAMES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resource", "behead_frames")
_FRAME_COUNT = 21
_HEAD_SIZE = (75, 75)

_LOCS = [
    (80, 72, 0),   (83, 73, 0),   (82, 73, 0),
    (78, 73, 0),   (72, 74, 0),   (72, 75, 0),
    (73, 76, 0),   (73, 76, 0),   (73, 76, 0),
    (74, 76, 0),   (74, 76, 0),   (70, 73, -12),
    (61, 62, -25), (49, 40, -45), (46, 30, -65),
    (50, 35, -85), (39, 34, -105),(19, 45, -135),
    (9, 91, -155), (6, 161, -175),(-4, 248, -180),
]


def generate_behead(input_path: str, output_path: str, fps: int = 20) -> str:
    avatar = Image.open(input_path).convert("RGBA")
    w, h = avatar.size
    side = min(w, h)
    avatar = avatar.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
    avatar = avatar.resize(_HEAD_SIZE, Image.LANCZOS)

    duration_ms = int(1000.0 / fps)
    frames = []

    for i in range(_FRAME_COUNT):
        overlay = Image.open(os.path.join(_FRAMES_DIR, f"{i:02d}.png")).convert("RGBA")
        canvas = Image.new("RGBA", overlay.size, (255, 255, 255, 255))
        x, y, angle = _LOCS[i]
        # Rust rotate 始终 expand；PIL 逆时针为正、Skia 顺时针为正，方向相反需取反
        head = avatar.rotate(-angle, expand=True, resample=Image.BICUBIC) if angle != 0 else avatar
        canvas.paste(head, (x, y), head)
        canvas.paste(overlay, (0, 0), overlay)
        frames.append(canvas)

    frames[0].save(
        output_path, "GIF", save_all=True, append_images=frames[1:],
        duration=duration_ms, loop=0, disposal=2,
    )
    return output_path
