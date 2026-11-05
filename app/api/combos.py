"""工时组合台账接口：889 条「产品 × 件名」的查询、发布与层级维护。

组合是工艺的**身份**（`work_combo`），路线只是它的某一版。所以这里多了
发布/撤回/层级三组接口——台账不再只是「看看有哪些组合」，而是「这些工艺
到底能不能被用」的唯一开关。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.sys_roles import DUTY_ROUTE_EDIT
from .auth import require_perm
from ..models import ProductNode, WorkCombo
from ..security import current_operator
from ..services import combo_registry as CR
from ..services import combos as C
from ..services import node_plan as NP

router = APIRouter(prefix="/api/combos", tags=["combos"])


class StepIn(BaseModel):
    process: str
    unit_hours: float = 0.0
    samples: int | None = None


class ComboCreate(BaseModel):
    product: str = ""
    part: str
    steps: list[StepIn] = []


class RenameIn(BaseModel):
    product: str = ""
    part: str
    new_product: str | None = None
    new_part: str | None = None
    sync_routes: bool = True


class ActiveIn(BaseModel):
    product: str = ""
    part: str
    active: bool = True


class StepCreate(BaseModel):
    product: str = ""
    part: str
    process: str
    unit_hours: float = 0.0
    samples: int | None = None


@router.get("")
def list_combos(
    q: str = "",
    state: str = "",
    only_issue: bool = False,
    # 默认显示全部：组合的身份不会因为历史工时行停用而消失。
    # 传 False 才只看「历史工时仍在用」的那批。
    include_disabled: bool = True,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    # 这里**不写库**。原来的 `if offset == 0: sync_combos()` 让每次翻回首页都
    # 付一次全量扫描；多进程下更会变成多个进程抢同一把 SQLite 写锁，
    # 组合台账并发时互相阻塞的主因就是它。登记改由三个明确时机触发：
    # OCR 落库后（带出新产品×件名）/ 工艺路线改名后 / 这里的手动同步。
    return C.list_combos(
        db,
        q=q,
        state=state,
        only_issue=only_issue,
        include_disabled=include_disabled,
        limit=limit,
        offset=offset,
    )


@router.post("/sync")
def sync_combos(db: Session = Depends(get_db)):
    """手动同步组合台账：把工时账/路线里新出现的「产品 × 件名」补登记为草稿。

    只补新行，绝不动已有状态 —— 否则点一次同步就会有人「被发布」一次。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    res = CR.sync_combos(db, actor=current_operator("") or "manual")
    db.commit()
    return res


@router.get("/detail")
def combo_detail(product: str = "", part: str = "", db: Session = Depends(get_db)):
    return C.detail(db, product, part)


@router.post("/step")
def add_step(data: StepCreate, db: Session = Depends(get_db)):
    require_perm(db, DUTY_ROUTE_EDIT)
    try:
        row = C.upsert_step(db, data.product, data.part, data.process, data.unit_hours, data.samples)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return {"id": row.id, "process": row.process, "unit_hours": row.unit_hours}


@router.put("/step/{step_id}")
def update_step(step_id: int, data: StepIn, db: Session = Depends(get_db)):
    require_perm(db, DUTY_ROUTE_EDIT)
    from ..models import StandardHour

    row = db.get(StandardHour, step_id)
    if not row:
        raise HTTPException(404, "工序行不存在")
    if data.unit_hours < 0:
        raise HTTPException(400, "单件工时不能为负")
    row.unit_hours = float(data.unit_hours)
    if data.samples is not None:
        row.samples = int(data.samples)
    db.commit()
    return {"ok": True}


