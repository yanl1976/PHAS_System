"""把二维码贴到《加工路线单》页面的右上角。

**这里是唯一实现**：无论是从「扫码报工 → 打印预印单」出图，还是从「工艺卡 → 打印」
出图，都调这一个函数。两份版式各贴一遍的后果是——改了一个忘了另一个，
纸上的码跑到表格里，识别侧当场列错位。

位置固定在 x=266mm / y=3mm、14mm 见方：
  · 公司名居中在 148mm、标题也居中，x>200 是空白，不压任何字；
  · 表格从 y=32mm 才开始，码在 3~17mm，离表格足够远；
  · 识别侧靠 `form_spec.COLUMNS` 的列锚点还原版面，表格里多一块黑白方块，
    整张单的列就会错位。放表格外，两边互不干扰 —— 这是「预印单照样能被 OCR 读」的前提。
"""
from __future__ import annotations

from PIL import ImageDraw, ImageFont

from ..domain import qr

QR_SIZE = 14.0
QR_X = 266.0
QR_Y = 3.0
TEXT_RIGHT = 263.0

_FONT_NO = r"C:\Windows\Fonts\simhei.ttf"
_FONT_TIP = r"C:\Windows\Fonts\simsun.ttc"


def stamp(page, form_no: str, token: str, dpi: int):
    """在页面右上角贴「单号 + 提示 + 二维码」，返回同一张图（就地修改）。"""
    k = dpi / 25.4
    draw = ImageDraw.Draw(page)
    f_no = ImageFont.truetype(_FONT_NO, max(11, int(3.7 * k)))
    f_tip = ImageFont.truetype(_FONT_TIP, max(9, int(2.8 * k)))

    img = qr.pil(qr.form_payload(form_no, token), size_px=int(QR_SIZE * k))
    page.paste(img, (int(QR_X * k), int(QR_Y * k)))

    # 文字右对齐到码的左边：单号最要紧，放大；两行提示写清「谁来扫、扫完干什么」
    draw.text((TEXT_RIGHT * k, (QR_Y + 0.6) * k), form_no, font=f_no, fill=(0, 0, 0), anchor="ra")
    draw.text((TEXT_RIGHT * k, (QR_Y + 5.6) * k), "计划员扫码报工", font=f_tip,
              fill=(50, 50, 50), anchor="ra")
    draw.text((TEXT_RIGHT * k, (QR_Y + 9.2) * k), "纸单签字后交回", font=f_tip,
              fill=(50, 50, 50), anchor="ra")
    return page
