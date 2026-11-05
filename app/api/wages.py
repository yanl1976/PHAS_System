"""工资核算接口：单价设置 + 月度工资 + 工资表导出。

为什么单价单独一张表、单独一组接口：
单价是**工资口径**，不是人员属性。车间调薪、临时补差、按年调整，
每一次都必须在历史月份里留下痕迹（见 models.WorkerRate 的注释）。
把它塞在 worker 上，改一次全体历史工资表就跟着变，那就不是账了。
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.aggregation import reconcile
from ..domain.export_excel import build_wage_workbook
from ..domain.wages import compute_wages, rate_cards
from ..models import AuditLog, Period, Worker, WorkerRate
from ..security import current_operator
from ..domain.sys_roles import PERM_WAGE_RATE
from .auth import require_perm

router = APIRouter(prefix="/api/wages", tags=["wages"])


def _check_period(db: Session, ym: str) -> None:
    if not db.scalar(select(Period).where(Period.ym == ym)):
        raise HTTPException(404, f"核算期 {ym} 不存在")


@router.get("")
def wages(ym: str, include_unverified: bool = False, db: Session = Depends(get_db)):
    """按核算月出工资：人 × 工时 × 该月生效单价。"""
    _check_period(db, ym)
    data = compute_wages(db, ym, include_unverified=include_unverified)
    period = db.scalar(select(Period).where(Period.ym == ym))
    data["period_status"] = period.status
    return data


@router.get("/rates")
def list_rates(ym: str, db: Session = Depends(get_db)):
    """单价设置卡片：所有在用人员 + 该月生效单价。"""
    return {"ym": ym, "items": rate_cards(db, ym)}


class RateItem(BaseModel):
    worker_id: int
    rate: float
    note: str = ""


class RatesIn(BaseModel):
    ym: str
    items: list[RateItem]
    note: str = ""


@router.put("/rates")
def save_rates(data: RatesIn, db: Session = Depends(get_db)):
    """按「生效月」写入单价。

    同一人同一月只留一条：再存就是改这一条（并留痕），不会堆出两条互相矛盾的价。
    改价不应该去动别的月份 —— 8 月发过的工资不能因为 9 月调价而变。
    """
    require_perm(db, PERM_WAGE_RATE)
    ym = (data.ym or "").strip()
    if len(ym) != 7 or ym[4] != "-":
        raise HTTPException(400, "生效月格式应为 YYYY-MM")
    # 已锁账的月份不能改单价：工资表已经按原价发下去了，
    # 改一个字，那个月的账和发出去的钱就对不上，而且没人看得出来。
    period = db.scalar(select(Period).where(Period.ym == ym))
    if period is not None and period.status == "locked":
        raise HTTPException(400, f"{ym} 已锁账，不能再调单价；请先解锁核算期")
    if not data.items:
        raise HTTPException(400, "没有要保存的单价")
    user = current_operator("system")

    saved = 0
    changed = 0
    for item in data.items:
        worker = db.get(Worker, item.worker_id)
        if worker is None:
            raise HTTPException(404, f"人员 #{item.worker_id} 不存在")
        rate = round(float(item.rate or 0), 2)
        if rate < 0:
            raise HTTPException(400, f"「{worker.name}」的单价不能为负数")
        row = db.scalar(
            select(WorkerRate).where(
                WorkerRate.worker_id == item.worker_id,
                WorkerRate.effective_ym == ym,
            )
        )
        if row is None:
            row = WorkerRate(
                worker_id=item.worker_id,
                effective_ym=ym,
                rate=rate,
                note=(item.note or data.note or "")[:64],
                updated_at=dt.datetime.now(),
            )
            db.add(row)
            db.flush()
            db.add(
                AuditLog(
                    entity="worker_rate",
                    entity_id=row.id,
                    field=f"rate@{ym}",
                    old_value="",
                    new_value=f"{rate:.2f}",
                    user=user,
                )
            )
            saved += 1
            continue
        if abs(float(row.rate or 0) - rate) > 1e-9:
            db.add(
                AuditLog(
                    entity="worker_rate",
                    entity_id=row.id,
                    field=f"rate@{ym}",
                    old_value=f"{float(row.rate or 0):.2f}",
                    new_value=f"{rate:.2f}",
                    user=user,
                )
            )
            row.rate = rate
            row.updated_at = dt.datetime.now()
            if item.note or data.note:
                row.note = (item.note or data.note)[:64]
            changed += 1
        saved += 1
    db.commit()
    return {"ok": True, "ym": ym, "saved": saved, "changed": changed}


@router.get("/export")
def export_wages(
    ym: str,
    include_unverified: bool = False,
    preview: bool = False,
    db: Session = Depends(get_db),
):
    """导出工时工资表：版式同《个人完成工时汇总表》，右侧追加单价与金额。

    **导出就是发钱的依据**，所以与工时汇总表用同一条口径：勾稽不过不许导
    （`/api/export` 早就是这么做的，工资表以前却照样能导 —— 那是两套标准）。

    三种情况必须拦住，它们都会让「算出来的钱」比「该发的钱」少：
      · 勾稽有 error：明细合计 ≠ 人员合计、行合计 ≠ 件数×单件、账内有行没归属人
      · 还有待核对的识别单：那些工时还没进账，导出来就是半成品
      · 有人没设单价：他的工时按 0 元算，工资表上看不出少了谁

    只想先看版式/核对数据时加 `preview=1`，与汇总导出同一个约定。
    """
    _check_period(db, ym)
    if not preview:
        report = reconcile(db, ym)
        data = compute_wages(db, ym)
        problems: list[str] = []
        if not report["balanced"]:
            errs = [i["message"] for i in report["issues"] if i["level"] == "error"]
            problems.append("勾稽校验未通过（" + "；".join(errs[:3]) + "）")
        pending = int(data["totals"]["pending_sheets"] or 0)
        if pending:
            problems.append(f"还有 {pending} 张识别单待核对，这些工时没进账")
        missing = int(data["totals"]["missing_rate"] or 0)
        if missing:
            problems.append(
                f"{missing} 人没设工时单价（{data['totals']['missing_hours']}h 会算成 0 元）"
            )
        if problems:
            raise HTTPException(
                400,
                "工资表还不能导出：" + "；".join(problems) + "。处理完再导，"
                "或加 preview=1 导出预览版（仅用于核对，不能作为发放依据）",
            )
    path = build_wage_workbook(db, ym, include_unverified=include_unverified)
    return FileResponse(
        str(path),
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
