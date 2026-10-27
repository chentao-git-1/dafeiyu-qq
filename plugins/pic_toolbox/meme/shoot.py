"""Shoot meme: 射击表情包 — 13帧 GIF，支持二次元+真人脸检测"""

import os
import numpy as np
from PIL import Image

_FRAMES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "resource", "shoot_frames")
_FRAME_COUNT = 13
_DURATION = 150

# ── 级联分类器（懒加载）────────────────────
_anime_cascade = None       # LBP 级联：二次元专用（nagadomi/lbpcascade_animeface）
_frontal_cascade = None     # Haar 级联：真人正面脸
_profile_cascade = None     # Haar 级联：真人侧脸


def _get_cascades():
    """加载三级级联分类器：LBP动漫 → Haar正面 → Haar侧脸"""
    global _anime_cascade, _frontal_cascade, _profile_cascade
    if _anime_cascade is None:
        import cv2

        # 1) LBP 级联：二次元动漫脸（首选）
        anime_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..", "resource", "lbpcascade_animeface.xml"
        )
        if os.path.isfile(anime_path):
            _anime_cascade = cv2.CascadeClassifier(anime_path)

        # 2) Haar 级联：真人正面（回退）
        haarcascades = cv2.data.haarcascades
        _frontal_cascade = cv2.CascadeClassifier(haarcascades + "haarcascade_frontalface_default.xml")

        # 3) Haar 级联：真人侧脸（回退）
        _profile_cascade = cv2.CascadeClassifier(haarcascades + "haarcascade_profileface.xml")

    return _anime_cascade, _frontal_cascade, _profile_cascade


def _detect_face_focal(img: Image.Image):
    """
    三级检测策略：LBP动漫 → Haar正面 → Haar侧脸
    每个级联尝试多组参数提升召回率，最终取面积最大的检测框。
    返回人脸中心点 (cx, cy)，未检测到返回 None。
    """
    try:
        import cv2 as _cv
        anime_cas, frontal_cas, profile_cas = _get_cascades()
        gray = np.array(img.convert("L"))

        all_faces = []  # [(x, y, w, h, source), ...]

        # ── 第 1 层：LBP 动漫级联 ────────────
        if anime_cas is not None:
            try:
                eq = _cv.equalizeHist(gray)
            except Exception:
                eq = gray
            # 多组参数：宽松 → 严格，优先召回
            for sf, mn in ((1.05, 4), (1.03, 3), (1.01, 2)):
                faces = anime_cas.detectMultiScale(
                    eq, scaleFactor=sf, minNeighbors=mn,
                    minSize=(20, 20),
                )
                for x, y, w, h in faces:
                    all_faces.append((x, y, w, h, "anime_lbp"))
                if len(all_faces) > 0:
                    break  # 宽松档已检出 → 不再降级

        # ── 第 2 层：Haar 正面 + 侧脸回退 ─────
        if not all_faces:
            for cascade, label in ((frontal_cas, "frontal"), (profile_cas, "profile")):
                for sf, mn in ((1.05, 3), (1.01, 2)):
                    faces = cascade.detectMultiScale(
                        gray, scaleFactor=sf, minNeighbors=mn,
                        minSize=(30, 30),
                    )
                    for x, y, w, h in faces:
                        all_faces.append((x, y, w, h, label))
                    if all_faces:
                        break
                if all_faces:
                    break

        if not all_faces:
            return None

        # 取面积最大的框
        x, y, w, h, _source = max(all_faces, key=lambda r: r[2] * r[3])
        return (x + w // 2, y + h // 2)

    except Exception:
        return None


def _resize_cover(img: Image.Image, target: tuple[int, int], focal=None):
    tw, th = target
    iw, ih = img.size
    scale = max(tw / iw, th / ih)
    new_w, new_h = int(iw * scale), int(ih * scale)
    resized = img.resize((new_w, new_h), Image.LANCZOS)

    if focal:
        fx, fy = focal[0] * scale, focal[1] * scale
        left, top = int(fx - tw / 2), int(fy - th / 2)
    else:
        left, top = (new_w - tw) // 2, (new_h - th) // 2

    left = max(0, min(left, new_w - tw))
    top = max(0, min(top, new_h - th))
    return resized.crop((left, top, left + tw, top + th))


def generate_shoot(input_path: str, output_path: str) -> str:
    avatar = Image.open(input_path).convert("RGBA")
    focal = _detect_face_focal(avatar)
    frames = []

    for i in range(_FRAME_COUNT):
        overlay = Image.open(os.path.join(_FRAMES_DIR, f"{i:02d}.png")).convert("RGBA")
        base = _resize_cover(avatar, overlay.size, focal)
        base.paste(overlay, (0, 0), overlay)
        frames.append(base)

    frames[0].save(
        output_path, "GIF", save_all=True, append_images=frames[1:],
        duration=_DURATION, loop=0, disposal=2,
    )
    return output_path
