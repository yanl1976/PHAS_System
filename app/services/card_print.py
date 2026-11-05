"""工艺卡打印 —— 版式就是《加工路线单》，全项目只有 `app/render/route_form.py` 一份。

**为什么不再另做一套卡片版式**：工艺卡和加工路线单是同一张纸的两个阶段。

    编排好、还没干活  → 印出来的是**工艺卡**：工序与定额预印，操作者/结果/检查员留白
    干完活、录进系统  → 同一张纸变成**加工路线单**（`sheet` + `work_item`）

以前给工艺卡单独写了一套 HTML 表格，工序/工种/设备一列一列排得很漂亮，
但车间手上那张纸印的还是加工路线单 —— 两套东西对不上：卡上写「准备工时」，
纸上没有这一栏；纸上要填「加工结果」，卡上又没地方写。所以这里只做一件事：
**把工艺卡的数据映射到加工路线单的格子里**，然后交给同一个渲染器出图。

映射口径（哪些栏预印、哪些留白）按车间实际用法定：

| 版面格 | 工艺卡预印什么 | 为什么 |
|---|---|---|
| 计调员 | 任务负责人 / 空 | 预印了也没人认，留空让调度自己签 |
| 产品批号 | 生产任务批次 | 一张卡要能独立看出属于哪批活 |
| 件号 ×2 | `01 整机装配` / 件名 | 第一格带层级编号，装订顺序一眼可见 |
| 类别 ×2 | 总装·部件·子件 / `装在 01` | 「上下级关系」落到纸上就靠这两格 |
| 投产台数 | 任务数量 | 没有任务时留空（单台口径） |
| 产品代号 / 产品名称 | 产品 | |
| 材料 | 组合材料（有则印） | |
| 加工件数 | 台数 × 每台用量 | 子件要印出真实件数，否则定额合计对不上 |
| 工序等级 | 工序名（关键工序加 ★） | 宽 65mm，是全表最宽的一栏，工序写这里 |
| 数量 | 留空 | 原单现场这一栏就基本不填 |
| 定额工时 单件 | 编排单件定额 | `2ʰ / 30' / 1ʰ30'` 手写体，与实物一致 |
| 定额工时 合计 | 单件 × 加工件数 | 按本批台数算，不是按 1 台 |
| 操作者 / 加工结果 / 检查员 / 日期 | **留白** | 这是车间要填的，预印等于造假 |
| 备注 | 外协 / 关键工序文字标记 | 只有 13mm，写不了作业要求全文 |
| 小计 | 定额合计 | |
| 计划调度 | 发布人 | 谁认的这版工艺 |
| 日期 | 打印日期 | |

作业要求（sop）版面上没有位置（备注栏只有 13mm），所以不进卡面，
在页面侧栏的卡片详情里看 —— 不为了一句话去改车间现行表格。
"""
from __future__ import annotations

import datetime as dt
import io

from ..render import qr_stamp
from ..render import route_form as form
from .print_form import clock_text, hours_text, qty_text

# 明细列宽（mm），从版式真源取，不在这里写死
_COL_W = {key: width for _name, width, key in form.COLUMNS}
# 手写体字高 4.8mm，汉字近似方块，按它估一列能写几个字
_HAND_MM = 4.8


