"""预印单上的二维码：载荷编码、验签、出图。

## 码里装什么

只装**定位信息**，不装业务数据：

    单码   HY1:<form_no>:<token>
    行码   HY1:<form_no>:<row_no>:<token>

为什么不把件名、工序、定额一起塞进码里：一张 A4 上要印 10 个行码，
每个码的物理尺寸只有 9mm 见方，数据一多码点就密到手机扫不动。
而且定额这类数据**必须以后端为准** —— 码里带定额，等于让一张纸决定工资，
纸被改了、被复制了都没人知道。所以码只回答「这是哪张单的第几行」，
其余全部回后端查，查的同时顺手验状态（作废了就不让报）。

## 为什么 token 要存库而不是纯签名

纯 HMAC 签名也能防伪造，但**没法作废**：单子印错一张、零件报废不做了，
那张纸还在工人手里，扫了照样能报工。token 存库 + `print_sheet.status`，
作废就把 status 置 void，纸上的码立刻失效。
"""

from __future__ import annotations

import io
import re
import secrets

import qrcode

PREFIX = "HY1"

# ---------------------------------------------------------------- 印刷体单号

# 纸面右上角印着人可读的单号（`qr_stamp` 用黑体 3.7mm 打在码左边），
# 识别侧靠它认出「这张纸是我们印的」。OCR 会把数字认错，所以数字位放宽到
# 常见混淆字符，事后再归一化 —— **认错的代价由「必须能在库里查到」兜住**。
_DIG = "0-9OoQDlIi|ZSB"
_DIGIT_MAP = {
    "O": "0", "Q": "0", "D": "0",
    "I": "1", "L": "1", "|": "1", "i": "1",
    "Z": "2", "S": "5", "B": "8",
}
# 年(2)+月(2)+流水(4~5)，中间可以是连字符、空格、下划线、全角减号、汉字「一」，
# 也可能被 OCR 吞掉（RG261100001）。
_FORM_NO_RE = re.compile(
    rf"R\s*G\s*([{_DIG}]{{2}})\s*([{_DIG}]{{2}})[\s\-–—ー－~_=:：一]{{0,3}}([{_DIG}]{{4,5}})",
    re.IGNORECASE,
)


def _digits(group: str) -> str:
    return "".join(_DIGIT_MAP.get(ch.upper(), ch) for ch in group)


def find_form_nos(lines) -> list[str]:
    """从 OCR 行里找出纸面上的印刷体单号，返回去重后的候选（形如 RG2611-00001）。

    为什么从 OCR 文本找、而不是解码二维码：PaddleOCR 只认文字、读不出二维码，
    而单号是**印刷体黑体**，认错概率远低于手写内容。码是给扫码枪的，
    单号是给 OCR 和人的 —— 两条路读同一个号，才能互认。

    这里只**提候选，不做判断**：调用方必须拿候选去查 `print_sheet` 命中才算数。
    猜错的代价是把别人的单算到自己头上，所以宁可不认。
    """
    out: list[str] = []
    for ln in lines or []:
        text = ln.get("text") if isinstance(ln, dict) else str(ln)
        text = text or ""
        for m in _FORM_NO_RE.finditer(text):
            yy, mm, seq = (_digits(g) for g in m.groups())
            if not (yy.isdigit() and mm.isdigit() and seq.isdigit()):
                continue
            no = f"RG{yy}{mm}-{int(seq):05d}"
            if no not in out:
                out.append(no)
    return out


def new_token() -> str:
    """8 位十六进制，够用且短（码面要小）。"""
    return secrets.token_hex(4)


def form_payload(form_no: str, token: str) -> str:
    return f"{PREFIX}:{form_no}:{token}"


def line_payload(form_no: str, row_no: int, token: str) -> str:
    return f"{PREFIX}:{form_no}:{row_no}:{token}"


def parse(code: str) -> dict:
    """把扫到的字符串拆开。**不验真伪**，验真伪要查库（见 services/print_sheet）。

    扫码枪会带回车/空格，手机扫码会带 URL 前缀，这里一并容错 ——
    计划员不会愿意为了「码里多了个空格」被退回重扫。
    """
    raw = (code or "").strip()
    if raw.startswith("http://") or raw.startswith("https://"):
        # 兼容把码做成链接的批次：取最后一个 path 段
        raw = raw.rstrip("/").rsplit("/", 1)[-1]
    parts = raw.split(":")
    if len(parts) < 3 or parts[0] != PREFIX:
        return {"ok": False, "reason": "不是本系统的单据码"}
    form_no = parts[1].strip()
    if not form_no:
        return {"ok": False, "reason": "码里没有单号"}
    if len(parts) == 3:
        return {"ok": True, "kind": "form", "form_no": form_no, "token": parts[2].strip(),
                "row_no": None}
    if len(parts) == 4:
        try:
            row_no = int(parts[2])
        except ValueError:
            return {"ok": False, "reason": "行号不是数字"}
        return {"ok": True, "kind": "line", "form_no": form_no, "token": parts[3].strip(),
                "row_no": row_no}
    return {"ok": False, "reason": "码格式不对"}


def png(payload: str, *, box_px: int = 300) -> bytes:
    """出 PNG 字节流。给 PDF 合成用。"""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=2,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    if box_px:
        img = img.resize((box_px, box_px))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def pil(payload: str, *, size_px: int = 120):
    """直接给 PIL Image，供渲染器往纸面上贴（不走磁盘、不走字节流）。"""
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img.resize((size_px, size_px))
