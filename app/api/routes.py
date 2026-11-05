"""工艺路线接口。

分层约定：这一层只做「参数校验 + 调用 service + 拼装响应」，
所有业务规则（版本状态机、工时分摊、顺序推断）都在 app/services/routes.py。

接口一览：
  GET    /api/routes                路线列表（按产品/件名/状态筛选）
  GET    /api/routes/{id}           路线详情（含工序行）
  POST   /api/routes                新建草稿（可带 steps，也可只建壳）
  PUT    /api/routes/{id}           保存草稿（改属性 + 全量替换工序行）
  POST   /api/routes/{id}/publish   发布（new_version=true 时升版）
  POST   /api/routes/{id}/archive   归档
  DELETE /api/routes/{id}           删除草稿
  POST   /api/routes/{id}/copy      复制为新版本草稿
  GET    /api/routes/suggest        按产品×件名生成候选工序行（不落库）
  GET    /api/routes/coverage       覆盖率与待建清单
  GET    /api/routes/tree           产品→件名→路线 树
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.sys_roles import DUTY_ROUTE_EDIT
from .auth import require_perm
from ..security import current_operator
from ..models import ProcessRoute
from ..services import routes as R

router = APIRouter(prefix="/api/routes", tags=["routes"])


# ------------------------------------------------------------------ 入参
class StepIn(BaseModel):
    seq: int = 0
    process_code: str = ""
    process_name: str = ""
    op_type: str = "self"
    category: str = ""
    work_center: str = ""
    setup_hours: float = 0.0
    unit_hours: float = 0.0
    hours_src: str = "manual"
    samples: int = 0
    is_key: bool = False
    inspect_required: bool = False
    sop: str = ""
    remark: str = ""


class RouteIn(BaseModel):
    product: str = ""
    part: str = ""
    kind: str = "route"
    title: str = ""
    batch_qty: float = 0.0
    allowance_rate: float = 0.0
    completeness: str = "partial"
    remark: str = ""
    created_by: str = ""
    steps: list[StepIn] = Field(default_factory=list)
    generate: bool = False  # True 时用历史数据自动生成工序行


class RoutePatch(BaseModel):
    title: str | None = None
    batch_qty: float | None = None
    allowance_rate: float | None = None
    completeness: str | None = None
    remark: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None
    steps: list[StepIn] | None = None


class PublishIn(BaseModel):
    new_version: bool = False
    user: str = ""


# ------------------------------------------------------------------ 列表
@router.get("")
def list_routes(
    product: str = "",
    part: str = "",
    kind: str = "route",
    status: str = "",
    q: str = "",
    limit: int = 500,
    db: Session = Depends(get_db),
):
    stmt = select(ProcessRoute)
    if kind:
        stmt = stmt.where(ProcessRoute.kind == kind)
    if status:
        stmt = stmt.where(ProcessRoute.status == status)
    if product:
        stmt = stmt.where(ProcessRoute.product == product)
    if part:
        stmt = stmt.where(ProcessRoute.part == part)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(ProcessRoute.product.like(like) | ProcessRoute.part.like(like))
    routes = db.scalars(
        stmt.order_by(ProcessRoute.product, ProcessRoute.part, ProcessRoute.version.desc())
    ).all()
    items = [R.route_out(r) for r in routes[: max(1, min(limit, 2000))]]
    return {"total": len(routes), "items": items}


# ------------------------------------------------------------------ 详情
@router.get("/tree")
def route_tree(
    q: str = "",
    kind: str = "route",
    only_unbuilt: bool = False,
    product: str | None = None,
    db: Session = Depends(get_db),
):
    """产品 → 件名 → 路线 的树，供工作台左栏使用。

    树节点自带覆盖统计，产品按工时价值降序——
    覆盖看板已并入树中，不再单独占版面。
    """
    return R.tree(
        db, kind=kind, q=q, only_unbuilt=only_unbuilt, product=product
    )


@router.get("/coverage")
def route_coverage(limit: int = 200, db: Session = Depends(get_db)):
    return R.coverage(db, limit=limit)


class BatchGenerateIn(BaseModel):
    limit: int = 50
    offset: int = 0
    auto_publish: bool = False
    skip_existing: bool = True
    created_by: str = "system"
    dry_run: bool = False


@router.post("/batch-generate")
def route_batch_generate(data: BatchGenerateIn, db: Session = Depends(get_db)):
    """按价值排序批量生成路线草稿。

    dry_run=True 时只回预览不落库——首期建线前先看看会生成什么。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    out = R.batch_generate(
        db,
        limit=max(1, min(data.limit, 500)),
        offset=max(0, data.offset),
        auto_publish=data.auto_publish,
        skip_existing=data.skip_existing,
        created_by=data.created_by,
        dry_run=data.dry_run,
    )
    # 批量生成不逐条写留痕（几十条会淹没审计表），
    # 只在结果里说清生成了什么；人工改动仍走正常留痕。
    return out


