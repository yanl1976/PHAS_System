"""月度汇总与勾稽校验。"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import RECONCILE_TOLERANCE
from ..models import Period, Sheet, WorkItem, Worker
from .rules import period_bounds


def _load_items(db: Session, ym: str, include_unverified: bool = False) -> list[tuple[WorkItem, Sheet | None]]:
    """取某核算期的全部报工明细。

    `Sheet` 是 **outer join**：扫码报工没有纸质单据（`sheet_id` 为空），
    它的核算期记在自己的 `period_id` 上。以前这里是 inner join，扫码报工
    的数据会一条都进不了工资表 —— 而且不报错，只是「少算了几个人的钱」，
    这种错误最难发现。
    """
    period_of = func.coalesce(Sheet.period_id, WorkItem.period_id)
    stmt = (
        select(WorkItem, Sheet)
        .outerjoin(Sheet, WorkItem.sheet_id == Sheet.id)
        .join(Period, Period.id == period_of)
        .where(Period.ym == ym)
        .options(selectinload(WorkItem.worker))
    )
    if not include_unverified:
        # 扫码报工没有单据状态可查：计划员扫码提交那一刻就已经核过签字了，
        # 它天然是「已入账」，不该被挂在「未确认单据」里等一张不存在的单。
        stmt = stmt.where(or_(Sheet.status == "verified", Sheet.id.is_(None)))
    else:
        # 账外只看「还等着入账的」，作废单不是待办 —— 它永远不会入账。
        # 把它算进「未确认入账工时」会让提示永远消不掉，人就不再看了。
        stmt = stmt.where(or_(Sheet.status != "rejected", Sheet.id.is_(None)))
    rows = db.execute(stmt).all()
    return [(r[0], r[1]) for r in rows]


def monthly_summary(db: Session, ym: str, include_unverified: bool = False) -> dict:
    """按人按工序汇总。include_unverified=True 时用于预览（含未确认单据）。"""
    pairs = _load_items(db, ym, include_unverified)

    per_person: dict[int | None, dict] = {}
    per_person_process: dict[tuple[int | None, str], float] = defaultdict(float)
    per_process: dict[str, float] = defaultdict(float)
    per_product: dict[str, float] = defaultdict(float)
    sheet_ids: set[int] = set()

    for item, sheet in pairs:
        wid = item.worker_id
        name = item.worker.name if item.worker else (item.worker_raw or "未匹配")
        # 扫码报工没有纸质单据。单据数与单据集合只统计真单据：
        # 把「扫码报工」也算成一张单，人就会以为这个月凭空多了几十张纸。
        sheet_id = sheet.id if sheet is not None else None
        if sheet_id is not None:
            sheet_ids.add(sheet_id)
        bucket = per_person.setdefault(
            wid if wid is not None else -1,
            {
                "worker_id": wid,
                "worker_name": name,
                "processes": defaultdict(float),
                "hours": 0.0,
                "item_count": 0,
                "sheet_count": set(),
                "qty": 0.0,
            },
        )
        bucket["hours"] = round(bucket["hours"] + item.total_hours, 4)
        bucket["item_count"] += 1
        if sheet_id is not None:
            bucket["sheet_count"].add(sheet_id)
        bucket["qty"] = round(bucket["qty"] + (item.qualified_qty or 0), 4)
        # 汇总按字典标准名归并：明细上可能写的是别名（「包板折弯」），
        # 那是给人对原图看的；工时账里必须和「折弯」记在同一列，
        # 否则同一道工序会被拆成几十个列
        proc = item.process_std or item.process or "未填"
        bucket["processes"][proc] = round(bucket["processes"].get(proc, 0.0) + item.total_hours, 4)
        per_person_process[(wid, proc)] += item.total_hours
        per_process[proc] += item.total_hours
        per_product[item.product_name or "未填"] += item.total_hours

    persons = []
    for bucket in per_person.values():
        bucket["sheet_count"] = len(bucket["sheet_count"])
        bucket["processes"] = [
            {"process": k, "hours": round(v, 4)} for k, v in sorted(bucket["processes"].items(), key=lambda x: -x[1])
        ]
        bucket["main_process"] = bucket["processes"][0]["process"] if bucket["processes"] else ""
        persons.append(bucket)
    persons.sort(key=lambda p: -p["hours"])

    total_hours = round(sum(p["hours"] for p in persons), 4)

    return {
        "ym": ym,
        "include_unverified": include_unverified,
        "persons": persons,
        "processes": [{"process": k, "hours": round(v, 4)} for k, v in sorted(per_process.items(), key=lambda x: -x[1])],
        "products": [{"product": k, "hours": round(v, 4)} for k, v in sorted(per_product.items(), key=lambda x: -x[1])][:20],
        "totals": {
            "hours": total_hours,
            "persons": len(persons),
            "items": sum(p["item_count"] for p in persons),
            "sheets": len(sheet_ids),
        },
    }


def reconcile(db: Session, ym: str) -> dict:
    """三级勾稽校验。

    逐级比对：明细行合计 -> 人员月合计 -> 全员总合计，并附带 6 项质量检查。

    **账内 / 账外分开判**：一张被驳回或还没确认的单据，它的工时没有入账，
    它的行缺人、缺维度都是「待办」而不是「账错了」。若把这类问题判成 error，
    一张作废单就能永久挡住整月导出，逼着人去关掉校验或强行导出 ——
    校验一旦能被绕过，等于没有校验。所以：
      · error 级（阻断导出）只针对**已确认**单据；
      · 未入账的问题单列成 `UNASSIGNED_OFFLINE` 等提示，提醒确认前补齐。
    """
    verified = _load_items(db, ym, include_unverified=False)
    all_pairs = _load_items(db, ym, include_unverified=True)
    verified_ids = {i.id for i, _s in verified}

    per_person_verified: dict[str, float] = defaultdict(float)
    per_person_items: dict[str, list[WorkItem]] = defaultdict(list)
    item_sum = 0.0
    computed_mismatch: list[dict] = []

    for item, _sheet in verified:
        key = item.worker.name if item.worker else (item.worker_raw or "未匹配")
        per_person_verified[key] = round(per_person_verified[key] + item.total_hours, 4)
        per_person_items[key].append(item)
        item_sum += item.total_hours
        expect = round((item.qualified_qty or 0) * (item.unit_hours or 0), 4)
        if abs(expect - item.total_hours) > 0.01:
            computed_mismatch.append(
                {"item_id": item.id, "sheet_id": item.sheet_id, "worker": key, "expect": expect, "actual": item.total_hours}
            )

    person_sum = round(sum(per_person_verified.values()), 4)
    diff = round(item_sum - person_sum, 4)

    # 检查 1：未确认/被驳回单据中的工时（未入账，但需提示）
    # 扫码报工没有单据可确认，它进来就是入账状态，所以排除掉（s 为 None）
    pending_pairs = [(i, s) for i, s in all_pairs if s is not None and s.status != "verified"]
    pending_hours = round(sum(i.total_hours for i, _s in pending_pairs), 4)

    # 检查 2：无人员归属的明细 —— 账内是错账（阻断），账外是待办（提示）
    unassigned_all = [
        {"item_id": i.id, "sheet_id": i.sheet_id, "worker_raw": i.worker_raw, "hours": i.total_hours}
        for i, _s in all_pairs
        if not i.worker_id
    ]
    unassigned = [x for x in unassigned_all if x["item_id"] in verified_ids]
    unassigned_offline = [x for x in unassigned_all if x["item_id"] not in verified_ids]

    # 检查 3：跨期日期（只判账内：账外单据还没入账，日期跨不跨期不影响本月账）
    start, end = period_bounds(ym)
    out_of_period = [
        {"item_id": i.id, "sheet_id": i.sheet_id, "date": str(i.finish_date), "worker": i.worker_raw}
        for i, _s in verified
        if i.finish_date and not (start <= i.finish_date <= end)
    ]

    # 检查 4：疑似重复（同人 + 同件名 + 同工序 + 同日 + 同单件工时）
    seen: dict[tuple, list[int]] = defaultdict(list)
    for item, _s in verified:
        key = (
            item.worker_id,
            item.part_name,
            item.process,
            str(item.finish_date),
            item.unit_hours,
        )
        seen[key].append(item.id)
    duplicates = [
        {"key": list(map(str, k)), "item_ids": v, "count": len(v)}
        for k, v in seen.items()
        if len(v) > 1 and any(x for x in k)
    ]

    # 检查 5：合格件数为 0 但填了单件工时的行（工时被静默计为 0，只判账内）
    zero_qty = [
        {"item_id": i.id, "sheet_id": i.sheet_id, "worker": i.worker_raw, "unit_hours": i.unit_hours}
        for i, _s in verified
        if i.unit_hours and not (i.qualified_qty or 0)
    ]

    # 检查 6：工时异常行（只判账内）
    abnormal = [
        {"item_id": i.id, "unit_hours": i.unit_hours, "total_hours": i.total_hours}
        for i, _s in verified
        if (i.total_hours or 0) > 400
    ]

    # 检查 7：已入账但归集维度不全（产品 / 件名 / 工序）。
    # 确认时已经拦过工序为空，能进账的只剩历史遗留；不列出来，
    # 汇总里那列「未填」就永远没人认领。
    dim_missing = [
        {
            "item_id": i.id,
            "sheet_id": i.sheet_id,
            "seq": i.seq,
            "lack": [
                name
                for name, value in (
                    ("产品", i.product_name),
                    ("件名", i.part_name),
                    ("工序", i.process_std or i.process),
                )
                if not (value or "").strip()
            ],
            "hours": i.total_hours,
        }
        for i, _s in verified
        if not (i.product_name or "").strip()
        or not (i.part_name or "").strip()
        or not ((i.process_std or i.process or "").strip())
    ]

    issues = []
    if abs(diff) > RECONCILE_TOLERANCE:
        issues.append({"level": "error", "code": "ITEM_PERSON_MISMATCH", "message": f"明细合计与人员合计差 {diff} 小时"})
    if computed_mismatch:
        issues.append({"level": "error", "code": "CALC_MISMATCH", "message": f"{len(computed_mismatch)} 行合计工时 ≠ 件数×单件"})
    if unassigned:
        issues.append({"level": "error", "code": "UNASSIGNED", "message": f"{len(unassigned)} 行无人员归属（已入账，必须补人）"})
    if unassigned_offline:
        issues.append({
            "level": "warn",
            "code": "UNASSIGNED_OFFLINE",
            "message": f"{len(unassigned_offline)} 行未入账单缺人员归属（不影响本月入账，确认前需补齐）",
        })
    if pending_hours > 0:
        issues.append({"level": "warn", "code": "PENDING", "message": f"尚有 {pending_hours} 小时未确认入账"})
    if out_of_period:
        issues.append({"level": "warn", "code": "OUT_OF_PERIOD", "message": f"{len(out_of_period)} 行日期跨期"})
    if zero_qty:
        issues.append({"level": "warn", "code": "ZERO_QTY", "message": f"{len(zero_qty)} 行有单件工时但合格件数为 0"})
    if duplicates:
        issues.append({"level": "warn", "code": "DUPLICATE", "message": f"{len(duplicates)} 组疑似重复明细"})
    if abnormal:
        issues.append({"level": "warn", "code": "ABNORMAL", "message": f"{len(abnormal)} 行合计工时异常偏高"})
    if dim_missing:
        hours = round(sum(x["hours"] or 0 for x in dim_missing), 4)
        issues.append(
            {
                "level": "warn",
                "code": "DIM_MISSING",
                "message": f"{len(dim_missing)} 行已入账但缺产品/件名/工序（{hours} 小时落入「未填」）",
            }
        )

    balanced = not any(i["level"] == "error" for i in issues)

    return {
        "ym": ym,
        "balanced": balanced,
        "tolerance": RECONCILE_TOLERANCE,
        "levels": {
            "detail_sum": round(item_sum, 4),
            "person_sum": person_sum,
            "grand_total": round(sum(per_person_verified.values()), 4),
            "diff": diff,
        },
        "per_person": [
            {"worker": k, "hours": round(v, 4), "items": len(per_person_items[k])}
            for k, v in sorted(per_person_verified.items(), key=lambda x: -x[1])
        ],
        "checks": {
            "pending_hours": pending_hours,
            "unassigned": unassigned[:50],
            "unassigned_offline": unassigned_offline[:50],
            "out_of_period": out_of_period[:50],
            "zero_qty": zero_qty[:50],
            "duplicates": duplicates[:50],
            "abnormal": abnormal[:50],
            "dim_missing": dim_missing[:50],
            "calc_mismatch": computed_mismatch[:50],
        },
        "issues": issues,
    }


def worker_process_map(db: Session, ym: str) -> dict[str, str]:
    """人员 -> 工种文本（用于导出表头）。"""
    pairs = _load_items(db, ym, include_unverified=True)
    procs: dict[str, set[str]] = defaultdict(set)
    worker_ids: dict[str, int] = {}
    for item, _s in pairs:
        key = item.worker.name if item.worker else (item.worker_raw or "未匹配")
        if item.process_std or item.process:
            procs[key].add(item.process_std or item.process)
        if item.worker_id:
            worker_ids[key] = item.worker_id
    result = {}
    for name, ps in procs.items():
        wid = worker_ids.get(name)
        default = ""
        if wid:
            w = db.get(Worker, wid)
            default = w.default_process if w else ""
        result[name] = default or "/".join(sorted(ps))
    return result
