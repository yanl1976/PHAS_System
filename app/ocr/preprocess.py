"""Pillow 图像预处理。

流水线：EXIF 方向校正 -> 长边归一 -> 灰度 -> 倾斜校正 -> 红笔/红章抑制 -> 对比度增强。

为什么必须做红笔抑制：现场用红笔圈注姓名、红色表格线与印章是 PP-OCR 的主要误检源。
抑制后识别率提升明显，同时把红色掩膜单独留存，用于识别"被红笔标注"的姓名。
"""
from __future__ import annotations

import io
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from ..config import MAX_LONG_SIDE, RED_DELTA, RED_MIN


def load_image(path: str | Path) -> Image.Image:
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # 手机照片按 EXIF 摆正
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    return img


def resize_long_side(img: Image.Image, max_side: int = MAX_LONG_SIDE) -> Image.Image:
    w, h = img.size
    long_side = max(w, h)
    if long_side <= max_side:
        return img
    scale = max_side / long_side
    return img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)


def _skew_score(small: Image.Image, angle: float) -> float:
    """行投影方差：越尖锐说明旋转角越接近真实水平。"""
    rotated = np.asarray(small.rotate(angle, resample=Image.BILINEAR, fillcolor=255), dtype=np.uint8)
    prof = (rotated < 160).astype(np.float32).sum(axis=1)
    if prof.sum() == 0:
        return -1.0
    return float(np.var(prof))


def estimate_skew(img: Image.Image, max_angle: float = 4.0, step: float = 0.5) -> float:
    """投影法估计倾斜角。返回需要反向旋转的角度（度）。

    两段搜索：先按 step 粗扫，再在最优角附近按 0.1° 细扫。
    只做粗扫（0.5° 网格）会留下近 0.5° 的残差——表格竖线在 1000px 高度上就横移
    8px，列投影彻底散掉，`detect_lines` 一条竖线都检不到，列边界只能退回「锚点中点」，
    于是窄列（序号）把宽列（工序等级）的左半边整块吃掉。
    """
    gray = img.convert("L")
    w, h = gray.size
    scale = 800 / max(w, h) if max(w, h) > 800 else 1.0
    small = gray.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.BILINEAR)

    best_angle, best_score = 0.0, -1.0
    for angle in np.arange(-max_angle, max_angle + 1e-6, step):
        score = _skew_score(small, float(angle))
        if score > best_score:
            best_score, best_angle = score, float(angle)

    for angle in np.arange(best_angle - step, best_angle + step + 1e-6, 0.1):
        score = _skew_score(small, float(angle))
        if score > best_score:
            best_score, best_angle = score, float(angle)

    # 细扫后 0.1° 内的抖动属于噪声，视为正交
    if abs(best_angle) < 0.15:
        return 0.0
    return round(-best_angle, 2)


def suppress_red(img: Image.Image) -> tuple[Image.Image, Image.Image, float]:
    """红色笔迹抑制。返回 (去红图, 红色掩膜图, 红色占比)。"""
    rgb = img.convert("RGB")
    arr = np.asarray(rgb, dtype=np.int16)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    mask = (r - np.maximum(g, b) > RED_DELTA) & (r > RED_MIN)
    ratio = float(mask.mean())

    out = arr.copy()
    out[mask] = [255, 255, 255]
    cleaned = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), mode="RGB")

    mask_img = Image.fromarray((mask * 255).astype(np.uint8), mode="L").convert("RGB")
    return cleaned, mask_img, round(ratio, 5)


def enhance(img: Image.Image) -> Image.Image:
    """对比度增强 + 轻度锐化。印刷表头与手写笔画都需要更强边缘。"""
    out = ImageOps.autocontrast(img.convert("L"), cutoff=1)
    out = ImageEnhance.Contrast(out).enhance(1.35)
    out = out.filter(ImageFilter.UnsharpMask(radius=1.4, percent=140, threshold=3))
    return out.convert("RGB")


def make_thumb(img: Image.Image, max_side: int = 420) -> Image.Image:
    thumb = img.copy()
    thumb.thumbnail((max_side, max_side), Image.LANCZOS)
    return thumb


def process(
    src_path: str | Path,
    processed_path: str | Path | None = None,
    thumb_path: str | Path | None = None,
    mask_path: str | Path | None = None,
    deskew: bool = True,
) -> dict:
    """完整预处理。返回元信息 dict（含 page_w/page_h）。"""
    original = load_image(src_path)
    orig_w, orig_h = original.size

    img = resize_long_side(original)
    angle = estimate_skew(img) if deskew else 0.0
    if angle:
        img = img.rotate(angle, resample=Image.BICUBIC, fillcolor=(255, 255, 255), expand=False)

    cleaned, mask_img, red_ratio = suppress_red(img)
    final = enhance(cleaned)

    if processed_path:
        final.save(processed_path, quality=92)
    if thumb_path:
        make_thumb(img).save(thumb_path, quality=85)
    if mask_path:
        make_thumb(mask_img).save(mask_path, quality=80)

    return {
        "orig_width": orig_w,
        "orig_height": orig_h,
        "width": final.size[0],
        "height": final.size[1],
        "skew_angle": angle,
        "red_ratio": red_ratio,
        "deskew": bool(deskew),
    }


def to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def polar_angle_note(angle: float) -> str:
    """给界面用的角度说明。"""
    if abs(angle) < 0.01:
        return "无需校正"
    return f"{'顺时针' if angle > 0 else '逆时针'}校正 {abs(math.degrees(math.radians(angle))):.1f}°"