@router.delete("/step/{step_id}")
def delete_step(step_id: int, db: Session = Depends(get_db)):
    require_perm(db, DUTY_ROUTE_EDIT)
    try:
        C.delete_step(db, step_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    db.commit()
    return {"ok": True}


@router.post("/rename")
def rename(data: RenameIn, db: Session = Depends(get_db)):
    require_perm(db, DUTY_ROUTE_EDIT)
    try:
        res = C.rename(db, data.product, data.part, data.new_product, data.new_part, data.sync_routes)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return res


@router.post("/active")
def set_active(data: ActiveIn, db: Session = Depends(get_db)):
    """停用 / 启用。停用后组合状态变 archived，会立刻从识别、挂线、工艺卡里消失。"""
    require_perm(db, DUTY_ROUTE_EDIT)
    res = C.set_active(db, data.product, data.part, data.active)
    db.commit()
    return res


@router.post("")
def create_combo(data: ComboCreate, db: Session = Depends(get_db)):
    require_perm(db, DUTY_ROUTE_EDIT)
    try:
        res = C.create_combo(db, data.product, data.part, [s.model_dump() for s in data.steps])
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return res


# ---------------------------------------------------------------- 发布控制


class ComboRef(BaseModel):
    product: str = ""
    part: str


class ReleaseIn(ComboRef):
    note: str = ""
    # force 只用来越过 block 级校验（空工序之外的那些），并且照样留痕。
    # 不提供「悄悄发布」的路子：留痕里一定有操作人。
    force: bool = False


class WithdrawIn(ComboRef):
    reason: str = ""
    force: bool = False


class BatchReleaseIn(BaseModel):
    ids: list[int] = []
    product: str = ""
    limit: int = 200
    dry_run: bool = True


class LevelIn(ComboRef):
    parent_part: str | None = None   # 空串 = 提到根；None = 不改
    qty_per_unit: float | None = None
    seq: int | None = None
    reason: str = ""


class MoveIn(ComboRef):
    # up / down 调同层顺序；indent / outdent 升降层级
    action: str


class AttachIn(BaseModel):
    product: str = ""
    parts: list[str] = []
    parent_part: str = ""
    seq: int | None = None


class UndoIn(BaseModel):
    product: str = ""
    # move 返回的 undo 快照，原样传回：每项 {id, parent_id, seq}
    rows: list[dict] = []


class AutoNestIn(BaseModel):
    product: str = ""
    dry_run: bool = True
    # 只落清单里勾中的那几条；空 = 全落（预演时还没挑）
    parts: list[str] = []


class NodeIn(BaseModel):
    """在现有结构里新增一件。`parent_part` 空 = 挂顶层。"""

    product: str
    part: str
    parent_part: str = ""
    qty_per_unit: float = 1.0
    # None = 按上级派生；填了 = 人工设定（plan_src=manual）
    plan_qty: float | None = None
    seq: int | None = None
    user: str = ""


class PlanIn(BaseModel):
    product: str = ""
    part: str
    plan_qty: float
    user: str = ""


def _sync_plans(db: Session, product: str = "", part: str = "") -> int:
    """结构改动之后把计划量整棵重算一遍。

    用量、上级、兄弟顺序都会改变派生值：装一台从 1 件变 2 件，下级需求量就翻倍。
    不重算的话结构树上「每台用量」和「计划数量」两列会互相矛盾，而人只会相信
    看得见的那一列。
    """
    from sqlalchemy import select as _select

    db.flush()  # Session 是 autoflush=False，不先落盘重算读到的是旧值
    if product:
        return len(NP.recalc_product(db, product))
    total = 0
    for name in db.scalars(_select(ProductNode.product).distinct()).all():
        total += len(NP.recalc_product(db, name or ""))
    return total


def _get_node(db: Session, product: str, part: str) -> ProductNode:
    """取结构节点。层级操作一律作用在 `product_node` 上 ——
    结构与工艺是两张表，改层级不该去动工时组合那一行。"""
    row = db.scalar(
        select(ProductNode).where(
            ProductNode.product == (product or "").strip(),
            ProductNode.part == (part or "").strip(),
        )
    )
    if row is None:
        raise HTTPException(404, "结构里没有这一件，先在台账里登记组合")
    return row


def _get_combo(db: Session, product: str, part: str) -> WorkCombo:
    row = db.scalar(
        select(WorkCombo).where(
            WorkCombo.product == (product or "").strip(),
            WorkCombo.part == (part or "").strip(),
        )
    )
    if row is None:
        raise HTTPException(404, "组合不存在，请先在台账里登记")
    return row


@router.get("/tree")
def combo_tree(
    product: str = "",
    status: str = "",
    q: str = "",
    structure: bool = False,
    db: Session = Depends(get_db),
):
    """产品分类 → 部件层级 的树。左栏与台账共用同一份结构。

    `structure=1` 只留真实 parent_id 结构、不掺按件名聚的虚拟分组 ——
    调层级的那棵树用它，虚拟分组没有可写的上级。
    """
    return CR.combo_tree(db, product=product, status=status, q=q, cluster=not structure)


@router.get("/level-tree")
def level_tree(product: str = "", part: str = "", db: Session = Depends(get_db)):
    """「改层级」弹窗要的那棵树：只认真实 parent_id，并标出不能选的节点。

    返回里每个节点带 `blocked`（自己 + 全部子孙）。界面据此把这几行点灰，
    免得人把它挂到自己的下级上、提交时才被成环校验顶回来。
    """
    if not (product or "").strip():
        raise HTTPException(400, "先指定产品：层级只在同一个产品内成立")
    try:
        return CR.level_tree(db, product, part)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e


@router.post("/sync")
def sync(db: Session = Depends(get_db)):
    """把工时账/路线里新出现、但台账还没有的组合补登记（默认草稿）。"""
    require_perm(db, DUTY_ROUTE_EDIT)
    res = CR.sync_combos(db, actor=current_operator("system"))
    res["product_ids"] = CR.backfill_product_ids(db)
    db.commit()
    return res


@router.post("/release")
def release(data: ReleaseIn, db: Session = Depends(get_db)):
    """确认并发布（一步）。发布后这个组合才会出现在任务挂线、识别校验、工艺卡里。"""
    require_perm(db, DUTY_ROUTE_EDIT)
    combo = _get_combo(db, data.product, data.part)
    try:
        res = CR.release_combo(
            db, combo, by=current_operator(""), note=data.note, force=data.force
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return res


@router.post("/withdraw")
def withdraw(data: WithdrawIn, db: Session = Depends(get_db)):
    """撤回发布。被在制任务引用时先列单号，force 才真撤。"""
    require_perm(db, DUTY_ROUTE_EDIT)
    combo = _get_combo(db, data.product, data.part)
    try:
        res = CR.withdraw_combo(
            db, combo, by=current_operator(""), reason=data.reason, force=data.force
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return res


@router.post("/batch-release")
def batch_release(data: BatchReleaseIn, db: Session = Depends(get_db)):
    """批量确认发布。默认预演：先给人看「哪几条会被发、哪几条会被跳过」。"""
    require_perm(db, DUTY_ROUTE_EDIT)
    res = CR.batch_release(
        db,
        ids=data.ids or None,
        product=data.product,
        by=current_operator(""),
        limit=data.limit,
        dry_run=data.dry_run,
    )
    if not data.dry_run:
        db.commit()
    return res


@router.post("/relayout")
def relayout(product: str = "", db: Session = Depends(get_db)):
    """按件名规则重铺层级。只动 level_src != manual 的，人工定过的不碰。"""
    require_perm(db, DUTY_ROUTE_EDIT)
    if product:
        rows = [CR.layout_levels(db, product)]
    else:
        rows = CR.layout_all(db)
    _sync_plans(db, product)
    db.commit()
    return {"products": len(rows), "detail": rows}


@router.patch("/level")
def set_level(data: LevelIn, db: Session = Depends(get_db)):
    """人工指定一个组合挂在哪个部件下。改过就标记 manual，重铺时不会被冲掉。

    返回里带 `path`（改后从总装到自己的件名链）和 `affected`（跟着重算层号
    的下级条数），前端拿它做「已挂到 整机装配 › 板总包 下」的提示。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    node = _get_node(db, data.product, data.part)
    try:
        res = CR.set_level(
            db,
            node,
            parent_part=data.parent_part,
            qty_per_unit=data.qty_per_unit,
            seq=data.seq,
            user=current_operator(""),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # 用量/上级变了，整棵树的计划量要跟着重算
    _sync_plans(db, data.product)
    db.commit()
    return res


@router.post("/move")
def move_level(data: MoveIn, db: Session = Depends(get_db)):
    """方向键：↑↓ 调同层顺序，← 退一级，→ 挂到上面最近的那件里。

    落位规则全在服务层算，前端只发「往哪走」。分不出目标时报 400 并说明
    原因（最前/最后/最上层/会成环），数据原样不动。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    node = _get_node(db, data.product, data.part)
    try:
        res = CR.move_combo(db, node, data.action, user=current_operator(""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _sync_plans(db, data.product)
    db.commit()
    return res


@router.post("/undo-move")
def undo_move(data: UndoIn, db: Session = Depends(get_db)):
    """撤销一次方向键操作。方向键是按下即入库，没有「不保存」这条路，
    所以必须有一条原路退回的通道：把 move 返回的 undo 快照送回来即可。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    try:
        res = CR.undo_move(db, data.product, data.rows, user=current_operator(""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _sync_plans(db, data.product)
    db.commit()
    return res


# ---------------------------------------------------------------- 计划数量


@router.patch("/plan")
def set_plan(data: PlanIn, db: Session = Depends(get_db)):
    """设定计划生产数量。改上级，下级按用量重算；改下级，上级按用量反推。

    返回里带 `warnings`（反推除不尽被向上取整时说的话），界面必须显示出来 ——
    「零件 3 件 ÷ 用量 2 = 1.5 台」这件事不能悄悄变成 2 台。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    try:
        res = NP.set_plan(
            db, data.product, data.part, data.plan_qty, user=current_operator(data.user)
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    # set_plan 只调了本条链，这里把整棵树拉齐（只动 auto 的，人工值不受影响）
    _sync_plans(db, data.product)
    db.commit()
    return res


@router.post("/node")
def create_node(data: NodeIn, db: Session = Depends(get_db)):
    """在现有结构里新增一件（可指定挂到哪个上级、每台用量、计划数量）。

    只建结构。要不要给它编工艺是下一步的事 —— 结构里有、工艺里没有是合法状态，
    台账会把它显示成「无工艺」。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    try:
        res = NP.create_node(
            db,
            data.product,
            data.part,
            parent_part=data.parent_part,
            qty_per_unit=data.qty_per_unit,
            plan_qty=data.plan_qty,
            seq=data.seq,
            user=current_operator(data.user),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _sync_plans(db, (data.product or "").strip())
    db.commit()
    return res


@router.delete("/node")
def delete_node(
    product: str = Query(""),
    part: str = Query(""),
    db: Session = Depends(get_db),
):
    """从结构里删掉一件。有下级、有工艺、有任务引用、有报工明细的一律拦住：
    删掉一个被工资表引用的件，比留着一行脏数据危险得多。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    try:
        res = NP.delete_node(db, product, part, user=current_operator(""))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    db.commit()
    return res


@router.post("/attach")
def attach(data: AttachIn, db: Session = Depends(get_db)):
    """把一批件一次挂到同一个上级（`parent_part` 空串 = 提到最上层）。

    批量场景：勾一批按 ←，或把一批拖到同一件上。单件失败不影响其他件。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    try:
        res = CR.attach_many(
            db,
            data.product,
            data.parts,
            parent_part=data.parent_part,
            seq=data.seq,
            user=current_operator(""),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    _sync_plans(db, data.product)
    db.commit()
    return res


@router.post("/auto-nest")
def auto_nest(data: AutoNestIn, db: Session = Depends(get_db)):
    """按件名包含关系一键归位（默认预演）：只认严格前缀、两个字以上。

    认不出来的不猜，留在原处等人按键调整 —— 猜错的层级比空着更难查。
    """
    require_perm(db, DUTY_ROUTE_EDIT)
    res = CR.auto_nest(
        db, data.product, dry_run=data.dry_run, only=data.parts, user=current_operator("")
    )
    if not data.dry_run:
        # 一批件换了上级，各支的计划量都要按新上级重算
        _sync_plans(db, data.product)
        db.commit()
    return res
