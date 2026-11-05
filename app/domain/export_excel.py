"""导出与车间现有版式同构的工时核算汇总单。

关键修复：原表把工时存成 Excel 日期时间值，超过 24 小时即溢出显示为 "5 days, 0:00:00"。
本导出统一用「小时小数」存值（'0.00'），另加一列「合计(时:分)」用 [h]:mm 格式，
既可直接求和，也不会出现天数溢出乱码。
"""
from __future__ import annotations

import datetime as dt
import re
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import (
    EXPORT_DIR,
    HOURS_PER_DAY,
    REWORK_FACTOR,
    SCRAP_FACTOR,
)
from ..models import Period, Sheet, WorkItem
from .aggregation import monthly_summary, worker_process_map

THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEADER_FILL = PatternFill("solid", fgColor="EDEDED")
TITLE_FONT = Font(name="宋体", size=16, bold=True)
SUB_FONT = Font(name="宋体", size=10.5)
HEAD_FONT = Font(name="宋体", size=10.5, bold=True)
CELL_FONT = Font(name="宋体", size=10.5)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center")

PERSON_HEADERS = [
    "序号", "产品名称/代号", "批次", "件名", "工序", "合格件数",
    "单件工时", "合计工时", "完成时间", "检验员", "备注", "合计(时:分)",
]
TOTAL_HEADERS = ["序号", "人员", "工序", "工时", "工时(时:分)"]


def _safe_sheet_title(name: str, used: set[str]) -> str:
    title = re.sub(r"[\\/*?:\[\]]", "", name)[:31] or "人员"
    base, i = title, 2
    while title in used:
        suffix = f"({i})"
        title = base[: 31 - len(suffix)] + suffix
        i += 1
    used.add(title)
    return title


def _person_items(db: Session, ym: str, worker_name: str) -> list[WorkItem]:
    # outer join + coalesce：扫码报工的明细没有 sheet，核算期记在 period_id 上。
    # 这里若继续用 inner join，工资表会出现「汇总有金额、明细一行都没有」，
    # 工人拿到表第一件事就是问「我的活去哪了」。
    stmt = (
        select(WorkItem)
        .outerjoin(Sheet, WorkItem.sheet_id == Sheet.id)
        .join(Period, Period.id == func.coalesce(Sheet.period_id, WorkItem.period_id))
        .where(Period.ym == ym, or_(Sheet.status == "verified", Sheet.id.is_(None)))
    )
    items = list(db.scalars(stmt).unique())
    out = []
    for it in items:
        name = it.worker.name if it.worker else (it.worker_raw or "未匹配")
        if name == worker_name:
            out.append(it)
    out.sort(key=lambda x: (x.finish_date or dt.date(1900, 1, 1), x.seq, x.id))
    return out


