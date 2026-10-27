"""旋转模块：静态图/GIF 顺时针/逆时针 90° 旋转"""

from PIL import Image
from .gif_utils import is_gif, unfold_frames, save_rgba_gif


def rotate_clockwise(input_path: str, output_path: str) -> str:
    """顺时针旋转 90°"""
    if is_gif(input_path):
        return _rotate_gif(input_path, output_path, Image.ROTATE_270)
    img = Image.open(input_path).convert("RGBA")
    img.transpose(Image.ROTATE_270).save(output_path, "PNG")
    return output_path


def rotate_counterclockwise(input_path: str, output_path: str) -> str:
    """逆时针旋转 90°"""
    if is_gif(input_path):
        return _rotate_gif(input_path, output_path, Image.ROTATE_90)
    img = Image.open(input_path).convert("RGBA")
    img.transpose(Image.ROTATE_90).save(output_path, "PNG")
    return output_path


def _rotate_gif(input_path: str, output_path: str, method) -> str:
    gif = Image.open(input_path)
    src_palette = gif.getpalette()
    src_trans = gif.info.get("transparency")
    frames, durations = unfold_frames(gif)
    rotated_frames = [frame.convert("RGBA").transpose(method) for frame in frames]

    save_rgba_gif(rotated_frames, durations, output_path, loop=gif.info.get("loop", 0),
                  source_palette=src_palette,
                  source_trans_idx=src_trans)
    return output_path
