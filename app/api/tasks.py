"""任务层接口：订单 → 工艺路线 → 实报。

任务不是单据的影子，而是业务锚点：一个订单拆成多张加工路线单，单据可以重传作废，
订单号不变。任务下的工艺路线默认**引用**基线（多任务共用一份），需要改才 fork 副本，
所以这里没有「每建一个任务复制一次工艺」的入口 —— 那样复制几轮，标准就不成其标准了。
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.sys_roles import DUTY_TASK_PLAN
from .auth import require_perm
from ..security import current_operator
from ..models import ProcessRoute, Sheet, StandardHour, TaskRoute, WorkTask
from ..services import routes as route_service
from ..services import tasks as task_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


# ------------------------------------------------------------------ 入参
class RouteSpec(BaseModel):
    product: str = ""
    part: str = ""
    route_id: int | None = None
    batch_no: str = ""
    qty: float = 0
    origin: str = "baseline"
    note: str = ""


class TaskCreate(BaseModel):
    task_no: str = ""
    title: str = ""
    customer: str = ""
    product: str = ""
    batch_no: str = ""
    period_id: int | None = None
    qty: float = 0
    order_date: str = ""
    due_date: str = ""
    status: str = "open"
    source: str = "manual"
    remark: str = ""
    created_by: str = "operator"
    routes: list[RouteSpec] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    task_no: str | None = None
    title: str | None = None
    customer: str | None = None
    qty: float | None = None
    period_id: int | None = None
    order_date: str | None = None
    due_date: str | None = None
    status: str | None = None
    remark: str | None = None
    # 谁改的：修订记录要写进版本，事后追人靠它
    user: str = "operator"
    # 改口径时可附一句说明（如「客户追加 20 台」），进修订记录
    note: str = ""


class RouteAdd(BaseModel):
    items: list[RouteSpec] = Field(default_factory=list)
    create_baseline: bool = True
    user: str = "operator"


class StepPayload(BaseModel):
    seq: int = 0
    process_code: str = ""
    process_name: str = ""
    op_type: str = "self"
    category: str = ""
    work_center: str = ""
    setup_hours: float = 0
    unit_hours: float = 0
    hours_src: str = "manual"
    samples: int = 0
    is_key: bool = False
    inspect_required: bool = False
    sop: str = ""
    remark: str = ""


class RouteStepsIn(BaseModel):
    steps: list[StepPayload] = Field(default_factory=list)
    # True = 保存并发布（基线会升版、立即生效）；False = 只存草稿，现行基准不动
    publish: bool = True
    user: str = "operator"


class AckIn(BaseModel):
    # 不传 tr_id = 确认本任务下全部待确认引用
    tr_id: int | None = None
    # adopt = 采用新版（切到现行已发布版）；keep = 本单保留现版（转成本单副本）
    mode: str = "adopt"
    user: str = "operator"


# ------------------------------------------------------------------ 列表 / 新建
@router.get("")
def list_tasks(
    q: str = "",
    status: str = "",
    product: str = "",
    origin: str = "",
    only_no_route: bool = False,
    only_no_sheet: bool = False,
    page: int = Query(1, ge=1),
    size: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """任务（订单）列表。路线数、工时、单据数都是一次批量算出来的。"""
    return task_service.task_rows(
        db,
        q=q,
        status=status,
        product=product,
        origin=origin,
        only_no_route=only_no_route,
        only_no_sheet=only_no_sheet,
        limit=size,
        offset=(page - 1) * size,
    )


@router.post("")
def create_task(data: TaskCreate, db: Session = Depends(get_db)):
    """手工建任务。可以先建空任务，也可以一次带上要复用的工艺路线。"""
    require_perm(db, DUTY_TASK_PLAN)
    if data.task_no.strip():
        exists = db.scalars(
            select(WorkTask).where(WorkTask.task_no == data.task_no.strip())
        ).first()
        if exists is not None:
            raise HTTPException(400, f"任务号 {data.task_no} 已存在")

    try:
        task = task_service.create_task(
            db,
            task_no=data.task_no,
            title=data.title,
            customer=data.customer,
            product=data.product,
            batch_no=data.batch_no,
            period_id=data.period_id,
            qty=data.qty,
            order_date=data.order_date,
            due_date=data.due_date,
            status=data.status,
            source=data.source,
            remark=data.remark,
            created_by=data.created_by,
            routes=[r.model_dump() for r in data.routes],
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return {"ok": True, "task": task_service.task_detail(db, task)}


@router.get("/todos")
def todos(db: Session = Depends(get_db)):
    """业务待办：每加一条规则就会产生一类「不满足规则的数据」。

    必须注册在 `/{task_id}` 之前，否则 "todos" 会被当成 task_id 解析。
    """
    return task_service.business_todos(db)


@router.post("/rebuild")
def rebuild(db: Session = Depends(get_db)):
    """按现有单据重算任务归集与聚合。幂等。"""
    require_perm(db, DUTY_TASK_PLAN)
    out = task_service.rebuild_tasks(db)
    db.commit()
    return out


# ------------------------------------------------------------------ 详情 / 修改
@router.get("/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db)):
    """任务详情：路线清单（含与基线的差异）、单据清单、汇总。"""
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task_service.task_detail(db, task)


@router.patch("/{task_id}")
def patch_task(task_id: int, data: TaskUpdate, db: Session = Depends(get_db)):
    """改任务基本信息。改了口径（数量/交期/期间/客户/任务号…）会升版并留痕。"""
    require_perm(db, DUTY_TASK_PLAN)
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    payload = data.model_dump(exclude_unset=True)
    user = str(payload.pop("user", "") or "operator")
    note = str(payload.pop("note", "") or "")
    try:
        meta = task_service.update_task(db, task, payload, user=user, note=note)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return {"ok": True, "task": task_service.task_detail(db, task), **meta}


@router.get("/{task_id}/revisions")
def task_revisions(task_id: int, db: Session = Depends(get_db)):
    """任务基本信息修订史（新版本在前）。"""
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return {
        "task_id": task.id,
        "version": int(task.version or 1),
        "items": task_service.task_revisions(db, task.id),
    }


@router.delete("/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    """删任务。挂着单据的任务不能删 —— 单据的归属会变成悬空。"""
    require_perm(db, DUTY_TASK_PLAN)
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    attached = db.scalars(select(Sheet.id).where(Sheet.task_id == task.id).limit(1)).first()
    if attached is not None:
        raise HTTPException(400, "该任务下还有单据，不能删除；先把单据改挂到别的任务")
    out = task_service.delete_task(db, task)
    db.commit()
    return {"ok": True, **out}


@router.post("/{task_id}/rebuild")
def rebuild_one(task_id: int, db: Session = Depends(get_db)):
    require_perm(db, DUTY_TASK_PLAN)
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    out = task_service.refresh_task(db, task)
    db.commit()
    return {"ok": True, **out}


# ------------------------------------------------------------------ 路线
@router.post("/{task_id}/routes")
def add_routes(task_id: int, data: RouteAdd, db: Session = Depends(get_db)):
    """把已有工艺路线挂到任务里（默认引用基线，不复制）。

    没有基线的产品×件名，`create_baseline=true` 时会自动建一条草稿基线再引用 ——
    新品第一次投产不该被「还没有标准工艺」挡在门外。
    """
    require_perm(db, DUTY_TASK_PLAN)
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    try:
        out = task_service.add_routes(
            db,
            task,
            [r.model_dump() for r in data.items],
            created_by=current_operator(data.user or "operator"),
            create_baseline=data.create_baseline,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return {"ok": True, **out, "task": task_service.task_detail(db, task)}


@router.delete("/{task_id}/routes/{tr_id}")
def remove_route(task_id: int, tr_id: int, db: Session = Depends(get_db)):
    """从任务里摘掉一条路线。引用只解除关联；本单副本才会连带删除。"""
    require_perm(db, DUTY_TASK_PLAN)
    tr = db.get(TaskRoute, tr_id)
    if tr is None or tr.task_id != task_id:
        raise HTTPException(404, "任务下没有这条路线")
    try:
        out = task_service.remove_route(db, tr)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return {"ok": True, **out}


@router.post("/{task_id}/routes/{tr_id}/fork")
def fork_route(task_id: int, tr_id: int, user: str = "operator", db: Session = Depends(get_db)):
    """把引用拆成本单副本：之后改它不影响基线，改基线也不影响本单。

    什么时候用：这单的工艺确实跟标准不一样。fork 之前一直引用着，
    标准一改本单跟着变，已经干完的活就说不清按哪版做的。
    """
    require_perm(db, DUTY_TASK_PLAN)
    tr = db.get(TaskRoute, tr_id)
    if tr is None or tr.task_id != task_id:
        raise HTTPException(404, "任务下没有这条路线")
    try:
        copy = task_service.fork_route(db, tr, created_by=current_operator(user))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    return {
        "ok": True,
        "route_id": copy.id,
        "version": copy.version,
        "from_route": copy.snapshot_of_id,
        "from_version": copy.snapshot_version,
    }


@router.put("/{task_id}/routes/{tr_id}/steps")
def edit_task_route_steps(
    task_id: int, tr_id: int, data: RouteStepsIn, db: Session = Depends(get_db)
):
    """在任务里直接改一条路线的工序（抽屉内弹窗用，不跳去编排页）。

    副本就地改、立即生效；引用基线走升版，其它引用同一标准的订单一起同步。
    """
    require_perm(db, DUTY_TASK_PLAN)
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    tr = db.get(TaskRoute, tr_id)
    if tr is None or tr.task_id != task_id:
        raise HTTPException(404, "任务下没有这条路线")
    if tr.status != "active":
        raise HTTPException(400, "这条路线已从任务移除，不能再编辑")

    try:
        route, meta = task_service.edit_route_steps(
            db,
            task,
            tr,
            [s.model_dump() for s in data.steps],
            created_by=current_operator(data.user or "operator"),
            publish=data.publish,
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    db.refresh(task)
    return {
        "ok": True,
        **meta,
        "route": route_service.route_out(route),
        "task": task_service.task_detail(db, task),
    }


@router.post("/{task_id}/routes/{tr_id}/promote")
def promote_copy_route(
    task_id: int, tr_id: int, user: str = "operator", db: Session = Depends(get_db)
):
    """把本单副本进阶为基线新版（副本的**唯一**出口）。

    副本与基线一致时自动合并回引用；不一致时升版发布，所有引用旧版的任务
    一起切到新版。两种结局都会删掉这份副本 —— 内容进了基线，留着就是双份真相。
    """
    require_perm(db, DUTY_TASK_PLAN)
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    tr = db.get(TaskRoute, tr_id)
    if tr is None or tr.task_id != task_id:
        raise HTTPException(404, "任务下没有这条路线")
    if tr.status != "active":
        raise HTTPException(400, "这条路线已从任务移除，不能进阶")
    copy = db.get(ProcessRoute, tr.route_id)
    if copy is None:
        raise HTTPException(404, "副本不存在")
    try:
        out = route_service.promote_copy(db, copy, task=task, created_by=current_operator(user or "operator"))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    db.refresh(task)
    return {"ok": True, **out, "task": task_service.task_detail(db, task)}


@router.get("/{task_id}/notices")
def notices(task_id: int, db: Session = Depends(get_db)):
    """本任务下「基线已升版、还没人确认」的引用清单。

    所有引用处（任务列表、抽屉、单据审核页）都读这一份判断，口径只此一处，
    避免各页面自己比版本号，比出不同结论。
    """
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    items = task_service.version_alerts(db, task_id=task.id)
    return {"task_id": task.id, "task_no": task.task_no, "total": len(items), "items": items}


@router.post("/{task_id}/ack")
def ack_version(task_id: int, data: AckIn, db: Session = Depends(get_db)):
    """确认升版：采用新版，或本单保留现版（转副本）。

    不确认不等于阻止业务，但它会一直挂在待办和各页面提醒上 ——
    「谁按哪一版干的」是必须有人回答的问题。
    """
    require_perm(db, DUTY_TASK_PLAN)
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    mode = (data.mode or "adopt").strip()
    if mode not in ("adopt", "keep"):
        raise HTTPException(400, "确认方式只能是 adopt（采用新版）或 keep（保留现版）")
    try:
        if data.tr_id is None:
            out = task_service.ack_all(db, task, mode=mode, user=current_operator(data.user or "operator"))
        else:
            tr = db.get(TaskRoute, data.tr_id)
            if tr is None or tr.task_id != task.id:
                raise HTTPException(404, "任务下没有这条路线")
            out = {
                "acked": 1,
                "failed": [],
                "items": [
                    task_service.ack_version(
                        db, task, tr, mode=mode, user=current_operator(data.user or "operator")
                    )
                ],
            }
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from exc
    db.commit()
    db.refresh(task)
    return {"ok": True, **out, "task": task_service.task_detail(db, task)}


@router.get("/{task_id}/candidates")
def candidates(
    task_id: int,
    q: str = "",
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """可加入本任务的基线路线清单（带已被引用数、是否已在任务里）。"""
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    return task_service.route_candidates(db, task.id, q=q, limit=limit)


@router.get("/{task_id}/diff")
def diff(task_id: int, db: Session = Depends(get_db)):
    """任务里所有「本单副本」与基线的差异；引用型的路线天然无差异。"""
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    detail = task_service.task_detail(db, task)
    items = [
        {
            "part": r["part"],
            "product": r["product"],
            "origin": r["origin"],
            "diff": r["diff"],
        }
        for r in detail["routes"]
        if r["status"] == "active" and r["origin"] == "copy"
    ]
    return {"task_no": task.task_no, "copy_count": len(items), "items": items}


@router.get("/{task_id}/source")
def source(task_id: int, db: Session = Depends(get_db)):
    """这个任务用到的工时样本 —— 定额「从哪来」的正面回答。"""
    task = db.get(WorkTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")

    trs = db.scalars(
        select(TaskRoute).where(
            TaskRoute.task_id == task.id, TaskRoute.status == "active"
        )
    ).all()
    out = []
    traceable = 0
    for tr in trs:
        rows = db.scalars(
            select(StandardHour).where(
                StandardHour.product == tr.product, StandardHour.part == tr.part
            )
        ).all()
        for r in rows:
            try:
                ids = json.loads(r.sample_item_ids or "[]")
            except (TypeError, ValueError):
                ids = []
            if ids:
                traceable += 1
            out.append(
                {
                    "product": r.product,
                    "part": r.part,
                    "process": r.process,
                    "unit_hours": float(r.unit_hours or 0),
                    "samples": int(r.samples or 0),
                    "agreement": float(r.agreement or 0),
                    "origin": r.origin,
                    "sample_item_ids": ids,
                    "traceable": bool(ids),
                }
            )
    return {
        "task_no": task.task_no,
        "total": len(out),
        "traceable_count": traceable,
        "items": out,
    }