def build_workbook(db: Session, ym: str, include_unverified: bool = False) -> Path:
    summary = monthly_summary(db, ym, include_unverified=include_unverified)
    proc_map = worker_process_map(db, ym)
    year, month = int(ym[:4]), int(ym[5:7])

    wb = Workbook()
    wb.remove(wb.active)
    used_titles: set[str] = set()

    for idx, person in enumerate(summary["persons"], start=1):
        name = person["worker_name"]
        ws = wb.create_sheet(_safe_sheet_title(name, used_titles))

        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(PERSON_HEADERS))
        c = ws.cell(row=1, column=1, value=f"{year}年{month}月份个人完成工时汇总表")
        c.font = TITLE_FONT
        c.alignment = CENTER
        ws.row_dimensions[1].height = 26

        ws.cell(row=2, column=1, value=f"姓名：{name}").font = SUB_FONT
        ws.cell(row=2, column=6, value=f"工种：{proc_map.get(name, person['main_process'])}").font = SUB_FONT
        ws.cell(row=2, column=1).alignment = LEFT
        ws.cell(row=2, column=6).alignment = LEFT

        for col, text in enumerate(PERSON_HEADERS, start=1):
            cell = ws.cell(row=3, column=col, value=text)
            cell.font = HEAD_FONT
            cell.fill = HEADER_FILL
            cell.border = BORDER
            cell.alignment = CENTER
        ws.row_dimensions[3].height = 22

        items = _person_items(db, ym, name) if not include_unverified else []
        if include_unverified:
            # 预览模式下按汇总行还原，不展开明细来源
            items = []

        r = 4
        total_hours = 0.0
        for seq, it in enumerate(items, start=1):
            ws.cell(row=r, column=1, value=seq)
            ws.cell(row=r, column=2, value=it.product_name)
            ws.cell(row=r, column=3, value=it.batch_no)
            ws.cell(row=r, column=4, value=it.part_name)
            ws.cell(row=r, column=5, value=it.process)
            ws.cell(row=r, column=6, value=it.qualified_qty)
            ws.cell(row=r, column=7, value=it.unit_hours)
            ws.cell(row=r, column=8, value=it.total_hours)
            date_cell = ws.cell(row=r, column=9, value=it.finish_date)
            date_cell.number_format = "yyyy-mm-dd"
            ws.cell(row=r, column=10, value=it.inspector)
            ws.cell(row=r, column=11, value=it.remark)
            frac = ws.cell(row=r, column=12, value=(it.total_hours or 0) / 24)
            frac.number_format = "[h]:mm"
            total_hours += it.total_hours or 0

            for col in range(1, len(PERSON_HEADERS) + 1):
                cell = ws.cell(row=r, column=col)
                cell.border = BORDER
                cell.font = CELL_FONT
                cell.alignment = CENTER if col != 11 else LEFT
            ws.cell(row=r, column=7).number_format = "0.00"
            ws.cell(row=r, column=8).number_format = "0.00"
            ws.cell(row=r, column=6).number_format = "0.##"
            r += 1

        # 预览模式无明细时，用汇总值占位
        if include_unverified and not items:
            total_hours = person["hours"]

        ws.cell(row=r, column=7, value="合计").font = HEAD_FONT
        ws.cell(row=r, column=7).alignment = CENTER
        total_cell = ws.cell(row=r, column=8, value=round(total_hours, 4))
        total_cell.font = HEAD_FONT
        total_cell.number_format = "0.00"
        total_cell.alignment = CENTER
        frac_cell = ws.cell(row=r, column=12, value=round(total_hours, 4) / 24)
        frac_cell.number_format = "[h]:mm"
        frac_cell.font = HEAD_FONT
        frac_cell.alignment = CENTER
        for col in range(1, len(PERSON_HEADERS) + 1):
            ws.cell(row=r, column=col).border = BORDER

        widths = [6, 22, 12, 18, 12, 10, 10, 10, 12, 10, 20, 12]
        for col, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.freeze_panes = "A4"

    # ---------------------------------------------------------- 总工时台账
    ws_total = wb.create_sheet("总工时")
    ws_total.merge_cells("A1:E1")
    t = ws_total.cell(row=1, column=1, value=f"{year}年{month}月总工时")
    t.font = TITLE_FONT
    t.alignment = CENTER
    ws_total.row_dimensions[1].height = 26

    for col, text in enumerate(TOTAL_HEADERS, start=1):
        cell = ws_total.cell(row=3, column=col, value=text)
        cell.font = HEAD_FONT
        cell.fill = HEADER_FILL
        cell.border = BORDER
        cell.alignment = CENTER

    r = 4
    grand = 0.0
    for idx, person in enumerate(summary["persons"], start=1):
        first = True
        for proc in person["processes"]:
            if first:
                ws_total.cell(row=r, column=1, value=idx)
                ws_total.cell(row=r, column=2, value=person["worker_name"])
                first = False
            ws_total.cell(row=r, column=3, value=proc["process"])
            hours_cell = ws_total.cell(row=r, column=4, value=proc["hours"])
            hours_cell.number_format = "0.00"
            frac = ws_total.cell(row=r, column=5, value=proc["hours"] / 24)
            frac.number_format = "[h]:mm"
            grand += proc["hours"]
            for col in range(1, len(TOTAL_HEADERS) + 1):
                cell = ws_total.cell(row=r, column=col)
                cell.border = BORDER
                cell.font = CELL_FONT
                cell.alignment = CENTER
            r += 1
        # 人员小计另起一列展示，便于核对（不破坏原结构）
        if not person["processes"]:
            ws_total.cell(row=r, column=1, value=idx)
            ws_total.cell(row=r, column=2, value=person["worker_name"])
            ws_total.cell(row=r, column=3, value="")
            hours_cell = ws_total.cell(row=r, column=4, value=0.0)
            hours_cell.number_format = "0.00"
            for col in range(1, len(TOTAL_HEADERS) + 1):
                cell = ws_total.cell(row=r, column=col)
                cell.border = BORDER
                cell.font = CELL_FONT
                cell.alignment = CENTER
            r += 1

    ws_total.cell(row=r, column=2, value="总计").font = HEAD_FONT
    ws_total.cell(row=r, column=2).alignment = CENTER
    gcell = ws_total.cell(row=r, column=4, value=round(grand, 4))
    gcell.font = HEAD_FONT
    gcell.number_format = "0.00"
    gcell.alignment = CENTER
    gfrac = ws_total.cell(row=r, column=5, value=round(grand, 4) / 24)
    gfrac.number_format = "[h]:mm"
    gfrac.font = HEAD_FONT
    gfrac.alignment = CENTER
    for col in range(1, len(TOTAL_HEADERS) + 1):
        ws_total.cell(row=r, column=col).border = BORDER

    for col, width in zip(range(1, 6), [6, 14, 14, 12, 12]):
        ws_total.column_dimensions[get_column_letter(col)].width = width
    ws_total.freeze_panes = "A4"

    # ---------------------------------------------------------- 口径说明
    ws_note = wb.create_sheet("口径说明")
    notes = [
        ("数据来源", "工时图片识别核算系统自动生成"),
        ("生成时间", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("核算期", ym),
        ("含未确认单据", "是（仅供预览，请勿作为结算依据）" if include_unverified else "否（仅统计已核对确认的单据）"),
        ("合计工时口径", "合格件数 × 单件工时，由系统重算，不采用手写合计"),
        ("工时单位", "小时（小数）。原表按 Excel 时间值存储，超过 24 小时会显示为“N days”，本表已修正"),
        ("单件工时制", "定额工时，可按工艺跨多个工作日，不按“一天几小时”封顶；>200 小时才判为误读"),
        ("一天折算", f"{HOURS_PER_DAY} 小时（仅当单据文字明写“天/日”时折算）"),
        ("返修折算系数", f"{REWORK_FACTOR}（0 表示不计工时）"),
        ("废品折算系数", f"{SCRAP_FACTOR}（0 表示不计工时）"),
        ("人员数 / 明细数", f"{summary['totals']['persons']} / {summary['totals']['items']}"),
        ("总工时", f"{summary['totals']['hours']:.2f} 小时"),
    ]
    for row, (k, v) in enumerate(notes, start=1):
        ws_note.cell(row=row, column=1, value=k).font = HEAD_FONT
        ws_note.cell(row=row, column=2, value=v).font = CELL_FONT
        ws_note.cell(row=row, column=2).alignment = LEFT
    ws_note.column_dimensions["A"].width = 20
    ws_note.column_dimensions["B"].width = 70

    suffix = "_预览" if include_unverified else ""
    path = Path(EXPORT_DIR) / f"{ym}车间工时核算{suffix}_{dt.datetime.now():%Y%m%d%H%M%S}.xlsx"
    wb.save(path)
    return path


# ======================================================================
# 工时工资表：在「个人完成工时汇总表」的原格式上加两列（工时单价 / 工时工资）
# ======================================================================
WAGE_HEADERS = PERSON_HEADERS + ["工时单价", "工时工资"]
WAGE_TOTAL_HEADERS = ["序号", "人员", "工种", "工时", "工时(时:分)", "工时单价", "工时工资"]
MONEY_FMT = "#,##0.00"


def build_wage_workbook(db: Session, ym: str, include_unverified: bool = False) -> Path:
    """生成工时工资表。

    版式刻意与车间在用的《个人完成工时汇总表》保持一致（同样的标题行、
    同样的 11 列、同样的顺序），只在右侧**追加** 工时单价 / 工时工资 两列。
    理由：这张表不是给系统看的，是车间主任和财务拿着跟历史月份对比的表 ——
    列一挪、顺序一变，他们就得重新学一遍怎么读。

    每人一张表 + 总工时（原口径）+ 工资汇总（新） + 口径说明。
    """
    from .wages import compute_wages

    data = compute_wages(db, ym, include_unverified=include_unverified)
    proc_map = worker_process_map(db, ym)
    year, month = int(ym[:4]), int(ym[5:7])
    total_cols = len(WAGE_HEADERS)

    wb = Workbook()
    wb.remove(wb.active)
    used_titles: set[str] = set()

    for person in data["persons"]:
        name = person["worker_name"]
        rate = float(person["rate"] or 0.0)
        ws = wb.create_sheet(_safe_sheet_title(name, used_titles))

        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=total_cols)
        c = ws.cell(row=1, column=1, value=f"{year}年{month}月份个人工时工资表")
        c.font = TITLE_FONT
        c.alignment = CENTER
        ws.row_dimensions[1].height = 26

        ws.cell(row=2, column=1, value=f"姓名：{name}").font = SUB_FONT
        ws.cell(row=2, column=1).alignment = LEFT
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=4)
        ws.cell(row=2, column=5, value=f"工种：{proc_map.get(name, person['main_process'])}").font = SUB_FONT
        ws.cell(row=2, column=5).alignment = LEFT
        ws.cell(row=2, column=8, value=f"单价：{rate:.2f} 元/小时").font = SUB_FONT
        ws.cell(row=2, column=8).alignment = LEFT
        ws.cell(
            row=2,
            column=10,
            value=f"工时 {person['hours']:.2f} h｜工资 {person['amount']:.2f} 元",
        ).font = SUB_FONT
        ws.cell(row=2, column=10).alignment = LEFT

        for col, text in enumerate(WAGE_HEADERS, start=1):
            cell = ws.cell(row=3, column=col, value=text)
            cell.font = HEAD_FONT
            cell.fill = HEADER_FILL
            cell.border = BORDER
            cell.alignment = CENTER
        ws.row_dimensions[3].height = 22

        # 明细行只取「已入账」的那部分：预览模式下列数对不上是故意的，
        # 预览就是给个总量，不展开来源（与工时表保持同一行为）
        items = _person_items(db, ym, name) if not include_unverified else []

        r = 4
        total_hours = 0.0
        total_amount = 0.0
        for seq, it in enumerate(items, start=1):
            hours = float(it.total_hours or 0)
            amount = round(hours * rate, 2)
            ws.cell(row=r, column=1, value=seq)
            ws.cell(row=r, column=2, value=it.product_name)
            ws.cell(row=r, column=3, value=it.batch_no)
            ws.cell(row=r, column=4, value=it.part_name)
            ws.cell(row=r, column=5, value=it.process)
            ws.cell(row=r, column=6, value=it.qualified_qty)
            ws.cell(row=r, column=7, value=it.unit_hours)
            ws.cell(row=r, column=8, value=hours)
            date_cell = ws.cell(row=r, column=9, value=it.finish_date)
            date_cell.number_format = "yyyy-mm-dd"
            ws.cell(row=r, column=10, value=it.inspector)
            ws.cell(row=r, column=11, value=it.remark)
            frac = ws.cell(row=r, column=12, value=hours / 24)
            frac.number_format = "[h]:mm"
            ws.cell(row=r, column=13, value=rate)
            ws.cell(row=r, column=14, value=amount)
            total_hours += hours
            total_amount += amount

            for col in range(1, total_cols + 1):
                cell = ws.cell(row=r, column=col)
                cell.border = BORDER
                cell.font = CELL_FONT
                cell.alignment = CENTER if col != 11 else LEFT
            ws.cell(row=r, column=7).number_format = "0.00"
            ws.cell(row=r, column=8).number_format = "0.00"
            ws.cell(row=r, column=6).number_format = "0.##"
            ws.cell(row=r, column=13).number_format = "0.00"
            ws.cell(row=r, column=14).number_format = MONEY_FMT
            r += 1

        if include_unverified and not items:
            total_hours = float(person["hours"] or 0)
            total_amount = float(person["amount"] or 0)

        ws.cell(row=r, column=7, value="合计").font = HEAD_FONT
        ws.cell(row=r, column=7).alignment = CENTER
        total_cell = ws.cell(row=r, column=8, value=round(total_hours, 4))
        total_cell.font = HEAD_FONT
        total_cell.number_format = "0.00"
        total_cell.alignment = CENTER
        frac_cell = ws.cell(row=r, column=12, value=round(total_hours, 4) / 24)
        frac_cell.number_format = "[h]:mm"
        frac_cell.font = HEAD_FONT
        frac_cell.alignment = CENTER
        amt_cell = ws.cell(row=r, column=14, value=round(total_amount, 2))
        amt_cell.font = HEAD_FONT
        amt_cell.number_format = MONEY_FMT
        amt_cell.alignment = CENTER
        for col in range(1, total_cols + 1):
            ws.cell(row=r, column=col).border = BORDER

        widths = [6, 22, 12, 18, 12, 10, 10, 10, 12, 10, 20, 12, 10, 12]
        for col, width in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(col)].width = width
        ws.freeze_panes = "A4"

    # ---------------------------------------------------------- 总工时（原格式）
    ws_total = wb.create_sheet("总工时")
    ws_total.merge_cells("A1:E1")
    t = ws_total.cell(row=1, column=1, value=f"{year}年{month}月总工时")
    t.font = TITLE_FONT
    t.alignment = CENTER
    ws_total.row_dimensions[1].height = 26
    for col, text in enumerate(TOTAL_HEADERS, start=1):
        cell = ws_total.cell(row=3, column=col, value=text)
        cell.font = HEAD_FONT
        cell.fill = HEADER_FILL
        cell.border = BORDER
        cell.alignment = CENTER

    r = 4
    grand = 0.0
    for idx, person in enumerate(data["persons"], start=1):
        first = True
        procs = person["processes"] or [{"process": "", "hours": person["hours"]}]
        for proc in procs:
            if first:
                ws_total.cell(row=r, column=1, value=idx)
                ws_total.cell(row=r, column=2, value=person["worker_name"])
                first = False
            ws_total.cell(row=r, column=3, value=proc["process"])
            hours_cell = ws_total.cell(row=r, column=4, value=proc["hours"])
            hours_cell.number_format = "0.00"
            frac = ws_total.cell(row=r, column=5, value=(proc["hours"] or 0) / 24)
            frac.number_format = "[h]:mm"
            grand += proc["hours"] or 0
            for col in range(1, len(TOTAL_HEADERS) + 1):
                cell = ws_total.cell(row=r, column=col)
                cell.border = BORDER
                cell.font = CELL_FONT
                cell.alignment = CENTER
            r += 1

    ws_total.cell(row=r, column=2, value="总计").font = HEAD_FONT
    ws_total.cell(row=r, column=2).alignment = CENTER
    gcell = ws_total.cell(row=r, column=4, value=round(grand, 4))
    gcell.font = HEAD_FONT
    gcell.number_format = "0.00"
    gcell.alignment = CENTER
    gfrac = ws_total.cell(row=r, column=5, value=round(grand, 4) / 24)
    gfrac.number_format = "[h]:mm"
    gfrac.font = HEAD_FONT
    gfrac.alignment = CENTER
    for col in range(1, len(TOTAL_HEADERS) + 1):
        ws_total.cell(row=r, column=col).border = BORDER
    for col, width in zip(range(1, 6), [6, 14, 14, 12, 12]):
        ws_total.column_dimensions[get_column_letter(col)].width = width
    ws_total.freeze_panes = "A4"

    # ---------------------------------------------------------- 工资汇总（新增）
    ws_pay = wb.create_sheet("工资汇总")
    ws_pay.merge_cells("A1:G1")
    pt = ws_pay.cell(row=1, column=1, value=f"{year}年{month}月工时工资汇总")
    pt.font = TITLE_FONT
    pt.alignment = CENTER
    ws_pay.row_dimensions[1].height = 26
    for col, text in enumerate(WAGE_TOTAL_HEADERS, start=1):
        cell = ws_pay.cell(row=3, column=col, value=text)
        cell.font = HEAD_FONT
        cell.fill = HEADER_FILL
        cell.border = BORDER
        cell.alignment = CENTER

    r = 4
    for idx, person in enumerate(data["persons"], start=1):
        ws_pay.cell(row=r, column=1, value=idx)
        ws_pay.cell(row=r, column=2, value=person["worker_name"])
        ws_pay.cell(row=r, column=3, value=person["process"])
        hc = ws_pay.cell(row=r, column=4, value=person["hours"])
        hc.number_format = "0.00"
        fc = ws_pay.cell(row=r, column=5, value=person["hours"] / 24)
        fc.number_format = "[h]:mm"
        rc = ws_pay.cell(
            row=r, column=6, value=person["rate"] if person["has_rate"] else "未设单价"
        )
        if person["has_rate"]:
            rc.number_format = "0.00"
        rc.alignment = CENTER
        ac = ws_pay.cell(row=r, column=7, value=person["amount"])
        ac.number_format = MONEY_FMT
        for col in range(1, len(WAGE_TOTAL_HEADERS) + 1):
            cell = ws_pay.cell(row=r, column=col)
            cell.border = BORDER
            cell.font = CELL_FONT
            if col in (1, 2, 3, 4, 5):
                cell.alignment = CENTER
        r += 1

    ws_pay.cell(row=r, column=2, value="总计").font = HEAD_FONT
    ws_pay.cell(row=r, column=2).alignment = CENTER
    th = ws_pay.cell(row=r, column=4, value=data["totals"]["hours"])
    th.number_format = "0.00"
    th.font = HEAD_FONT
    th.alignment = CENTER
    tf = ws_pay.cell(row=r, column=5, value=data["totals"]["hours"] / 24)
    tf.number_format = "[h]:mm"
    tf.font = HEAD_FONT
    tf.alignment = CENTER
    ta = ws_pay.cell(row=r, column=7, value=data["totals"]["amount"])
    ta.number_format = MONEY_FMT
    ta.font = HEAD_FONT
    ta.alignment = CENTER
    for col in range(1, len(WAGE_TOTAL_HEADERS) + 1):
        ws_pay.cell(row=r, column=col).border = BORDER
    for col, width in zip(range(1, 8), [6, 14, 14, 12, 13, 12, 14]):
        ws_pay.column_dimensions[get_column_letter(col)].width = width
    ws_pay.freeze_panes = "A4"

    # ---------------------------------------------------------- 口径说明
    ws_note = wb.create_sheet("口径说明")
    notes = [
        ("数据来源", "工时识别核算系统自动生成"),
        ("生成时间", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("核算期", ym),
        ("含未确认单据", "是（仅供预览，请勿作为结算依据）" if include_unverified else "否（仅统计已核对确认的单据）"),
        ("工时口径", "报工单的合格件数 × 单件工时，由系统重算，不采用手写合计"),
        ("工资公式", "个人月工时 × 该月生效工时单价，按人四舍五入到分"),
        ("单价来源", "系统管理 → 工时单价（按生效月留档，改价不覆盖历史月份）"),
        ("单价算法", "取生效月 ≤ 核算月的最近一条记录"),
        ("未设单价", f"{data['totals']['missing_rate']} 人 / {data['totals']['missing_hours']:.2f} 小时（这些人金额为 0，需先设单价）"),
        ("合计金额", f"{data['totals']['amount']:.2f} 元"),
        ("合计工时", f"{data['totals']['hours']:.2f} 小时"),
        ("人员数 / 明细数", f"{data['totals']['persons']} / {data['totals']['items']}"),
        ("工时单位", "小时（小数）。原表按 Excel 时间值存储，超过 24 小时会显示为“N days”，本表已修正"),
        ("返修折算系数", f"{REWORK_FACTOR}（0 表示不计工时，也就不计工资）"),
        ("废品折算系数", f"{SCRAP_FACTOR}（0 表示不计工时，也就不计工资）"),
    ]
    for row, (k, v) in enumerate(notes, start=1):
        ws_note.cell(row=row, column=1, value=k).font = HEAD_FONT
        ws_note.cell(row=row, column=2, value=v).font = CELL_FONT
        ws_note.cell(row=row, column=2).alignment = LEFT
    ws_note.column_dimensions["A"].width = 20
    ws_note.column_dimensions["B"].width = 74

    suffix = "_预览" if include_unverified else ""
    path = Path(EXPORT_DIR) / f"{ym}工时工资表{suffix}_{dt.datetime.now():%Y%m%d%H%M%S}.xlsx"
    wb.save(path)
    return path
