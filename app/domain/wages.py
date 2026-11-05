"""工资核算：月度工时 × 工时单价 = 工时工资。

口径只在这里定义一次，页面、导出、测试都从这里取数：

- 工时取 `monthly_summary()` 的结果 —— 也就是报工单上「合格件数 × 单件工时」
  重算出来的合计工时，不是手写合计。工资必须建立在同一份工时上，否则
  「汇总说 180 小时、工资说 176 小时」这种账在车间里永远说不清。
- 单价按 `worker_rate` 的**生效月**取：核算月及之前最近的一条。
  没有记录的就是「未设单价」，单独统计，绝不静默按 0 元发工资。
- 金额保留 2 位小数（分），四舍五入在「人」这一级做；合计由各人金额相加，
  不拿总工时乘平均单价 —— 那样加起来会对不上分。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Period, Sheet, Worker, WorkerRate
from .aggregation import monthly_summary, worker_process_map


def rate_index(db: Session, ym: str) -> dict[int, float]:
    """(人员 → 该核算月生效的时薪)。同一人有多条时取生效月最近的一条。"""
    rows = db.scalars(
        select(WorkerRate)
        .where(WorkerRate.effective_ym <= ym)
        .order_by(WorkerRate.worker_id, WorkerRate.effective_ym)
    ).all()
    out: dict[int, float] = {}
    for r in rows:
        # 按 effective_ym 升序遍历，后面的自然覆盖前面的 = 最近生效的那条
        out[r.worker_id] = float(r.rate or 0.0)
    return out


def rate_cards(db: Session, ym: str) -> list[dict]:
    """单价设置卡片的数据：所有在用人员 + 当前生效单价 + 最近一次调整。"""
    workers = db.scalars(
        select(Worker).where(Worker.active == True).order_by(Worker.name)  # noqa: E712
    ).all()
    rows = db.scalars(
        select(WorkerRate).where(WorkerRate.effective_ym <= ym)
    ).all()
    latest: dict[int, WorkerRate] = {}
    for r in rows:
        cur = latest.get(r.worker_id)
        if cur is None or r.effective_ym >= cur.effective_ym:
            latest[r.worker_id] = r
    out = []
    for w in workers:
        hit = latest.get(w.id)
        out.append(
            {
                "worker_id": w.id,
                "name": w.name,
                "employee_no": w.employee_no,
                "team": w.team,
                "process": w.default_process,
                "rate": round(float(hit.rate or 0.0), 2) if hit else 0.0,
                "effective_ym": hit.effective_ym if hit else "",
                "has_rate": bool(hit and (hit.rate or 0) > 0),
                "updated_at": hit.updated_at.isoformat(timespec="seconds") if hit else None,
                "note": hit.note if hit else "",
            }
        )
    # 未设单价的排前面（那才是要动手填的），同组按车间 → 工种 → 姓名：
    # 同一工种往往同一价，挨着填不容易填错
    out.sort(key=lambda x: (x["has_rate"], x["team"] or "", x["process"] or "", x["name"]))
    return out


def compute_wages(db: Session, ym: str, include_unverified: bool = False) -> dict:
    summary = monthly_summary(db, ym, include_unverified=include_unverified)
    rates = rate_index(db, ym)
    proc_map = worker_process_map(db, ym)

    persons: list[dict] = []
    for p in summary["persons"]:
        wid = p.get("worker_id")
        rate = round(float(rates.get(wid, 0.0)), 2) if wid else 0.0
        hours = round(float(p["hours"] or 0.0), 4)
        amount = round(hours * rate, 2)
        persons.append(
            {
                "worker_id": wid,
                "worker_name": p["worker_name"],
                "process": proc_map.get(p["worker_name"], p.get("main_process") or ""),
                "main_process": p.get("main_process") or "",
                "processes": p.get("processes") or [],
                "hours": hours,
                "rate": rate,
                "amount": amount,
                "item_count": p.get("item_count", 0),
                "sheet_count": p.get("sheet_count", 0),
                "qty": p.get("qty", 0.0),
                # 没有单价 ≠ 工资 0 元。界面和导出都要显式标出来，
                # 否则这份工资表看起来「算完了」，其实是漏了人。
                "has_rate": rate > 0,
                "unmatched": wid is None,
            }
        )

    persons.sort(key=lambda x: (-x["amount"], -x["hours"], x["worker_name"]))
    total_hours = round(sum(x["hours"] for x in persons), 4)
    total_amount = round(sum(x["amount"] for x in persons), 2)
    missing = [x for x in persons if not x["has_rate"]]
    # 还有没确认入账的报工单时，这份工资表就是「半成品」：
    # 界面与导出都要提示，否则车间照它发钱，月底补单就变成了打不完的补丁。
    # 只算「待核对」：作废（rejected）是**已经处理完**的结果，不是待办，
    # 把它算进来会让一张废单永久挂着「未确认」的横幅。
    pending_sheets = (
        db.scalar(
            select(func.count())
            .select_from(Sheet)
            .join(Period, Sheet.period_id == Period.id)
            .where(Period.ym == ym, Sheet.status == "recognized")
        )
        or 0
    )
    return {
        "ym": ym,
        "include_unverified": include_unverified,
        "persons": persons,
        "totals": {
            "persons": len(persons),
            "hours": total_hours,
            "amount": total_amount,
            # 加权平均单价：只作参考，合计金额绝不按它反算
            "avg_rate": round(total_amount / total_hours, 2) if total_hours else 0.0,
            "missing_rate": len(missing),
            "missing_hours": round(sum(x["hours"] for x in missing), 4),
            "pending_sheets": int(pending_sheets),
            "items": summary["totals"]["items"],
            "sheets": summary["totals"]["sheets"],
        },
        "missing": [
            {"worker_name": x["worker_name"], "hours": x["hours"]} for x in missing
        ],
    }
