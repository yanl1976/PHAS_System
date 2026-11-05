"""把一张已识别的《加工路线单》按原版式渲染成可打印的 PDF。

版式不是重新设计的——`app/render/route_form.py` 画的就是车间现行空白表，
识别侧拟合列边界用的也是同一份毫米坐标。所以：

    打印出来的单子 ≡ 之前完善后的空白单据，只是格子里填了字。

手写约定也跟着原单走（用户当面确认过的规矩）：
    2ʰ      = 2 小时
    30'     = 30 分钟
    1ʰ30'   = 1 小时 30 分
定额工时栏按这个写法打印，车间看得懂、也和实物一致；
「合计」栏与「小计」沿用印刷体的 时:分:秒（原单 0:00:00 的格式）。
"""
from __future__ import annotations

import datetime as dt
import io

from ..models import Sheet
from ..render import route_form as form


# ------------------------------------------------------------------ 数字写法
def hours_text(value: float | None) -> str:
    """单件工时 → 手写体写法。2.0→「2ʰ」，0.5→「30'」，1.5→「1ʰ30'」。"""
    if value is None:
        return ""
    total_min = int(round(float(value) * 60))
    if total_min <= 0:
        return ""
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h}ʰ{m}'"
    if h:
        return f"{h}ʰ"
    return f"{m}'"


def clock_text(value: float | None) -> str:
    """合计工时 → 印刷体 时:分:秒（与原单小计同格式）。

    秒位固定 00：手写定额最细只到分钟，算出 115:29:55 这种秒数是浮点误差，
    打到单子上反而像系统算错了。
    """
    if value is None:
        return ""
    total_min = int(round(float(value) * 60))
    if total_min <= 0:
        return "0:00:00"
    h, m = divmod(total_min, 60)
    return f"{h}:{m:02d}:00"


def qty_text(value: float | None) -> str:
    if value is None:
        return ""
    if abs(float(value) - round(float(value))) < 1e-6:
        return str(int(round(float(value))))
    return f"{float(value):g}"


def date_text(value: dt.date | None, raw: str = "") -> str:
    if value:
        return f"{value.month}.{value.day}"
    # 手写日期识别不出年份，但原文（如「9.10」）比空白更有用
    return (raw or "").strip()


# ------------------------------------------------------------------ 组装数据
def build_values(sheet: Sheet, print_date: dt.date | None = None) -> dict:
    """把单据数据映射成 render 需要的 values 结构。"""
    header = {}
    try:
        import json

        header = json.loads(sheet.header_json or "{}")
    except Exception:  # noqa: BLE001 - 表头坏了也要能打出来
        header = {}

    # 原表「件号」栏现场普遍写的是件名，识别也把件名归到 part_name，
    # 所以两个「件号」格都填件名，避免打出来是空的
    part_name = (header.get("part_name") or "").strip()
    if not part_name and sheet.items:
        part_name = (sheet.items[0].part_name or "").strip()

    header_out = {
        "batch_no": header.get("batch_no") or "",
        "product_code": header.get("product_code") or "",
        "product_name": header.get("product_name") or "",
        "part_no": part_name,
        "part_no@2": part_name,
        "material": header.get("material") or "",
        "machine_count": header.get("machine_count") or "",
        "part_count": header.get("part_count") or "",
        "category": header.get("category") or "",
        "dispatcher": header.get("dispatcher") or "",
        "picker": header.get("picker") or "",
    }

    rows = []
    for it in sheet.items:
        worker = it.worker.name if getattr(it, "worker", None) else (it.worker_raw or "")
        rows.append(
            {
                "process": it.process or it.process_raw or "",
                "_qty": "",  # 原单「数量」栏多为空，识别后也无法可靠还原，不臆造
                "unit_hours": hours_text(it.unit_hours),
                "declared_total": clock_text(it.total_hours),
                "worker_raw": worker,
                "qualified_qty": qty_text(it.qualified_qty),
                "rework_qty": qty_text(it.rework_qty),
                "scrap_qty": qty_text(it.scrap_qty),
                "inspector": it.inspector or "",
                "finish_date": date_text(it.finish_date, it.finish_date_raw),
                "remark": "",
            }
        )

    today = print_date or dt.date.today()
    return {
        "header": header_out,
        "rows": rows,
        "footer": {"plan": "", "date": f"{today.year}.{today.month}.{today.day}"},
        "subtotal": clock_text(sheet.total_hours),
    }


# ------------------------------------------------------------------ 输出
def render_pdf(sheet: Sheet, dpi: int = 300) -> bytes:
    """按需拆页输出 PDF（10 行/页），纸张 A4 横向，与空白模板同尺寸。"""
    values = build_values(sheet)
    pages = form.render_pages(values, dpi=dpi)
    buf = io.BytesIO()
    pages[0].save(
        buf,
        "PDF",
        resolution=float(dpi),
        save_all=True,
        append_images=pages[1:],
    )
    return buf.getvalue()


def render_png(sheet: Sheet, dpi: int = 150, page: int = 1) -> bytes:
    """单页 PNG，用于页面内预览（不进打印链路）。"""
    values = build_values(sheet)
    pages = form.render_pages(values, dpi=dpi)
    idx = max(0, min(page - 1, len(pages) - 1))
    buf = io.BytesIO()
    pages[idx].save(buf, "PNG")
    return buf.getvalue()


def page_count(sheet: Sheet) -> int:
    return max(1, -(-len(sheet.items) // form.ROW_COUNT))