@router.get("/suggest")
def route_suggest(product: str = "", part: str = "", db: Session = Depends(get_db)):
    """按产品×件名生成候选工序行（不落库），供建线时预览。

    两个参数都做成可选并在函数体内校验：写成必填 query 参数时 FastAPI
    会先返回 422 的英文校验错误，车间用户看不懂。
    """
    if not (product or "").strip() or not (part or "").strip():
        raise HTTPException(400, "请同时指定产品与件名")
    steps = R.suggest_steps(db, product, part)
    return {"product": product, "part": part, "total": len(steps), "steps": steps}


@router.get("/copies")
def route_copies(
    q: str = "",
    task_id: int | None = None,
    limit: int = 300,
    db: Session = Depends(get_db),
):
    """全部「本单副本」清单。

    副本不参与基线清单（kind 是 task），所以必须有独立入口，
    否则拆出去就再也找不到了 —— 那等于把工艺藏起来。
    """
    return R.copies_list(db, q=q, task_id=task_id, limit=max(1, min(limit, 1000)))


class PublishDraftsIn(BaseModel):
    product: str = ""
    part: str = ""
    limit: int = 500
    user: str = "system"
    dry_run: bool = True


@router.post("/publish-drafts")
def route_publish_drafts(data: PublishDraftsIn, db: Session = Depends(get_db)):
    """批量发布「从未发布过」的草稿基线（dry_run=True 时只预演）。

    只有已发布的路线能被任务引用；已有生效版本的组合一律跳过，交给人工升版发布。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    out = R.publish_drafts(
        db,
        product=data.product,
        part=data.part,
        limit=max(1, min(data.limit, 1000)),
        published_by=current_operator(data.user or "system"),
        dry_run=data.dry_run,
    )
    if not data.dry_run:
        db.commit()
    return out


@router.get("/{route_id}")
def route_detail(route_id: int, db: Session = Depends(get_db)):
    route = R.get_route(db, route_id)
    if not route:
        raise HTTPException(404, "路线不存在")
    out = R.route_out(route)
    # 被多少在制任务引用：界面要在点「撤回/归档」之前就知道会被拒，
    # 而不是点了才弹一个错误
    out["ref_count"] = R.active_ref_count(db, route.id)
    # 引用这条工艺的订单清单：编排页要能说出「是哪几单」，不能只报个数
    refs = R.ref_tasks(db, route.id)
    out["ref_tasks"] = refs
    out["pending_count"] = sum(1 for r in refs if not r.get("confirmed"))
    # 这一版是不是升版来的、谁发的、比上一版差多少工时
    notice_map = R.notices_by_pair(db, [(route.product, route.part)])
    out["notice"] = notice_map.get((route.product, route.part))
    return out


# ------------------------------------------------------------------ 写入
@router.post("")
def create_route(data: RouteIn, db: Session = Depends(get_db)):
    require_perm(db, DUTY_ROUTE_EDIT)
    product = (data.product or "").strip()
    part = (data.part or "").strip()
    if not product:
        raise HTTPException(400, "产品不能为空")
    if data.kind == "route" and not part:
        raise HTTPException(400, "件名不能为空")

    steps = [s.model_dump() for s in data.steps]
    if data.generate and not steps:
        steps = R.suggest_steps(db, product, part)

    # 同一 (kind, product, part) 已有未归档版本 → 提示用升版，不要另起草稿
    existing = db.scalars(
        select(ProcessRoute).where(
            ProcessRoute.kind == data.kind,
            ProcessRoute.product == product,
            ProcessRoute.part == part,
            ProcessRoute.status != "archived",
        )
    ).all()
    if existing:
        raise HTTPException(
            409,
            f"「{product} / {part}」已有版本 v{existing[0].version}"
            f"（{existing[0].status}），请在原版本上升版，避免版本号分叉",
        )

    route = R.create_draft(
        db,
        product,
        part,
        kind=data.kind,
        title=data.title,
        steps=steps,
        source="history" if data.generate else "manual",
        created_by=data.created_by,
        batch_qty=data.batch_qty,
        remark=data.remark,
    )
    route.completeness = data.completeness or "partial"
    route.allowance_rate = data.allowance_rate
    db.add(route)
    db.flush()
    # 建路线必须同时建结构位置。结构（product_node）与工艺（process_route）是
    # 两张表，只建路线的话这件在结构树里根本不存在 —— 就成了「编了工艺但装配
    # 结构里没这个件」的孤儿，工艺编排页左栏、工艺卡沿结构展开都看不到它。
    if data.kind == "route" and part:
        from ..services.combo_registry import ensure_node

        ensure_node(db, product, part, source="manual")
    db.commit()
    db.refresh(route)
    return R.route_out(route)


@router.put("/{route_id}")
def update_route(route_id: int, data: RoutePatch, db: Session = Depends(get_db)):
    require_perm(db, DUTY_ROUTE_EDIT)
    route = R.get_route(db, route_id)
    if not route:
        raise HTTPException(404, "路线不存在")
    if route.status != "draft":
        raise HTTPException(400, "已发布或已归档的路线不能直接修改，请升版")

    if data.title is not None:
        route.title = data.title
    if data.batch_qty is not None:
        route.batch_qty = data.batch_qty
    if data.allowance_rate is not None:
        route.allowance_rate = data.allowance_rate
    if data.completeness is not None:
        route.completeness = data.completeness
    if data.remark is not None:
        route.remark = data.remark
    if data.effective_from is not None:
        route.effective_from = data.effective_from or None
    if data.effective_to is not None:
        route.effective_to = data.effective_to or None
    db.add(route)
    if data.steps is not None:
        R.save_steps(db, route, [s.model_dump() for s in data.steps])
    db.commit()
    db.refresh(route)
    return R.route_out(route)


@router.post("/{route_id}/publish")
def publish_route(route_id: int, data: PublishIn | None = None, db: Session = Depends(get_db)):
    require_perm(db, DUTY_ROUTE_EDIT)
    route = R.get_route(db, route_id)
    if not route:
        raise HTTPException(404, "路线不存在")
    # 发布前记下旧基准与它的引用方：发布后旧版归档，引用仍在旧版上，
    # 这些任务就是「需要确认升版」的对象，必须当场告诉操作人。
    old = R.pick_baseline(db, route.product, route.part)
    affected = (
        [t for t in R.ref_tasks(db, old.id)] if old is not None and old.id != route.id else []
    )
    try:
        new = R.publish(
            db,
            route,
            new_version=bool(data and data.new_version),
            published_by=current_operator((data.user if data else "") or "system"),
            action="publish",
            note="编排页发布",
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    db.refresh(new)
    out = R.route_out(new)
    if out["status"] == "released":
        out["affected_tasks"] = affected
        out["affected_count"] = len(affected)
        out["previous_version"] = old.version if old is not None else None
        if affected:
            # 发布改变的是「全厂核对基准」。引用它的订单不会自动跟着确认，
            # 必须明说，否则用户以为发完就都切过来了。
            out["hint"] = (
                f"已发布 v{out['version']}（上一版 v{old.version if old else '—'} 归档）。"
                f"{len(affected)} 个在制订单仍引用旧版，已标记「升版待确认」；"
                "这些订单的已确认单据需重校验后，告警才会按新基准更新"
            )
        else:
            out["hint"] = "已发布为核对基准；已确认单据需要重校验后，告警才会按新基准更新"
    return out


@router.post("/{route_id}/archive")
def archive_route(route_id: int, db: Session = Depends(get_db)):
    require_perm(db, DUTY_ROUTE_EDIT)
    route = R.get_route(db, route_id)
    if not route:
        raise HTTPException(404, "路线不存在")
    if route.status == "draft":
        raise HTTPException(400, "草稿直接删除即可，无需归档")
    try:
        R.archive(db, route)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    db.refresh(route)
    return R.route_out(route)


class RestoreIn(BaseModel):
    to: str = "draft"  # draft / released


@router.post("/{route_id}/restore")
def restore_route(route_id: int, data: RestoreIn | None = None, db: Session = Depends(get_db)):
    """撤销归档：已归档 → 草稿（默认）或已发布。"""
    require_perm(db, DUTY_ROUTE_EDIT)
    route = R.get_route(db, route_id)
    if not route:
        raise HTTPException(404, "路线不存在")
    try:
        R.restore(db, route, to=(data.to if data else "draft"))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    db.refresh(route)
    return R.route_out(route)


@router.post("/{route_id}/reopen")
def reopen_route(route_id: int, db: Session = Depends(get_db)):
    """撤回发布：已发布 → 草稿，让原稿能就地改。"""
    require_perm(db, DUTY_ROUTE_EDIT)
    route = R.get_route(db, route_id)
    if not route:
        raise HTTPException(404, "路线不存在")
    try:
        R.reopen(db, route)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    db.refresh(route)
    return R.route_out(route)


@router.post("/{route_id}/revision")
def revision_route(route_id: int, db: Session = Depends(get_db)):
    """升版：复制成一份可编辑的新草稿，旧版继续生效直到新草稿发布。"""
    require_perm(db, DUTY_ROUTE_EDIT)
    route = R.get_route(db, route_id)
    if not route:
        raise HTTPException(404, "路线不存在")
    try:
        new = R.revision(db, route)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    db.refresh(new)
    return R.route_out(new)


@router.post("/{route_id}/copy")
def copy_route(route_id: int, db: Session = Depends(get_db)):
    """复制为新版本草稿（不改原版本）。"""
    require_perm(db, DUTY_ROUTE_EDIT)
    route = R.get_route(db, route_id)
    if not route:
        raise HTTPException(404, "路线不存在")
    new = R.create_draft(
        db,
        route.product,
        route.part,
        kind=route.kind,
        title=route.title,
        steps=[R.step_out(s) for s in route.steps],
        source=route.source,
        created_by=route.created_by,
        batch_qty=route.batch_qty,
        remark=route.remark,
    )
    db.commit()
    db.refresh(new)
    return R.route_out(new)


@router.delete("/{route_id}")
def delete_route(route_id: int, db: Session = Depends(get_db)):
    require_perm(db, DUTY_ROUTE_EDIT)
    route = R.get_route(db, route_id)
    if not route:
        raise HTTPException(404, "路线不存在")
    try:
        R.delete_route(db, route)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return {"ok": True}
