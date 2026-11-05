"""字典与统计接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import (
    CONF_HIGH,
    CONF_MID,
    ENABLE_AMOUNT,
    HOURS_PER_DAY,
    REWORK_FACTOR,
    RECONCILE_TOLERANCE,
    SCRAP_FACTOR,
    UNIT_HOURS_LIMIT,
    UNIT_HOURS_WARN,
)
from ..db import get_db
from ..domain import sys_roles
from ..models import Batch, Image, Period, ProcessDict, Sheet, Worker, WorkItem

# 这里**不再** `from ..ocr.engine import engine_status`。
# C1 之后引擎只活在独立 OCR 进程里，Web 进程调 engine_status() 永远得到
# loaded=False —— 一个恒假的字段，比没有更糟（顶栏会一直报「引擎异常」）。
# 引擎与队列的真实状态统一走 /api/health（它读 worker 的心跳文件）。

router = APIRouter(prefix="/api", tags=["dicts"])


class WorkerIn(BaseModel):
    name: str
    aliases: str = ""
    team: str = "2车间"
    default_process: str = ""
    is_inspector: bool = False


class ProcessIn(BaseModel):
    code: str
    name: str
    aliases: str = ""
    category: str = ""


@router.get("/dicts")
def get_dicts(db: Session = Depends(get_db)):
    workers = db.scalars(select(Worker).order_by(Worker.id)).all()
    processes = db.scalars(select(ProcessDict).order_by(ProcessDict.sort_order)).all()

    # 人员可能身兼多职（既是工作者又是检验员），前端下拉要按角色 kind 过滤，
    # 不能只看 is_inspector 布尔——那会把「马永刚」这类兼任者整个排除掉，
    # 结果 el-select 找不到选项，回显成裸 id「13」。
    from ..models import Role, WorkerRole

    kind_of_role = dict(db.execute(select(Role.id, Role.kind)).all())
    duties_of_role = dict(db.execute(select(Role.id, Role.duties)).all())
    kinds_by_worker: dict[int, set[str]] = {}
    role_ids_by_worker: dict[int, list[int]] = {}
    duties_by_worker: dict[int, set[str]] = {}
    for wid, rid in db.execute(select(WorkerRole.worker_id, WorkerRole.role_id)).all():
        role_ids_by_worker.setdefault(wid, []).append(rid)
        kind = kind_of_role.get(rid)
        if kind:
            kinds_by_worker.setdefault(wid, set()).add(kind)
        for code in sys_roles.duties_of_role(duties_of_role.get(rid) or ""):
            duties_by_worker.setdefault(wid, set()).add(code)

    def _kinds(w: Worker) -> list[str]:
        kinds = kinds_by_worker.get(w.id)
        if kinds:
            return sorted(kinds)
        # 与 services/dicts.py 的识别名册保持同一套兜底口径
        return ["inspector"] if w.is_inspector else ["worker"]

    return {
        "workers": [
            {
                "id": w.id,
                "name": w.name,
                "aliases": w.aliases,
                "team": w.team,
                "default_process": w.default_process,
                "is_inspector": w.is_inspector,
                "active": w.active,
                "kinds": _kinds(w),
                # 岗位与岗位职能：账号弹窗里选人后就能看出「这个人能承担哪些功能」
                "role_ids": role_ids_by_worker.get(w.id, []),
                "duties": [c for c in sys_roles.DUTY_CODES if c in duties_by_worker.get(w.id, ())],
            }
            for w in workers
        ],
        "processes": [
            {
                "id": p.id,
                "code": p.code,
                "name": p.name,
                "aliases": p.aliases,
                "category": p.category,
            }
            for p in processes
        ],
        "config": {
            "hours_per_day": HOURS_PER_DAY,
            "rework_factor": REWORK_FACTOR,
            "scrap_factor": SCRAP_FACTOR,
            "enable_amount": ENABLE_AMOUNT,
            "reconcile_tolerance": RECONCILE_TOLERANCE,
            "unit_hours_limit": UNIT_HOURS_LIMIT,
            "unit_hours_warn": UNIT_HOURS_WARN,
            "conf_high": CONF_HIGH,
            "conf_mid": CONF_MID,
        },
    }


@router.post("/workers")
def create_worker(data: WorkerIn, db: Session = Depends(get_db)):
    """旧接口保留兼容；角色统一在 /api/admin/workers 维护。"""
    if db.scalar(select(Worker).where(Worker.name == data.name)):
        raise HTTPException(400, "人员已存在")
    w = Worker(**data.model_dump())
    db.add(w)
    db.flush()
    role_code = "INSPECTOR" if data.is_inspector else "WORKER"
    from ..models import Role, WorkerRole

    role = db.scalar(select(Role).where(Role.code == role_code))
    if role:
        db.add(WorkerRole(worker_id=w.id, role_id=role.id))
    db.commit()
    db.refresh(w)
    return {"id": w.id}


@router.post("/processes")
def create_process(data: ProcessIn, db: Session = Depends(get_db)):
    if db.scalar(select(ProcessDict).where(ProcessDict.code == data.code)):
        raise HTTPException(400, "工序编码已存在")
    p = ProcessDict(**data.model_dump(), count_hours=True, sort_order=999)
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"id": p.id}


@router.get("/stats")
def stats(ym: str | None = None, db: Session = Depends(get_db)):
    period = None
    if ym:
        period = db.scalar(select(Period).where(Period.ym == ym))
    else:
        period = db.scalar(select(Period).order_by(Period.ym.desc()))

    if not period:
        return {"period": None, "counts": {}, "hours": {}}

    sheet_q = select(Sheet.status, func.count(Sheet.id)).where(Sheet.period_id == period.id).group_by(Sheet.status)
    counts = {status: cnt for status, cnt in db.execute(sheet_q).all()}

    img_q = select(Image.status, func.count(Image.id)).join(Batch, Image.batch_id == Batch.id).where(
        Batch.period_id == period.id
    ).group_by(Image.status)
    img_counts = {status: cnt for status, cnt in db.execute(img_q).all()}

    # 报工明细有两条来源：识别进来的（挂在 sheet 上）和扫码报工进来的
    # （sheet_id 为空，核算期记在自己身上）。这里必须 outer join + coalesce，
    # 否则顶栏会把扫码工时算成 0 —— 人刚报完工，抬头一看「已确认工时 0 h」，
    # 只会得出「系统没记账」的结论，然后再报一遍。
    period_of = func.coalesce(Sheet.period_id, WorkItem.period_id)
    verified_cond = or_(Sheet.status == "verified", Sheet.id.is_(None))
    verified_hours = float(
        db.scalar(select(func.coalesce(func.sum(WorkItem.total_hours), 0.0))
                  .select_from(WorkItem)
                  .outerjoin(Sheet, WorkItem.sheet_id == Sheet.id)
                  .where(period_of == period.id, verified_cond))
        or 0.0
    )
    pending_hours = float(
        db.scalar(
            select(func.coalesce(func.sum(WorkItem.total_hours), 0.0))
            .select_from(WorkItem)
            .outerjoin(Sheet, WorkItem.sheet_id == Sheet.id)
            .where(
                period_of == period.id,
                Sheet.id.is_not(None),
                Sheet.status != "verified",
            )
        )
        or 0.0
    )
    persons = db.scalar(
        select(func.count(func.distinct(WorkItem.worker_id)))
        .select_from(WorkItem)
        .outerjoin(Sheet, WorkItem.sheet_id == Sheet.id)
        .where(period_of == period.id, verified_cond)
    )
    scan_rows = int(
        db.scalar(
            select(func.count(WorkItem.id)).where(
                WorkItem.period_id == period.id,
                WorkItem.sheet_id.is_(None),
                WorkItem.source == "scan",
            )
        )
        or 0
    )

    return {
        "period": {"id": period.id, "ym": period.ym, "title": period.title, "status": period.status},
        "counts": {
            "verified": counts.get("verified", 0),
            "recognized": counts.get("recognized", 0),
            "rejected": counts.get("rejected", 0),
            "scan_items": scan_rows,
            "images_pending": img_counts.get("pending", 0),
            "images_processing": img_counts.get("processing", 0),
            "images_failed": img_counts.get("failed", 0),
            "images_done": img_counts.get("done", 0),
        },
        "hours": {
            "verified": round(verified_hours, 2),
            "pending": round(pending_hours, 2),
            "persons": int(persons or 0),
        },
    }