def _fit(text: str, key: str, *, pad_mm: float = 2.4, char_mm: float = _HAND_MM) -> str:
    """把一句话压到某一列能放下的长度。

    PIL 的 draw.text 不会自动截断，超宽直接压到隔壁格子上，
    打出来就是两行字叠在一起。宁可截断加省略号，也不能让版面糊掉。

    `char_mm` 是这一格用的字高：工序名 4.8mm，挪到工序名下面那行改 3.2mm，
    同样的 65.3mm 宽度就能多写几个字。
    """
    text = (text or "").strip()
    if not text:
        return ""
    limit = max(1, int((_COL_W.get(key, 20.0) - pad_mm) // char_mm))
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


# 备注格只有 13.3mm 宽：4.0mm 字一行只放得下 2 个字（「外协」「检验」刚好）
_CELL_CHARS = 4


def _remark_plan(marker: str, note: str) -> dict:
    """决定备注写在哪儿 —— 纸面上这一格只有 13.3mm，写长了会压到相邻列。

    三条路，都不截断：
      · 只有标记（外协/检验）→ 备注格单行，4.0mm 字
      · 备注 ≤ 4 字 → 备注格两行：上行标记、下行备注，缩到 2.8mm
      · 备注更长 → 备注格只留标记，整条备注挪到工序等级格第二行
        （那里宽 65.3mm，3.2mm 字能写 19 个，够放一整套作业交代）
    """
    marker = (marker or "").strip()
    note = (note or "").strip()
    if note:
        if len(note) <= _CELL_CHARS:
            return {"remark": marker, "remark2": note}
        return {"remark": marker, "process_note": note}
    if marker:
        return {"remark": marker}
    return {}


def card_values(
    card: dict,
    *,
    task: dict | None = None,
    print_date: dt.date | None = None,
) -> dict:
    """一张工艺卡 → 加工路线单的版面值。

    `task` 是生产任务口径（数量、批次、负责人）。没有任务时按**单台**印，
    这时不印投产台数 —— 印「0 台」会让人以为这活是零，印「1」又会被当成
    真的只做一台。留空最诚实：这张卡是标准工艺，不是某一单的执行凭证。
    """
    qty = float((task or {}).get("qty") or 0)
    per_unit = float(card.get("qty_per_unit") or 1)
    # 加工件数由 service 层算好（task_route 上写了数量就用它，否则台数×用量）：
    # 这里再算一遍就会出现「页面显示 44 件、纸上印 1 件」这种对不上的事
    part_qty = float(card.get("print_qty") or per_unit)
    has_batch = qty > 0

    level_label = card.get("level_name") or "零件"
    code = card.get("code") or ""
    parent_code = card.get("parent_code") or ""

    header = {
        "dispatcher": (task or {}).get("dispatcher") or "",
        "batch_no": (task or {}).get("batch_no") or "",
        # 第一格件号带编号：装订时按 01 / 01-01 排，纸上就得看得见
        "part_no": f"{code} {card.get('part') or ''}".strip(),
        "part_no@2": card.get("part") or "",
        "category": level_label,
        "category@2": f"装在 {parent_code}" if parent_code else "",
        "picker": "",
        "product_code": card.get("product") or "",
        "product_name": card.get("product") or "",
        "material": card.get("material") or "",
        "machine_count": qty_text(qty) if has_batch else "",
        "part_count": qty_text(part_qty) if part_qty else "",
    }

    rows = []
    total = 0.0
    for s in card.get("steps") or []:
        unit = float(s.get("unit_hours") or 0)
        line_total = unit * (part_qty or 1)
        total += line_total
        name = (s.get("process_name") or "").strip()
        if s.get("is_key"):
            name = f"{name}★"
        note = ""
        if (s.get("op_type") or "") == "outsource":
            note = "外协"
        elif s.get("inspect_required"):
            note = "检验"
        # 工序备注是工艺员手写的交代，必须印出去 —— 以前只印了「外协/检验」，
        # 页面上有、纸上没有，等于没写。「疑似非工序」是系统核对标记，不是备注。
        hand_note = (s.get("remark") or "").strip()
        if hand_note.startswith("疑似非工序"):
            hand_note = ""
        plan = _remark_plan(note, hand_note)
        row = {
            "process": _fit(name, "process"),
            "_qty": "",
            "unit_hours": hours_text(unit),
            "declared_total": clock_text(line_total),
            # 下面这些是车间填的，预印就是编数据
            "worker_raw": "",
            "qualified_qty": "",
            "rework_qty": "",
            "scrap_qty": "",
            "inspector": "",
            "finish_date": "",
            "remark": plan.get("remark", ""),
            "remark2": plan.get("remark2", ""),
        }
        if plan.get("process_note"):
            # 备注太长塞不进 13mm 的备注格：工序名缩一号放上面，备注放下面
            row["process"] = _fit(name, "process", char_mm=4.0)
            row["process_note"] = _fit(plan["process_note"], "process", char_mm=3.2)
        rows.append(row)
    # 准备工时（每批一次）不进明细行：原单没有这一栏，硬塞进某一行会重复计。
    # 并进小计，让人看得出「这批活一共要多久」。
    total += float(card.get("setup_hours") or 0)

    today = print_date or dt.date.today()
    return {
        "header": header,
        "rows": rows,
        "footer": {
            "plan": card.get("published_by") or "",
            "date": f"{today.year}.{today.month}.{today.day}",
        },
        "subtotal": clock_text(total),
    }


def page_count(card: dict) -> int:
    """这张卡占几页 —— 一道工序一行，超过 10 行就续页（版式写死 10 行/页）。"""
    return max(1, -(-len(card.get("steps") or []) // form.ROW_COUNT))


def render_png(
    card: dict,
    *,
    task: dict | None = None,
    dpi: int = 150,
    page: int = 1,
    qr_map: dict[str, tuple[str, str]] | None = None,
) -> bytes:
    """单页 PNG，页面内预览用（不进打印链路）。

    `qr_map`：`{件名: (单号, token)}`。给了就在右上角贴码 —— 页面预览与
    打印出来的纸必须是同一张，否则「预览看不见码、打出来有码」会让人以为打错了。
    """
    values = card_values(card, task=task)
    pages = form.render_pages(values, dpi=dpi)
    idx = max(0, min(page - 1, len(pages) - 1))
    img = pages[idx]
    hit = (qr_map or {}).get(card.get("part") or "")
    if hit:
        qr_stamp.stamp(img, hit[0], hit[1], dpi)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def render_pdf(
    cards: list[dict],
    *,
    task: dict | None = None,
    dpi: int = 300,
    with_cover: bool = False,
    qr_map: dict[str, tuple[str, str]] | None = None,
) -> bytes:
    """整本工艺卡 → 一个 PDF，一卡一页（工序多的卡自己续页）。

    装订顺序就是传进来的顺序（调用方已按 01 / 01-01 / 01-01-01 排过），
    这里不再排序：打印出来的顺序必须和屏幕上左栏看到的完全一致。

    `qr_map`：`{件名: (单号, token)}`。预印单报工靠纸面上的码，
    所以「工艺卡打印」和「预印单打印」出来的必须是同一张纸 ——
    调用方先确保单号存在，再把映射传进来。
    """
    pages = []
    for c in cards:
        values = card_values(c, task=task)
        rendered = form.render_pages(values, dpi=dpi)
        hit = (qr_map or {}).get(c.get("part") or "")
        for page in rendered:
            if hit:
                qr_stamp.stamp(page, hit[0], hit[1], dpi)
            pages.append(page)
    if not pages:
        pages = [form.render(blank=True, dpi=dpi)]
    buf = io.BytesIO()
    pages[0].save(
        buf,
        "PDF",
        resolution=float(dpi),
        save_all=True,
        append_images=pages[1:],
    )
    return buf.getvalue()
