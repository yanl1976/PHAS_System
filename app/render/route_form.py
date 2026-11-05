"""标准《加工路线单》的版面渲染 —— 全项目唯一的版式真源。

版式取自车间现行空白表格（A4 横向，见 docs/工时识别系统设计方案.md §1.1）：

    公司名 / 加工路线单
    表头 3 行 × 8 格（计调员・产品批号・件号・类别 / 材料员・产品代号・件号・类别 /
                     投产台数・产品名称・材料・加工件数）
    明细 12 列：序号 | 工序等级 | 数量 | 定额工时(单件・合计) | 操作者 |
                加工结果(合格・返修・废品) | 检查员 | 日期 | 备注
    10 行明细 + 小计行
    表尾：车间主任：    计划调度：    日期：

所有坐标用毫米书写，渲染时按 DPI 换算，打印即 1:1 的 A4。
识别侧的列锚点（app/ocr/layout.py）与本文件的 COLUMNS/HEADER_ROWS 必须保持一致，
改版式时两边一起改——这是「模板」与「识别」不脱节的唯一保证。

历史：本文件原先住在 scripts/form_template.py，为了让「打印接口」也能复用，
搬进了 app/render/route_form.py；脚本改为 13 行的调用方，版式仍然只有一份。
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ..ocr import form_spec as spec

# ---------------------------------------------------------------- 纸张与整体
PAGE_W, PAGE_H = 297.0, 210.0  # A4 横向（mm）
DPI = 300

MARGIN_X = 15.0
TABLE_W = spec.TABLE_W  # 267mm
TABLE_RIGHT = MARGIN_X + TABLE_W

Y_COMPANY = 12.0  # 公司名基线中心
Y_TITLE = 22.0  # 标题中心
TABLE_TOP = 32.0
HEADER_ROW_H = 7.5
COLHEAD_H = 9.0  # 主表头（序号/工序等级/定额工时/加工结果…）
SUBHEAD_H = 7.0  # 子表头（单件/合计、合格/返修/废品）
ROW_H = 9.2
ROW_COUNT = 10
TOTAL_H = 8.0  # 小计行
FOOTER_Y = 178.0

COMPANY = "天水华圆制药设备科技有限责任公司"
TITLE = "加 工 路 线 单"

# ---------------------------------------------------------------- 明细列
COLUMNS = spec.COLUMNS
COL_GROUPS = spec.COL_GROUPS
VERTICAL_LABELS = spec.VERTICAL_LABELS
HEADER_EDGES = spec.HEADER_EDGES
HEADER_ROWS = spec.HEADER_ROWS

FONT_CANDIDATES = {
    "print": [r"C:\Windows\Fonts\simsun.ttc", r"C:\Windows\Fonts\simhei.ttf"],
    "title": [r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\msyhbd.ttc"],
    "hand": [r"C:\Windows\Fonts\simkai.ttf", r"C:\Windows\Fonts\STKAITI.TTF"],
}

_FONT_CACHE: dict[tuple[str, float, int], ImageFont.FreeTypeFont] = {}


def _font(kind: str, size_mm: float, dpi: int) -> ImageFont.FreeTypeFont:
    key = (kind, size_mm, dpi)
    hit = _FONT_CACHE.get(key)
    if hit is not None:
        return hit
    px = max(6, int(round(size_mm * dpi / 25.4)))
    for path in FONT_CANDIDATES.get(kind, []):
        if Path(path).exists():
            try:
                font = ImageFont.truetype(path, px)
                _FONT_CACHE[key] = font
                return font
            except Exception:  # noqa: BLE001 - 字体损坏时换下一个
                continue
    font = ImageFont.load_default()
    _FONT_CACHE[key] = font
    return font


def col_edges() -> list[float]:
    edges = [0.0]
    for _name, width, _key in COLUMNS:
        edges.append(edges[-1] + width)
    return edges


def body_top() -> float:
    return TABLE_TOP + HEADER_ROW_H * 3 + COLHEAD_H + SUBHEAD_H


def total_top() -> float:
    return body_top() + ROW_H * ROW_COUNT


def render(
    values: dict | None = None,
    dpi: int = DPI,
    blank: bool = False,
) -> Image.Image:
    """渲染一张加工路线单（单页）。

    values = {
        "header": {字段键: 值, ...},          # 键见 HEADER_ROWS
        "rows":   [{字段键: 值, ...}, ...],   # 键见 COLUMNS，最多 ROW_COUNT 行
        "footer": {"plan": "张喜军", "date": "2026.9", "workshop_head": ""},
        "subtotal": "0:00:00",
    }
    blank=True 时忽略所有值，只出印刷体表格。
    """
    return _render_page(values, dpi=dpi, blank=blank, page_no=1, page_count=1)


def render_pages(values: dict | None = None, dpi: int = DPI) -> list[Image.Image]:
    """按 10 行/页拆页渲染。明细不超过 10 行时只返回 1 页（与空白模板完全一致）。"""
    values = values or {}
    rows = list(values.get("rows") or [])
    pages = max(1, -(-len(rows) // ROW_COUNT))
    return [
        _render_page(values, dpi=dpi, blank=False, page_no=i + 1, page_count=pages)
        for i in range(pages)
    ]


def _render_page(
    values: dict | None,
    dpi: int = DPI,
    blank: bool = False,
    page_no: int = 1,
    page_count: int = 1,
) -> Image.Image:
    values = values or {}
    header_vals = {} if blank else dict(values.get("header") or {})
    all_rows = [] if blank else list(values.get("rows") or [])
    start = (page_no - 1) * ROW_COUNT
    rows = all_rows[start : start + ROW_COUNT]
    footer = {} if blank else dict(values.get("footer") or {})

    k = dpi / 25.4

    def X(mm: float) -> float:
        return MARGIN_X * k + mm * k

    def Y(mm: float) -> float:
        return mm * k

    img = Image.new("RGB", (int(PAGE_W * k), int(PAGE_H * k)), "white")
    d = ImageDraw.Draw(img)

    f_company = _font("title", 4.8, dpi)
    f_title = _font("title", 7.8, dpi)
    f_label = _font("print", 3.4, dpi)
    f_label_s = _font("print", 2.8, dpi)
    f_idx = _font("print", 3.8, dpi)
    f_hand = _font("hand", 4.8, dpi)
    f_hand_s = _font("hand", 4.0, dpi)
    f_hand_xs = _font("hand", 3.2, dpi)
    f_note = _font("print", 2.6, dpi)
    f_footer = _font("print", 3.8, dpi)

    thin = max(1, int(round(0.30 * k)))
    thick = max(2, int(round(0.55 * k)))

    def line(x0, y0, x1, y1, width=thin):
        d.line([X(x0), Y(y0), X(x1), Y(y1)], fill=(0, 0, 0), width=width)

    def rect(x0, y0, x1, y1, width=thin):
        d.rectangle([X(x0), Y(y0), X(x1), Y(y1)], outline=(0, 0, 0), width=width)

    def text_c(cx, cy, s, font, fill=(0, 0, 0)):
        d.text((X(cx), Y(cy)), s, font=font, fill=fill, anchor="mm")

    def text_l(x, cy, s, font, fill=(0, 0, 0)):
        d.text((X(x), Y(cy)), s, font=font, fill=fill, anchor="lm")

    def text_r(x, cy, s, font, fill=(0, 0, 0)):
        d.text((X(x), Y(cy)), s, font=font, fill=fill, anchor="rm")

    def text_v(cx, cy, s, font, fill=(0, 0, 0)):
        """竖排（序号/数量在原表是上下两字）。"""
        n = len(s)
        step = 4.2
        y0 = cy - (n - 1) * step / 2
        for i, ch in enumerate(s):
            text_c(cx, y0 + i * step, ch, font, fill)

    def text_c_super(cx, cy, s, font, fill=(0, 0, 0)):
        """居中写含上标 h 的工时（2ʰ / 1ʰ30'）。

        宋体没有 U+02B0（ʰ）这个码位，直接写会画成方框。车间手写本来就是
        「2 后面跟一个抬高的 h」，所以按小一号字上移绘制，既是真上标，
        也不依赖字体是否有这个码位。
        """
        if "ʰ" not in s:
            text_c(cx, cy, s, font, fill)
            return
        pieces: list[tuple[str, bool]] = []
        buf = ""
        for ch in s:
            if ch == "ʰ":
                if buf:
                    pieces.append((buf, False))
                    buf = ""
                pieces.append(("h", True))
            else:
                buf += ch
        if buf:
            pieces.append((buf, False))

        sup_font = _font("hand", 2.9, dpi)
        widths = []
        for txt, sup in pieces:
            f = sup_font if sup else font
            widths.append(d.textlength(txt, font=f))
        x_px = X(cx) - sum(widths) / 2
        for (txt, sup), w in zip(pieces, widths):
            f = sup_font if sup else font
            dy = Y(cy) - (1.5 * k if sup else 0.0)
            d.text((x_px, dy), txt, font=f, fill=fill, anchor="lm")
            x_px += w

    # ---- 抬头
    text_c(PAGE_W / 2, Y_COMPANY, COMPANY, f_company)
    text_c(PAGE_W / 2, Y_TITLE, TITLE, f_title)

    edges = col_edges()

    # ---- 表头 3 行 × 8 格
    y = TABLE_TOP
    for ri, row_spec in enumerate(HEADER_ROWS):
        for gi, (label, key) in enumerate(row_spec):
            li, vi = gi * 2, gi * 2 + 1
            lx0, lx1 = HEADER_EDGES[li], HEADER_EDGES[li + 1]
            vx0, vx1 = HEADER_EDGES[vi], HEADER_EDGES[vi + 1]
            rect(lx0, y, lx1, y + HEADER_ROW_H)
            rect(vx0, y, vx1, y + HEADER_ROW_H)
            text_c((lx0 + lx1) / 2, y + HEADER_ROW_H / 2, label, f_label)
            # 「件号」「类别」原表印了两遍，现场第二格常被写成件名，
            # 所以允许用 "part_no@2" 单独指定第 2 次出现的值
            val = header_vals.get(f"{key}@{ri + 1}", header_vals.get(key)) or ""
            if str(val).strip():
                text_c((vx0 + vx1) / 2, y + HEADER_ROW_H / 2, str(val).strip(), f_hand)
        y += HEADER_ROW_H

    # ---- 列头区：主表头 + 子表头两段
    # 只有「定额工时」「加工结果」分成上下两格，其余列（序号/工序等级/数量/操作者/
    # 检查员/日期/备注）都是通栏一格——原表就是印刷成这样，通栏格不能被横线切开。
    ch_top = y
    ch_bottom = ch_top + COLHEAD_H
    sh_bottom = ch_bottom + SUBHEAD_H
    rect(0, ch_top, TABLE_W, sh_bottom, thick)

    for name, start_i, end_i in COL_GROUPS:
        x0, x1 = edges[start_i], edges[end_i]
        line(x0, ch_top, x0, sh_bottom)  # 组左边界（最右侧组的右边界由外框给）
        if end_i - start_i > 1:
            line(x0, ch_bottom, x1, ch_bottom)  # 母表头与子表头的分隔线
            text_c((x0 + x1) / 2, (ch_top + ch_bottom) / 2, name, f_label)
            for i in range(start_i, end_i):
                if i > start_i:
                    line(edges[i], ch_bottom, edges[i], sh_bottom)
                text_c(
                    (edges[i] + edges[i + 1]) / 2,
                    (ch_bottom + sh_bottom) / 2,
                    COLUMNS[i][0],
                    f_label_s,
                )
        elif name in VERTICAL_LABELS:
            text_v((x0 + x1) / 2, (ch_top + sh_bottom) / 2, name, f_label)
        else:
            text_c((x0 + x1) / 2, (ch_top + sh_bottom) / 2, name, f_label)

    # ---- 明细行
    b_top = body_top()
    for r in range(ROW_COUNT):
        ry = b_top + r * ROW_H
        rect(0, ry, TABLE_W, ry + ROW_H)
        for i in range(1, len(edges) - 1):
            line(edges[i], ry, edges[i], ry + ROW_H)
        # 印刷体行号（续页接着编号）
        text_c((edges[0] + edges[1]) / 2, ry + ROW_H / 2, str(start + r + 1), f_idx)
        if r < len(rows):
            row = rows[r]
            for i, (_name, _w, key) in enumerate(COLUMNS):
                if key == "_idx":
                    continue
                cx = (edges[i] + edges[i + 1]) / 2
                cy = ry + ROW_H / 2
                if key == "remark":
                    # 备注格只有 13.3mm：标记（外协/检验）单行 4.0mm 字刚好；
                    # 再加上一句短备注就排两行、缩到 2.8mm（见 card_print._remark_plan）。
                    # 这里要在「值为空跳过」之前处理 —— 只有备注、没有标记时，
                    # 第一行是空的、第二行有字，按常规分支会被整格跳过。
                    val = str(row.get("remark") or "").strip()
                    val2 = str(row.get("remark2") or "").strip()
                    if val and val2:
                        text_c(cx, ry + 3.05, val, f_note)
                        text_c(cx, ry + 6.55, val2, f_note)
                    elif val2:
                        text_c(cx, cy, val2, f_note)
                    elif val:
                        text_c(cx, cy, val, f_hand_s)
                    continue
                val = row.get(key) or ""
                val = str(val).strip()
                if not val:
                    continue
                if key == "process":
                    # 备注太长会挪到工序名下面一行：上面工序名、下面小字交代
                    val2 = str(row.get("process_note") or "").strip()
                    if val2:
                        text_l(edges[i] + 1.2, ry + 2.95, val, f_hand_s)
                        text_l(edges[i] + 1.2, ry + 6.75, val2, f_hand_xs)
                    else:
                        text_l(edges[i] + 1.2, cy, val, f_hand)
                elif key == "worker_raw":
                    # 工序与签名靠左写，跟手写习惯一致
                    text_l(edges[i] + 1.2, cy, val, f_hand)
                elif key == "unit_hours":
                    # 定额工时是「2ʰ / 30' / 1ʰ30'」的上标写法，单独绘制
                    text_c_super(cx, cy, val, f_hand)
                else:
                    text_c(cx, cy, val, f_hand if key != "finish_date" else f_hand_s)

    # ---- 小计行
    t_top = total_top()
    t_bottom = t_top + TOTAL_H
    rect(0, t_top, TABLE_W, t_bottom, thick)
    line(edges[3], t_top, edges[3], t_bottom)  # 数量右
    for i in (4, 5, 6, 7, 8, 9, 10, 11):
        line(edges[i], t_top, edges[i], t_bottom)
    text_l(1.5, (t_top + t_bottom) / 2, "小计：", f_label)
    if blank:
        subtotal = "0:00:00"
    elif page_no < page_count:
        subtotal = "（续下页）"
    else:
        subtotal = values.get("subtotal") or "0:00:00"
    text_c((edges[4] + edges[5]) / 2, (t_top + t_bottom) / 2, subtotal, f_idx)

    # ---- 表尾
    text_l(0.0, FOOTER_Y, "车间主任：", f_footer)
    text_c(TABLE_W * 0.55, FOOTER_Y, "计划调度：", f_footer)
    if footer.get("plan"):
        text_l(TABLE_W * 0.55 + 19.0, FOOTER_Y, footer["plan"], f_hand_s)
    text_r(TABLE_W, FOOTER_Y, "日期：" + (footer.get("date") or ""), f_footer)

    # 页码只在多页时出现，单页输出与空白模板逐像素一致（格式不能变）
    if page_count > 1:
        text_c(TABLE_W / 2, FOOTER_Y + 12.0, f"第 {page_no} 页 / 共 {page_count} 页", f_label_s)

    return img


def header_text_boxes() -> dict[str, tuple[float, float, float, float]]:
    """表头值格的 (x0, y0, x1, y1)，单位 mm —— 供测试脚本核对填值位置。"""
    boxes: dict[str, tuple[float, float, float, float]] = {}
    y = TABLE_TOP
    for row_spec in HEADER_ROWS:
        for gi, (_label, key) in enumerate(row_spec):
            vi = gi * 2 + 1
            boxes.setdefault(key, (HEADER_EDGES[vi], y, HEADER_EDGES[vi + 1], y + HEADER_ROW_H))
        y += HEADER_ROW_H
    return boxes
