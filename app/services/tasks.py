"""任务层：**一个订单 = 一个任务**，任务由多条工艺路线组成。

关键模型转变（P2）：

    旧：一张加工路线单 = 一个任务，任务上带 product/part/batch，建任务就复制一份工艺
    新：一个订单 = 一个任务，路线通过 task_route **引用**已有基线，需要改才 fork

为什么必须是引用：85 条基线要支撑成百上千个订单，每个订单复制一份，一年就是几千条
几乎相同的路线，「标准工艺」立刻失去意义；而且改一次标准要改几千处，等于不能改。
引用之后，改基线所有在制任务同步生效 —— 这才是复用的价值。确实要为本单改工艺时
fork 出副本（kind='task'），两边从此互不污染。

本模块负责：
1. 产品写法归一（精确归一到 product_dict，认不出就返回 None，绝不猜）；
2. 任务 CRUD：手工建单、加/移除路线、fork 副本、结案；
3. 单据归集：一张单确认后归到「同产品同批次同期间」的已有任务，没有才新建；
4. 聚合刷新：任务的数量/实报工时/单据数/主产品都是派生值，一次算清。
"""
from __future__ import annotations

import json
import re
import unicodedata
import datetime as dt
from collections import defaultdict

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..models import (
    Period,
    ProcessRoute,
    ProcessRouteStep,
    ProductDict,
    Sheet,
    TaskRevision,
    TaskRoute,
    WorkCombo,
    WorkItem,
    WorkTask,
)

KIND_BASELINE = "route"
KIND_TASK = "task"

ORIGIN_BASELINE = "baseline"
ORIGIN_COPY = "copy"
ORIGIN_MANUAL = "manual"

_PUNCT = re.compile(r"[\s\-_·•.,，。、()（）\[\]【】/\\|:：]+")

# 已结案/已取消的订单，工艺路线一律锁定。
# 理由和「已发布路线不能改」一样：这两个状态说明活已经干完或作废，
# 再改它引用的工艺，会让当初按哪版做的无法回答，实报与定额的对照也失去意义。
TASK_LOCKED_STATUS = ("closed", "cancelled")

# 任务「口径字段」：改这些就升版。顺序即界面展示顺序。
TASK_FIELD_LABELS: dict[str, str] = {
    "task_no": "任务号",
    "title": "订单名",
    "customer": "客户",
    "qty": "订单数量",
    "order_date": "下单日期",
    "due_date": "交期",
    "period_id": "核算期间",
}
VERSIONED_TASK_FIELDS = tuple(TASK_FIELD_LABELS)


def _task_field_value(db: Session, task: WorkTask, field: str):
    """口径字段的可读值。期间要翻成 YYYY-MM，否则修订记录里是一串 id。"""
    value = getattr(task, field, None)
    if field == "period_id":
        return _period_ym(db, value)
    if field == "qty":
        qty = round(float(value or 0), 4)
        # 整数保留数字类型但显示成 100 而不是 100.0：修订记录是给人念的，
        # 但仍让接口返回数字，界面不必再转一次
        return int(qty) if qty == int(qty) else qty
    return "" if value is None else str(value)


def ensure_task_editable(task: WorkTask | None) -> None:
    """任务已结案/已取消时，拒绝一切改路线的动作。"""
    if task is None:
        return
    if task.status in TASK_LOCKED_STATUS:
        label = "已结案" if task.status == "closed" else "已取消"
        raise ValueError(
            f"任务 {task.task_no} {label}，工艺路线已锁定；"
            "如需调整，请先把任务状态改回「在制」"
        )


# ------------------------------------------------------------------ 产品归一
def norm_key(value: str) -> str:
    """把产品写法压成可比较的键。只用于比对，不用于显示。"""
    s = unicodedata.normalize("NFKC", value or "").strip()
    return _PUNCT.sub("", s).upper()


def product_index(db: Session) -> dict[str, ProductDict]:
    """归一键 → 产品。规范名优先，代号与别名次之。"""
    index: dict[str, ProductDict] = {}
    for p in db.scalars(select(ProductDict)).all():
        for raw in [p.name, p.code, *(p.aliases or "").split(",")]:
            key = norm_key(raw)
            if key:
                index.setdefault(key, p)
    return index


def resolve_product(db: Session, name: str, code: str = "") -> ProductDict | None:
    """按写法找产品。找不到返回 None —— 由调用方决定是新建还是进待认领。"""
    index = product_index(db)
    for raw in (code, name):
        key = norm_key(raw)
        if key and key in index:
            return index[key]
    return None


def ensure_product(db: Session, name: str, code: str = "") -> ProductDict | None:
    name = (name or "").strip()
    if not name:
        return None
    found = resolve_product(db, name, code)
    if found:
        return found
    p = ProductDict(name=name, code=(code or "").strip())
    db.add(p)
    db.flush()
    return p


def canonical_product(db: Session, name: str) -> str:
    """写法 → 字典规范名；认不出就原样返回（不丢信息，进待认领）。"""
    found = resolve_product(db, name)
    return found.name if found else (name or "").strip()


# ------------------------------------------------------------------ 任务号
def _clean_token(value: str, fallback: str = "NA") -> str:
    s = unicodedata.normalize("NFKC", value or "").strip()
    s = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", s).strip("-")
    return s or fallback


def make_task_no(product_code: str, batch_no: str, ym: str) -> str:
    """合成任务号：`产品代号-批次-期间`，如 `YUJ-18BZ-26-1-202609`。

    单据上没有订单号字段（只有批次号 26-1 / 河北凯诺 / 销 三种写法），
    所以自动建单时按这个规则合成；手工建单可以自己填订单号。
    """
    return "-".join(
        [
            _clean_token(product_code),
            _clean_token(batch_no),
            _clean_token((ym or "").replace("-", "")),
        ]
    )


def _period_ym(db: Session, period_id: int | None) -> str:
    if not period_id:
        return ""
    p = db.get(Period, period_id)
    return p.ym if p else ""


def _unique_task_no(db: Session, base: str, exclude_id: int | None = None) -> str:
    """撞号顺延。同产品同批次同期间会有多张单，合成任务号必然重复。"""
    no, n = base, 2
    while True:
        stmt = select(WorkTask).where(WorkTask.task_no == no)
        if exclude_id:
            stmt = stmt.where(WorkTask.id != exclude_id)
        if db.scalars(stmt).first() is None:
            return no
        no = f"{base}-{n}"
        n += 1


# ------------------------------------------------------------------ 聚合刷新
def _title_is_auto(title: str) -> bool:
    """标题空着、或还是上一版自动生成的，都该重算。

    早期自动标题写成「产品 × N 条路线」，一旦落库就不再是空串，
    只判空会让它永远留在库里 —— 这类「程序自己写的」字段要能被自己认出来。
    """
    t = (title or "").strip()
    return not t or bool(re.search(r"×\s*\d+\s*条路线$", t))


def refresh_task(db: Session, task: WorkTask) -> dict:
    """重算派生字段：主产品、订单数量、实报工时、单据数、待核对。

    **实报只算已确认单据**。汇总页的口径是「已确认才入账」，任务页若把待核对
    也算进实报，同一批单据在两个页面会给出两个数，对账时先要解释半天。
    待核对单独物化（pending_hours/pending_count），界面上和实报并排显示。

    数量只在为空时用单据兜底，不覆盖手填的订单台数 —— 台数和合格件数不是一回事。
    """
    sheets = db.scalars(
        select(Sheet).where(Sheet.task_id == task.id, Sheet.status.notin_(("rejected", "void")))
    ).all()
    verified = [s for s in sheets if s.status == "verified"]
    pending = [s for s in sheets if s.status != "verified"]
    task.sheet_count = len(verified)
    # 扫码报工没有 sheet：它的工时与单据数记在 work_item 自己身上
    # （sheet_id 为空、task_id 指向本任务）。只算 sheet 的话，
    # 计划员扫了一整天，任务页的「实报工时」还是 0 —— 人会以为白干了。
    scan_hours = float(
        db.scalar(
            select(func.coalesce(func.sum(WorkItem.total_hours), 0.0)).where(
                WorkItem.task_id == task.id,
                WorkItem.sheet_id.is_(None),
            )
        )
        or 0.0
    )
    task.actual_hours = round(
        sum(float(s.total_hours or 0) for s in verified) + scan_hours, 4
    )
    task.pending_count = len(pending)
    task.pending_hours = round(sum(float(s.total_hours or 0) for s in pending), 4)

    routes = db.scalars(
        select(TaskRoute)
        .where(TaskRoute.task_id == task.id, TaskRoute.status == "active")
        .order_by(TaskRoute.seq, TaskRoute.id)
    ).all()
    if routes and (routes[0].product or ""):
        task.product = routes[0].product
        prod = resolve_product(db, routes[0].product)
        if prod is not None:
            task.product_id = prod.id

    if _title_is_auto(task.title):
        # 标题只是列表上的识别标签，取「主产品 + 批次」；没有批次就只留产品。
        batch = ""
        for s in sheets:
            try:
                head = json.loads(s.header_json or "{}")
            except (TypeError, ValueError):
                head = {}
            batch = str(head.get("batch_no") or "").strip()
            if batch:
                break
        task.title = " ".join(x for x in (task.product or "", batch) if x) or "未命名任务"

    if not (task.qty or 0) and sheets:
        # 表头「机器台数」才是订单数量；没有就退到合格件数合计
        for s in sheets:
            try:
                head = json.loads(s.header_json or "{}")
            except (TypeError, ValueError):
                head = {}
            raw = str(head.get("machine_count") or "").strip()
            if raw.isdigit():
                task.qty = float(raw)
                break
    return {
        "sheet_count": task.sheet_count,
        "actual_hours": task.actual_hours,
        "pending_count": task.pending_count,
        "pending_hours": task.pending_hours,
        "route_count": len(routes),
        "product": task.product,
    }


# ------------------------------------------------------------------ 路线挂载
def _route_of(db: Session, route_id: int | None) -> ProcessRoute | None:
    return db.get(ProcessRoute, route_id) if route_id else None


def _route_totals(db: Session, route_ids: list[int]) -> dict[int, tuple[int, float]]:
    """一次算完多条路线的（工序数, 单件工时合计）。"""
    out: dict[int, tuple[int, float]] = {}
    if not route_ids:
        return out
    for rid, cnt, hours in db.execute(
        select(
            ProcessRouteStep.route_id,
            func.count(ProcessRouteStep.id),
            func.coalesce(func.sum(ProcessRouteStep.unit_hours), 0.0),
        )
        .where(ProcessRouteStep.route_id.in_(route_ids))
        .group_by(ProcessRouteStep.route_id)
    ).all():
        out[int(rid)] = (int(cnt or 0), round(float(hours or 0), 4))
    return out


def _route_ref_counts(db: Session, route_ids: list[int]) -> dict[int, int]:
    """每条路线被多少个在制任务引用。编辑基线前要拿它告诉用户「会影响几个订单」。"""
    ids = [int(x) for x in dict.fromkeys(route_ids) if x]
    if not ids:
        return {}
    return {
        int(rid): int(n)
        for rid, n in db.execute(
            select(TaskRoute.route_id, func.count())
            .where(TaskRoute.route_id.in_(ids), TaskRoute.status == "active")
            .group_by(TaskRoute.route_id)
        ).all()
    }


def add_routes(
    db: Session,
    task: WorkTask,
    specs: list[dict],
    *,
    created_by: str = "manual",
    create_baseline: bool = True,
) -> dict:
    """往任务里加路线。默认**引用**该产品×件名当前生效的基线，不复制。

    每个 spec：`{product, part, route_id?, batch_no?, qty?, origin?, note?}`。
    `route_id` 给了就用指定的那版（比如要把某任务钉在旧版工艺上）；
    没给就找**已发布**的基线。

    **只允许引用已发布的基线**。未发布的草稿是「还在改的东西」，
    拿它当核对基准等于把没定稿的定额写进账里：改一次草稿，历史实报就被重新定性。
    所以：
      - 指定了草稿/归档版本 → 直接失败，并说明它现在是什么状态；
      - 该组合只有草稿 → 失败并提示去「工艺及工时编排」发布；
      - 完全没有路线且 `create_baseline=True` → 按历史工时生成一条，**立即发布**后引用
        （新品第一次投产不该被挡住，但引用的一定是定稿版本）。

    返回 `{added, reactivated, failed, auto_published}`，部分失败不影响其余项。
    """
    from . import routes as route_service

    ensure_task_editable(task)

    added = reactivated = 0
    auto_published = 0
    failed: list[dict] = []
    items: list[dict] = []
    seq = db.scalar(
        select(func.coalesce(func.max(TaskRoute.seq), 0)).where(TaskRoute.task_id == task.id)
    ) or 0

    for spec in specs:
        raw_product = (spec.get("product") or "").strip()
        part = (spec.get("part") or "").strip()
        if not raw_product:
            failed.append({"product": raw_product, "part": part, "reason": "缺产品"})
            continue
        product = canonical_product(db, raw_product)

        # 停用的组合一律不能挂线。这里必须先拦：pick_baseline 对归档组合返回
        # None，不拦的话就会掉进下面的 create_baseline 兜底 —— 给它现造一条
        # 已发布路线，等于「停用的组合照样投产」，比不做闸更糟。
        combo = db.scalar(
            select(WorkCombo).where(
                WorkCombo.product == (product or "").strip(),
                WorkCombo.part == part,
            )
        )
        if combo is not None and combo.status == "archived":
            failed.append(
                {
                    "product": product,
                    "part": part,
                    "reason": "该组合已停用（归档），不能挂线；请先在「工时组合」台账里启用",
                    "need_publish": False,
                }
            )
            continue

        route = _route_of(db, spec.get("route_id"))
        origin = (spec.get("origin") or ORIGIN_BASELINE).strip()

        if route is not None:
            # 指定了具体版本：必须是一条「已发布基线」。
            # 指向副本的情况也不放行 —— 副本是别的任务的私有工艺，
            # 要复用就让它先进阶为基线，否则等于绕过版本管理。
            if route.kind != KIND_BASELINE:
                failed.append(
                    {
                        "product": product,
                        "part": part,
                        "route_id": route.id,
                        "reason": "这是一份本单副本，不能直接挂到别的任务；请先在副本清单里「进阶为基线」",
                        "need_publish": False,
                    }
                )
                continue
            if route.status != "released":
                state = {"draft": "草稿", "archived": "已归档"}.get(route.status, route.status)
                failed.append(
                    {
                        "product": product,
                        "part": part,
                        "route_id": route.id,
                        "reason": f"该版本是{state}状态，未发布不能引用；请先在「工艺及工时编排」中发布",
                        "need_publish": route.status == "draft",
                    }
                )
                continue

        if route is None:
            route = route_service.pick_baseline(db, product, part)

        if route is None and create_baseline and part:
            steps = route_service.suggest_steps(db, product, part)
            if not steps:
                failed.append(
                    {
                        "product": product,
                        "part": part,
                        "reason": "该产品×件名没有历史工时可生成工艺，请先在「工艺及工时编排」中新建并发布",
                        "need_publish": True,
                    }
                )
                continue
            route = route_service.create_draft(
                db,
                product,
                part,
                steps=steps,
                source="manual",
                created_by=created_by,
            )
            # 生成完立刻发布：只有已发布的才能被引用，这一步不能省
            route_service.publish(db, route, published_by=created_by)
            origin = ORIGIN_BASELINE
            auto_published += 1
            items.append({"auto_published_route": route.id, "version": route.version})

        if route is None:
            draft = route_service.latest_draft(db, product, part)
            if draft is not None:
                failed.append(
                    {
                        "product": product,
                        "part": part,
                        "route_id": draft.id,
                        "reason": f"该产品×件名只有草稿 v{draft.version}，尚未发布，不能引用",
                        "need_publish": True,
                    }
                )
            else:
                failed.append(
                    {
                        "product": product,
                        "part": part,
                        "reason": "该产品×件名还没有工艺路线，请先在「工艺及工时编排」中新建并发布",
                        "need_publish": True,
                    }
                )
            continue

        exist = db.scalars(
            select(TaskRoute).where(
                TaskRoute.task_id == task.id, TaskRoute.route_id == route.id
            )
        ).first()
        if exist is not None:
            if exist.status == "active":
                failed.append(
                    {"product": product, "part": part, "reason": "这条路线已经在任务里了"}
                )
                continue
            # 移除过又重新加回来：复用同一行，别留一堆 removed 垃圾
            exist.status = "active"
            exist.origin = origin
            exist.batch_no = (spec.get("batch_no") or exist.batch_no or "")
            exist.qty = float(spec.get("qty") or exist.qty or 0)
            # 重新挂回来等于当场认过这一版，别再弹「待确认升版」
            exist.seen_route_id = route.id
            exist.seen_at = dt.datetime.now()
            exist.seen_by = created_by
            reactivated += 1
            items.append({"id": exist.id, "route_id": route.id, "origin": origin})
            continue

        seq += 1
        tr = TaskRoute(
            task_id=task.id,
            route_id=route.id,
            origin=origin if route.kind != KIND_TASK else ORIGIN_COPY,
            product=product,
            part=(part or route.part or "").strip(),
            batch_no=(spec.get("batch_no") or "").strip(),
            qty=float(spec.get("qty") or 0),
            seq=seq,
            status="active",
            note=(spec.get("note") or "").strip(),
            # 刚引用的一版就是用户当下看到并接受的一版
            seen_route_id=route.id,
            seen_at=dt.datetime.now(),
            seen_by=created_by,
        )
        db.add(tr)
        db.flush()
        added += 1
        items.append({"id": tr.id, "route_id": route.id, "origin": tr.origin})

    db.flush()
    refresh_task(db, task)
    return {
        "added": added,
        "reactivated": reactivated,
        "auto_published": auto_published,
        "failed": failed,
        "items": items,
    }


def remove_route(db: Session, tr: TaskRoute, *, drop_copy: bool = True) -> dict:
    """把路线从任务里摘掉。引用只是解除关联，基线本身不动；副本才可能被删。

    删副本时必须先把 task_route 里的引用（含历史 removed 行）清干净：
    SQLAlchemy 的 delete 顺序按 mapper 依赖排，不先 flush 掉引用就会撞外键，
    报一个 `FOREIGN KEY constraint failed` —— 看起来像数据库坏了，其实是顺序问题。
    """
    deleted_copy = None
    ensure_task_editable(db.get(WorkTask, tr.task_id))
    if tr.origin == ORIGIN_COPY and drop_copy:
        others = db.scalars(
            select(TaskRoute).where(
                TaskRoute.route_id == tr.route_id,
                TaskRoute.id != tr.id,
                TaskRoute.status == "active",
            )
        ).first()
        route = db.get(ProcessRoute, tr.route_id)
        if others is None and route is not None and route.kind == KIND_TASK:
            task_id = tr.task_id
            db.query(TaskRoute).filter(TaskRoute.route_id == route.id).delete(
                synchronize_session=False
            )
            db.flush()
            db.query(ProcessRouteStep).filter(
                ProcessRouteStep.route_id == route.id
            ).delete(synchronize_session=False)
            db.flush()
            db.delete(route)
            db.flush()
            deleted_copy = route.id
            task = db.get(WorkTask, task_id)
            if task is not None:
                refresh_task(db, task)
            return {"removed": True, "deleted_copy": deleted_copy}
    tr.status = "removed"
    db.flush()
    task = db.get(WorkTask, tr.task_id)
    if task is not None:
        refresh_task(db, task)
    return {"removed": True, "deleted_copy": None}


def fork_route(
    db: Session, tr: TaskRoute, *, created_by: str = "manual", allow_archived: bool = False
) -> ProcessRoute:
    """把引用拆成副本：之后改这条路线只影响本任务。

    什么时候需要：这张单的工艺确实跟标准不一样（少一道、多一道、定额不同）。
    在这之前一直引用着，标准一变本单也跟着变，已开工的活就说不清按哪版做的。

    `allow_archived=True` 是给「升版后本单继续按旧版做」用的：旧版虽然
    已经退出基准，但它就是这张单当初按着干的工艺，必须能原样冻成副本。
    普通的「拆副本」仍然只认已发布版 —— 从没生效过的草稿不能当起点。
    """
    from . import routes as route_service

    ensure_task_editable(db.get(WorkTask, tr.task_id))
    if tr.origin == ORIGIN_COPY:
        route = db.get(ProcessRoute, tr.route_id)
        if route is not None:
            return route
    base = db.get(ProcessRoute, tr.route_id)
    if base is None:
        raise ValueError("路线不存在，无法拆出副本")
    if base.kind != KIND_BASELINE:
        raise ValueError("只有基线可以拆出副本；这份已经是副本了")
    # 拆副本等于「以某版标准为起点做本单的偏离」。起点本身没定稿，
    # 偏离就没有参照 —— 所以只能从已发布基线拆。
    if base.status != "released" and not (allow_archived and base.status == "archived"):
        state = {"draft": "草稿", "archived": "已归档"}.get(base.status, base.status)
        raise ValueError(
            f"这条引用指向的是{state}版本 v{base.version}，未发布不能作为标准拆副本；"
            "请先在「工艺及工时编排」中发布它"
        )

    base_steps = db.scalars(
        select(ProcessRouteStep)
        .where(ProcessRouteStep.route_id == base.id)
        .order_by(ProcessRouteStep.seq)
    ).all()
    version = (
        db.scalar(
            select(func.coalesce(func.max(ProcessRoute.version), 0)).where(
                ProcessRoute.kind == KIND_TASK, ProcessRoute.task_id == tr.task_id
            )
        )
        or 0
    ) + 1
    copy = ProcessRoute(
        kind=KIND_TASK,
        product=tr.product or base.product,
        part=tr.part or base.part,
        product_id=base.product_id,
        task_id=tr.task_id,
        version=version,
        status="released",
        completeness=base.completeness,
        batch_qty=base.batch_qty,
        allowance_rate=base.allowance_rate,
        source="fork",
        snapshot_of_id=base.id,
        snapshot_version=base.version,
        snapshot_status=base.status,
        title=f"{tr.part or base.part} · 本单副本",
        created_by=created_by,
    )
    db.add(copy)
    db.flush()
    for s in base_steps:
        db.add(
            ProcessRouteStep(
                route_id=copy.id,
                seq=s.seq,
                process_code=s.process_code,
                process_name=s.process_name,
                op_type=s.op_type,
                category=s.category,
                work_center=s.work_center,
                setup_hours=s.setup_hours,
                unit_hours=s.unit_hours,
                hours_src="fork",
                samples=s.samples,
                is_key=s.is_key,
                inspect_required=s.inspect_required,
                sop=s.sop,
                remark=s.remark,
            )
        )
    db.flush()
    tr.route_id = copy.id
    tr.origin = ORIGIN_COPY
    tr.seen_route_id = copy.id
    tr.seen_at = dt.datetime.now()
    tr.seen_by = created_by
    db.flush()
    return copy


# ------------------------------------------------- 任务内就地编辑工艺（不跳页）
def _norm_steps(source) -> list[tuple]:
    """把「ORM 工序行」和「前端提交的 dict」压成同一个可比较的形状。

    只比业务字段，不比 id / hours_src / samples —— 升版会重新生成这些，
    拿它们做差异判断会把「什么都没改」误判成改过，白白多出一版。
    """
    rows = []
    for i, s in enumerate(source, start=1):
        if isinstance(s, ProcessRouteStep):
            get = lambda k, d=None: getattr(s, k, d)  # noqa: E731
            seq = int(s.seq or i * 10)
        else:
            get = lambda k, d=None: s.get(k, d)  # noqa: E731
            seq = int(s.get("seq") or i * 10)
        rows.append(
            (
                seq,
                str(get("process_code", "") or "").strip(),
                str(get("process_name", "") or "").strip(),
                str(get("op_type", "self") or "self").strip(),
                str(get("category", "") or "").strip(),
                str(get("work_center", "") or "").strip(),
                round(float(get("setup_hours", 0) or 0), 4),
                round(float(get("unit_hours", 0) or 0), 4),
                bool(get("is_key", False)),
                bool(get("inspect_required", False)),
                str(get("sop", "") or "").strip(),
                str(get("remark", "") or "").strip(),
            )
        )
    rows.sort(key=lambda r: r[0])
    return rows


def _latest_baseline(db: Session, product: str, part: str) -> ProcessRoute | None:
    return db.scalars(
        select(ProcessRoute)
        .where(
            ProcessRoute.kind == KIND_BASELINE,
            ProcessRoute.product == product,
            ProcessRoute.part == part,
            ProcessRoute.status == "released",
        )
        .order_by(ProcessRoute.version.desc())
    ).first()


def edit_route_steps(
    db: Session,
    task: WorkTask,
    tr: TaskRoute,
    steps: list[dict],
    *,
    created_by: str = "operator",
    publish: bool = True,
) -> tuple[ProcessRoute, dict]:
    """在任务抽屉里直接改一条路线的工序，不打断正在做的事。

    两种路线，两种改法，这是整个模块最容易搞混的地方：

    **本单副本**（kind='task'）：它只属于这一个订单，就地改、改完即生效。
        不走「草稿 → 发布」那一套，因为副本天生是任务的一部分，没有审核流。

    **引用基线**（kind='route'）：它代表全厂标准，直接覆盖会把所有订单的
        比对基准悄悄改掉。所以走「升版 → 写工序 → 发布 → 把引用重指到新版」：
        旧版归档留痕，其它引用同一标准的任务一起换到新版本（这正是引用的价值）。
        值没变就直接返回，不白造一版。

    `publish=False` 时只生成新草稿、**不发布**：现行基准一个字节都不动，
    任务继续按旧版核对，草稿留给人确认（比如工艺员改完等主管批）。
    这是「已发布不允许随意修改」在界面上的落地方式 —— 修改可以，
    但要不要让它生效是另一个动作。
    """
    from . import routes as route_service

    ensure_task_editable(task)

    route = db.get(ProcessRoute, tr.route_id)
    if route is None:
        raise ValueError("路线不存在，可能已被删除，请刷新后重试")

    if tr.origin == ORIGIN_COPY or route.kind == KIND_TASK:
        route_service.save_steps(db, route, steps, allow_task=True)
        tr.seen_route_id = route.id
        tr.seen_at = dt.datetime.now()
        tr.seen_by = created_by
        db.flush()
        return route, {
            "mode": "copy",
            "synced_tasks": 0,
            "version": route.version,
            "effective": True,
        }

    if not steps:
        raise ValueError("标准工艺至少要保留一道工序；只想本单不做某道，请先「拆副本」")

    # 别人可能已经升过版，自己还引用着归档版：先跟上，别在旧版上再升一版
    latest = _latest_baseline(db, route.product, route.part)
    if latest is not None and latest.id != route.id:
        tr.route_id = latest.id
        route = latest
        db.flush()

    if _norm_steps(route.steps) == _norm_steps(steps):
        # 只是点开看了一眼就保存：等同于当场确认了这一版
        tr.seen_route_id = route.id
        tr.seen_at = dt.datetime.now()
        tr.seen_by = created_by
        db.flush()
        return route, {
            "mode": "baseline",
            "unchanged": True,
            "synced_tasks": 0,
            "version": route.version,
            "effective": True,
            "message": "内容没有变化，未生成新版本",
        }

    old_version, old_id = route.version, route.id
    draft = route_service.revision(db, route)
    route_service.save_steps(db, draft, steps)

    if not publish:
        db.flush()
        return draft, {
            "mode": "draft",
            "synced_tasks": 0,
            "from_version": old_version,
            "version": draft.version,
            "draft_id": draft.id,
            "effective": False,
            "message": (
                f"已生成草稿 v{draft.version}，尚未发布："
                f"所有订单仍按现行 v{old_version} 核对，发布后才切换"
            ),
        }

    # 发布前先记下「除了本单，还有谁在这版上」：发布后引用会被改指，就查不到了
    affected = _other_ref_tasks(db, old_id, exclude_task_id=task.id)
    route_service.publish(
        db, draft, published_by=created_by, action="edit",
        note=f"任务 {task.task_no} 内直接修改",
    )
    synced = db.execute(
        update(TaskRoute).where(TaskRoute.route_id == old_id).values(route_id=draft.id)
    ).rowcount
    tr.route_id = draft.id
    # 改的人就是确认的人；别的任务一律保持「未确认」，让它们的页面亮起提醒
    tr.seen_route_id = draft.id
    tr.seen_at = dt.datetime.now()
    tr.seen_by = created_by
    db.flush()
    return draft, {
        "mode": "baseline",
        "synced_tasks": int(synced or 0),
        "from_version": old_version,
        "version": draft.version,
        "effective": True,
        "affected_tasks": affected,
        "affected_count": len(affected),
    }


def _other_ref_tasks(
    db: Session, route_id: int, *, exclude_task_id: int | None = None, limit: int = 50
) -> list[dict]:
    """还有哪几个任务挂在这条路上（用于「你刚改了别人也在用的标准」提醒）。"""
    rows = db.execute(
        select(TaskRoute.task_id, WorkTask.task_no, TaskRoute.part)
        .join(WorkTask, WorkTask.id == TaskRoute.task_id)
        .where(TaskRoute.route_id == route_id, TaskRoute.status == "active")
        .order_by(WorkTask.id)
        .limit(max(1, limit))
    ).all()
    seen: set[int] = set()
    out: list[dict] = []
    for task_id, task_no, part in rows:
        if exclude_task_id is not None and task_id == exclude_task_id:
            continue
        if task_id in seen:
            continue
        seen.add(task_id)
        out.append({"task_id": task_id, "task_no": task_no, "part": part or ""})
    return out


# ------------------------------------------------------------------ 任务 CRUD
def create_task(
    db: Session,
    *,
    task_no: str = "",
    title: str = "",
    customer: str = "",
    product: str = "",
    batch_no: str = "",
    period_id: int | None = None,
    qty: float = 0.0,
    order_date: str = "",
    due_date: str = "",
    status: str = "open",
    source: str = "manual",
    remark: str = "",
    created_by: str = "operator",
    routes: list[dict] | None = None,
) -> WorkTask:
    """手工创建任务（订单）。任务号留空则按「产品代号-批次-期间」合成。

    可以先建空任务再加路线（订单刚下来、工艺还没定），也可以一次带上路线。
    产品认不出不拦着：`product_id` 留空，进待认领，别因为字典没维护就干不了活。
    """
    product = canonical_product(db, product)
    if not (task_no or "").strip():
        prod = resolve_product(db, product) if product else None
        code = (prod.code if prod and prod.code else "") or product or "TASK"
        no = make_task_no(code, batch_no, _period_ym(db, period_id))
    else:
        no = task_no.strip()
    no = _unique_task_no(db, no)

    prod = resolve_product(db, product) if product else None
    task = WorkTask(
        task_no=no,
        title=(title or "").strip() or (f"{product} {batch_no}".strip() if product else ""),
        customer=(customer or "").strip(),
        product=product,
        product_id=prod.id if prod else None,
        period_id=period_id,
        order_date=(order_date or "").strip(),
        due_date=(due_date or "").strip(),
        qty=float(qty or 0),
        status=status or "open",
        source=source,
        remark=(remark or "").strip(),
        created_by=created_by,
    )
    db.add(task)
    db.flush()

    if routes:
        for spec in routes:
            spec = dict(spec)
            spec.setdefault("batch_no", batch_no)
            if spec.get("qty") in (None, "", 0) and qty:
                spec["qty"] = qty
        add_routes(db, task, routes, created_by=created_by)
    else:
        refresh_task(db, task)
    return task


def delete_task(db: Session, task: WorkTask) -> dict:
    """删任务，并连带清掉它私有的副本。

    副本（kind='task'）只属于这个任务：任务没了，它就是孤儿 ——
    不能被别的任务引用（add_routes 明确禁止），也不出现在基线树里，
    只会在库里留下一堆查不到主人的工序行。所以删任务必须一起删。

    删除顺序不能反：先清 task_route 引用，再清工序行，最后删路线，
    否则会撞 `FOREIGN KEY constraint failed`（ORM 按 mapper 依赖排序，
    不会替我们考虑这条链）。
    """
    copies = db.scalars(
        select(ProcessRoute).where(
            ProcessRoute.kind == KIND_TASK, ProcessRoute.task_id == task.id
        )
    ).all()

    db.query(TaskRoute).filter(TaskRoute.task_id == task.id).delete(synchronize_session=False)
    db.flush()
    # 修订史是任务私有的：任务没了，版本记录也必须一起走，
    # 否则外键挡着删不掉（500），或者留下指向已删任务的孤儿版本
    db.query(TaskRevision).filter(TaskRevision.task_id == task.id).delete(
        synchronize_session=False
    )
    db.flush()

    deleted_copies = 0
    for c in copies:
        # 兜底：历史脏数据里可能有别的任务的 task_route 也指向这份副本
        db.query(TaskRoute).filter(TaskRoute.route_id == c.id).delete(synchronize_session=False)
        db.flush()
        db.query(ProcessRouteStep).filter(ProcessRouteStep.route_id == c.id).delete(
            synchronize_session=False
        )
        db.flush()
        db.delete(c)
        deleted_copies += 1
    db.flush()

    db.delete(task)
    db.flush()
    return {"deleted_copies": deleted_copies}


def update_task(
    db: Session, task: WorkTask, data: dict, *, user: str = "", note: str = ""
) -> dict:
    """改任务基本信息。**改口径就升版**，并把变了什么记下来。

    哪些算「口径」：任务号、订单名、客户、数量、下单日、交期、核算期间。
    这些字段决定成本归集与月度汇总怎么算，改了就是一次新的口径，
    事后问「这单当初按多少台下的」必须答得出来，所以要留版本。
    状态（草稿/在制/结案）与备注不算口径 —— 状态是流程动作，备注是说明，
    把它们也算进版本号，版本会退化成一串无意义的流水号。
    """
    before = {f: _task_field_value(db, task, f) for f in VERSIONED_TASK_FIELDS}

    for field in ("title", "customer", "order_date", "due_date", "status", "remark"):
        if field in data and data[field] is not None:
            setattr(task, field, str(data[field]).strip())
    if "qty" in data and data["qty"] is not None:
        task.qty = float(data["qty"] or 0)
    if "task_no" in data and data["task_no"]:
        no = str(data["task_no"]).strip()
        if no and no != task.task_no:
            # 手工改号撞号必须报错。这里若沿用建单时的「顺延」，
            # 用户以为改成了 A，实际被悄悄改成 A-2，账就对不上了。
            if db.scalars(
                select(WorkTask).where(WorkTask.task_no == no, WorkTask.id != task.id)
            ).first():
                raise ValueError(f"任务号 {no} 已被其它任务占用")
            task.task_no = no
    if "period_id" in data:
        task.period_id = data["period_id"]
    db.flush()

    after = {f: _task_field_value(db, task, f) for f in VERSIONED_TASK_FIELDS}
    changes = [
        {
            "field": f,
            "label": TASK_FIELD_LABELS[f],
            "old": before[f],
            "new": after[f],
        }
        for f in VERSIONED_TASK_FIELDS
        if before[f] != after[f]
    ]
    bumped = False
    if changes:
        task.version = int(task.version or 1) + 1
        task.revised_at = dt.datetime.now()
        task.revised_by = (user or "").strip() or "operator"
        db.add(
            TaskRevision(
                task_id=task.id,
                version=task.version,
                changes=json.dumps(changes, ensure_ascii=False),
                note=(note or "").strip(),
                user=task.revised_by,
            )
        )
        db.flush()
        bumped = True

    refresh_task(db, task)

    # 改核算期间会改变月度汇总的归属。已挂的旧单据仍在原期间，
    # 于是「单据属 A 月、任务属 B 月」—— 两边汇总同时失真，必须当场说。
    warnings: list[str] = []
    if before["period_id"] != after["period_id"] and task.period_id:
        cross = int(
            db.scalar(
                select(func.count())
                .select_from(Sheet)
                .where(
                    Sheet.task_id == task.id,
                    Sheet.status != "rejected",
                    Sheet.period_id != task.period_id,
                )
            )
            or 0
        )
        if cross:
            warnings.append(
                f"{cross} 张已归集单据仍属原期间（{before['period_id'] or '未定'}），"
                f"改成 {after['period_id']} 后它们与本任务跨期，两个月汇总都会变"
            )

    return {
        "version": int(task.version or 1),
        "bumped": bumped,
        "changes": changes,
        "warnings": warnings,
        # 一句话回执，界面直接念，不让人自己拼
        "message": (
            "已升到 v%d：%s"
            % (
                int(task.version or 1),
                "、".join(f"{c['label']} {c['old'] or '—'}→{c['new'] or '—'}" for c in changes),
            )
            if changes
            else "保存成功（口径未变，版本仍为 v%d）" % int(task.version or 1)
        ),
    }


def task_revisions(db: Session, task_id: int, limit: int = 50) -> list[dict]:
    """任务的修订历史，新版本在前。"""
    rows = db.scalars(
        select(TaskRevision)
        .where(TaskRevision.task_id == task_id)
        .order_by(TaskRevision.version.desc())
        .limit(limit)
    ).all()
    out = []
    for r in rows:
        try:
            changes = json.loads(r.changes or "[]")
        except (TypeError, ValueError):
            changes = []
        out.append(
            {
                "version": r.version,
                "changes": changes,
                "note": r.note,
                "user": r.user,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "summary": "、".join(
                    f"{c.get('label') or c.get('field')} {c.get('old') or '—'}→{c.get('new') or '—'}"
                    for c in changes
                ),
            }
        )
    return out


# ------------------------------------------------------------------ 单据归集
def _sheet_pairs(db: Session, sheet: Sheet) -> list[tuple[str, str]]:
    """单据里出现的（产品, 件名）组合。表头优先，明细兜底。"""
    try:
        head = json.loads(sheet.header_json or "{}")
    except (TypeError, ValueError):
        head = {}
    head_product = canonical_product(db, (head.get("product_name") or head.get("product_code") or ""))
    pairs: list[tuple[str, str]] = []
    for it in sorted(sheet.items or [], key=lambda x: x.seq or 0):
        product = canonical_product(db, (it.product_name or "").strip()) or head_product
        part = (it.part_name or "").strip()
        if not part and head.get("part_name"):
            part = str(head.get("part_name")).strip()
        if product and part and (product, part) not in pairs:
            pairs.append((product, part))
    if not pairs and head_product and head.get("part_name"):
        pairs.append((head_product, str(head["part_name"]).strip()))
    return pairs


def _find_task_for_sheet(db: Session, sheet: Sheet) -> WorkTask | None:
    """按「产品 + 批次 + 期间」找已有任务。这就是订单归集的口径。

    已结案/已取消的订单不再收货：结案意味着这批活已经交付并核算完，
    再把新单据挂上去，等于往一个已经封账的订单里塞成本。
    此时应当新建一个任务，而不是塞进旧任务。
    """
    try:
        head = json.loads(sheet.header_json or "{}")
    except (TypeError, ValueError):
        head = {}
    product = canonical_product(db, (head.get("product_name") or head.get("product_code") or ""))
    batch = (head.get("batch_no") or "").strip()
    if not product and not batch:
        return None
    stmt = select(WorkTask).where(
        WorkTask.status.notin_(("cancelled", "closed"))
    )
    if product:
        stmt = stmt.where(WorkTask.product == product)
    if batch:
        stmt = stmt.where(
            WorkTask.id.in_(
                select(TaskRoute.task_id).where(TaskRoute.batch_no == batch)
            )
            | WorkTask.id.in_(
                select(TaskRoute.task_id).where(TaskRoute.product == product)
            )
        )
    if sheet.period_id:
        stmt = stmt.where(
            or_(WorkTask.period_id == sheet.period_id, WorkTask.period_id.is_(None))
        )
    return db.scalars(stmt.order_by(WorkTask.id)).first()


def sync_sheet_task(
    db: Session, sheet: Sheet, *, create_missing: bool = True, created_by: str = "system"
) -> tuple[WorkTask | None, list[str]]:
    """把单据归到一个任务上（幂等），并补挂工艺引用。

    归集顺序：单据已指定任务 → 沿用；否则按（产品+批次+期间）找已有任务；
    还是没有才新建。这样同一订单的多张单不会各立一个「任务」。

    返回 `(task, warnings)`。warnings 不是可选的装饰 —— 单据确认后
    如果某个件名没有**已发布**工艺，这条单的实报就没法跟定额对照，
    必须让确认的人当场看见，而不是等月底汇总时才发现少了一条路线。

    这里**不自动生成基线**：机器按历史统计出来的工序如果直接发布，
    等于让没人看过的数字成为全厂核对基准。要建线请人工去编排页建并发布。
    """
    warnings: list[str] = []
    task = db.get(WorkTask, sheet.task_id) if sheet.task_id else None
    pairs = _sheet_pairs(db, sheet)

    if task is None:
        task = _find_task_for_sheet(db, sheet)
    if task is None:
        if not create_missing:
            return None, warnings
        try:
            head = json.loads(sheet.header_json or "{}")
        except (TypeError, ValueError):
            head = {}
        product = pairs[0][0] if pairs else canonical_product(
            db, (head.get("product_name") or head.get("product_code") or "")
        )
        task = create_task(
            db,
            product=product,
            batch_no=(head.get("batch_no") or "").strip(),
            period_id=sheet.period_id,
            title=f"{product} {(head.get('batch_no') or '').strip()}".strip(),
            status="open",
            source="sheet",
            created_by=created_by,
        )

    sheet.task_id = task.id
    # 单据里出现、任务里还没有的件名，补成路线引用（不复制）
    have = {
        tr.route_id
        for tr in db.scalars(
            select(TaskRoute).where(TaskRoute.task_id == task.id, TaskRoute.status == "active")
        ).all()
    }
    specs = []
    for product, part in pairs:
        baseline = _baseline_for(db, product, part)
        if baseline is not None and baseline.id in have:
            continue
        specs.append({"product": product, "part": part, "batch_no": (head_batch(sheet) or "")})
    if specs:
        try:
            out = add_routes(
                db, task, specs, created_by=created_by, create_baseline=False
            )
            for f in out.get("failed") or []:
                warnings.append(
                    f"{f.get('product') or '?'} / {f.get('part') or '?'}：{f.get('reason')}"
                )
        except ValueError as exc:
            # 任务已结案/已取消时 add_routes 会拒绝。单据归集本身不能因为
            # 补不了路线就整体失败 —— 归集是账务事实，工艺挂载是后续动作。
            warnings.append(str(exc))
    refresh_task(db, task)
    return task, warnings


def _scan_stats(db: Session, task_id: int) -> tuple[int, float]:
    """本任务扫码报工的（单数, 工时）。免得不打开明细就看不见这部分实报。"""
    row = db.execute(
        select(
            func.count(func.distinct(WorkItem.print_form_no)),
            func.coalesce(func.sum(WorkItem.total_hours), 0.0),
        ).where(WorkItem.task_id == task_id, WorkItem.sheet_id.is_(None))
    ).one()
    return int(row[0] or 0), round(float(row[1] or 0), 4)


def _task_brief(task: WorkTask | None) -> dict | None:
    if task is None:
        return None
    return {
        "id": task.id,
        "task_no": task.task_no,
        "title": task.title,
        "status": task.status,
        "sheet_count": task.sheet_count,
        "actual_hours": round(float(task.actual_hours or 0), 4),
        "pending_count": task.pending_count or 0,
        "pending_hours": round(float(task.pending_hours or 0), 4),
    }


def _task_route_pairs(db: Session, task_id: int) -> set[tuple[str, str]]:
    """任务当下已经挂着的（产品, 件名）。用来判断改挂后这张单有没有工艺可对照。"""
    return {
        (tr.product, tr.part)
        for tr in db.scalars(
            select(TaskRoute).where(TaskRoute.task_id == task_id, TaskRoute.status == "active")
        ).all()
    }


def move_sheet_task(
    db: Session,
    sheet: Sheet,
    target_task_id: int | None,
    *,
    user: str = "operator",
    attach_routes: bool = False,
    force: bool = False,
) -> dict:
    """把一张单据改挂到另一个订单任务（target 为空 = 从任务上摘下来）。

    为什么必须有这个入口：归集是**自动**的（同产品 + 同批次 + 同期间），
    自动就一定会错 —— 同规格两批活分给了两个订单、批次号抄错、补录时期间填错。
    没有入口时，「归错了」只能改库，等于把账务归属交给一个敢写 SQL 的人。

    改挂不是改一个外键那么轻，它一次动三样东西：
    1. 两个任务的物化字段（单据数、实报工时）必须重算，否则列表上的数字骗人；
    2. 新任务的工艺参照面 —— 这个件名在新订单下有没有路线，没有就核不出差异；
    3. 责任留痕（谁、何时、从哪挪到哪），这是账务动作，必须有据可查（由接口层写审计）。

    拒绝的三种情况都有明确业务理由，不是技术上的不方便：
    - 核算期已锁定：那个月的账已经封了，再改归属等于事后改结算；
    - 目标任务已结案/取消：往封账订单里塞单据（与自动归集同一口径）；
    - 跨期改挂：单据属 A 月、任务属 B 月，两边月度汇总会同时失真。
      确有必要时 `force=True` 放行，但结果必须带着 warning 回来，不能悄悄过。
    """
    if sheet.period_id:
        period = db.get(Period, sheet.period_id)
        if period is not None and period.status == "locked":
            raise ValueError(f"核算期 {period.ym} 已锁定，不能改挂任务（需先解锁该期）")

    old = db.get(WorkTask, sheet.task_id) if sheet.task_id else None
    target = db.get(WorkTask, target_task_id) if target_task_id else None
    if target_task_id and target is None:
        raise ValueError("目标任务不存在，可能已被删除")
    if old is not None and target is not None and old.id == target.id:
        return {
            "ok": True,
            "unchanged": True,
            "sheet_id": sheet.id,
            "from_task": _task_brief(old),
            "to_task": _task_brief(target),
            "checks": [],
            "notices": [],
            "notice_count": 0,
            "message": "这张单本来就挂在这个订单上，无需改动",
        }
    if target is not None and target.status in TASK_LOCKED_STATUS:
        state = "已结案" if target.status == "closed" else "已取消"
        raise ValueError(
            f"订单 {target.task_no} {state}，不再接收单据；请改挂到在制订单，或先恢复该订单状态"
        )
    if (
        target is not None
        and target.period_id
        and sheet.period_id
        and target.period_id != sheet.period_id
    ):
        tp = db.get(Period, target.period_id)
        sp = db.get(Period, sheet.period_id)
        msg = (
            f"跨期改挂：单据属 {sp.ym if sp else sheet.period_id}，"
            f"目标任务属 {tp.ym if tp else target.period_id}，两个月的汇总都会变"
        )
        if not force:
            raise ValueError(msg + "；确认无误请勾选「跨期改挂」后再提交")

    checks: list[dict] = []
    pairs = _sheet_pairs(db, sheet)

    sheet.task_id = target.id if target is not None else None
    db.flush()

    attached = 0
    if target is not None:
        if attach_routes and pairs:
            have = _task_route_pairs(db, target.id)
            specs = [
                {"product": p, "part": pt, "batch_no": head_batch(sheet)}
                for p, pt in pairs
                if (p, pt) not in have
            ]
            if specs:
                try:
                    out = add_routes(
                        db, target, specs, created_by=user, create_baseline=False
                    )
                    attached = int(out.get("added") or 0) + int(out.get("reactivated") or 0)
                    for f in out.get("failed") or []:
                        checks.append(
                            {
                                "level": "warn",
                                "code": "route_attach_failed",
                                "message": (
                                    f"{f.get('product') or '?'} / {f.get('part') or '?'} "
                                    f"补挂工艺失败：{f.get('reason')}"
                                ),
                            }
                        )
                except ValueError as exc:
                    checks.append(
                        {
                            "level": "warn",
                            "code": "route_attach_failed",
                            "message": f"补挂工艺失败：{exc}",
                        }
                    )
        # 改挂完再查一次：勾了补挂也勾了但没基线时，这里仍会如实报缺口
        have_pairs = _task_route_pairs(db, target.id)
        for p, pt in pairs:
            if (p, pt) in have_pairs:
                continue
            base = _baseline_for(db, p, pt)
            if base is None:
                checks.append(
                    {
                        "level": "warn",
                        "code": "no_baseline",
                        "message": (
                            f"{p} / {pt} 还没有已发布的工艺基线，"
                            "这张单的实报暂时没有定额可对照"
                        ),
                    }
                )
            else:
                checks.append(
                    {
                        "level": "warn",
                        "code": "no_route_ref",
                        "message": (
                            f"新订单里没有 {p} / {pt} 的工艺引用（基线 v{base.version} 已存在），"
                            "可勾选「同时补挂工艺引用」一并处理"
                        ),
                    }
                )
        refresh_task(db, target)

    if old is not None:
        refresh_task(db, old)
        if old.sheet_count == 0:
            left = db.scalar(
                select(func.count())
                .select_from(TaskRoute)
                .where(TaskRoute.task_id == old.id, TaskRoute.status == "active")
            )
            checks.append(
                {
                    "level": "info",
                    "code": "from_task_empty",
                    "message": (
                        f"原订单 {old.task_no} 已没有单据"
                        + ("（也无工艺路线）" if not left else "")
                        + "，如确已完工可结案，避免它继续出现在在制列表里"
                    ),
                }
            )

    if target is None:
        checks.append(
            {
                "level": "info",
                "code": "detached",
                "message": (
                    "已从订单上摘下：该单不再计入任何任务的实报。"
                    "注意下次「确认」时系统会按产品+批次+期间重新自动归集"
                ),
            }
        )
    if target is not None and target.period_id and sheet.period_id and target.period_id != sheet.period_id:
        tp = db.get(Period, target.period_id)
        sp = db.get(Period, sheet.period_id)
        checks.insert(
            0,
            {
                "level": "warn",
                "code": "cross_period",
                "message": (
                    f"已跨期改挂：单据 {sp.ym if sp else ''} → 任务 {tp.ym if tp else ''}，"
                    "请确认两个月的工时汇总都符合预期"
                ),
            },
        )

    notices = version_alerts(db, task_id=target.id) if target is not None else []
    if notices:
        checks.insert(
            0,
            {
                "level": "warn",
                "code": "version_notice",
                "message": (
                    f"新订单有 {len(notices)} 条工艺升版待确认，"
                    "这张单按哪一版核定额要先认下来"
                ),
            },
        )

    action = "detach" if target is None else ("move" if old is not None else "attach")
    label = {
        "detach": "已摘下任务归属",
        "move": f"已从 {old.task_no} 改挂到 {target.task_no}" if old and target else "已改挂",
        "attach": f"已归集到 {target.task_no}" if target else "已归集",
    }[action]
    return {
        "ok": True,
        "sheet_id": sheet.id,
        "action": action,
        "from_task": _task_brief(old),
        "to_task": _task_brief(target),
        "attached_routes": attached,
        "checks": checks,
        "notices": notices,
        "notice_count": len(notices),
        "message": label,
    }


def move_sheets_task(
    db: Session,
    sheet_ids: list[int],
    target_task_id: int | None,
    *,
    user: str = "operator",
    attach_routes: bool = False,
    force: bool = False,
) -> dict:
    """批量改挂。逐张独立成败：一张跨期被拒，不该拖累另外九张。"""
    moved: list[dict] = []
    failed: list[dict] = []
    checks: list[dict] = []
    for sid in sheet_ids:
        sheet = db.get(Sheet, sid)
        if sheet is None:
            failed.append({"sheet_id": sid, "reason": "单据不存在"})
            continue
        try:
            r = move_sheet_task(
                db,
                sheet,
                target_task_id,
                user=user,
                attach_routes=attach_routes,
                force=force,
            )
        except ValueError as exc:
            failed.append({"sheet_id": sid, "reason": str(exc)})
            continue
        moved.append(r)
        for c in r.get("checks") or []:
            checks.append({**c, "sheet_id": sid})
    return {
        "ok": True,
        "moved": len(moved),
        "failed": failed,
        "results": moved,
        "checks": checks,
        "to_task": moved[0]["to_task"] if moved else None,
    }


def head_batch(sheet: Sheet) -> str:
    try:
        return str(json.loads(sheet.header_json or "{}").get("batch_no") or "").strip()
    except (TypeError, ValueError):
        return ""


def _baseline_for(db: Session, product: str, part: str) -> ProcessRoute | None:
    from . import routes as route_service

    return route_service.pick_baseline(db, product, part)


# ------------------------------------------------- 升版提醒（引用漂移）
STATE_LABEL = {
    "upgraded": "已升版",
    "unconfirmed": "待确认",
    "draft": "引用草稿",
    "archived": "引用已归档",
    "missing": "路线缺失",
}


def _release_index(db: Session, pairs: set[tuple[str, str]] | None = None) -> dict[tuple[str, str], ProcessRoute]:
    """各组合当前生效的已发布基线。一次查完，别在循环里逐条 find。"""
    stmt = select(ProcessRoute).where(
        ProcessRoute.kind == KIND_BASELINE, ProcessRoute.status == "released"
    )
    if pairs:
        stmt = stmt.where(ProcessRoute.product.in_({p for p, _ in pairs} or {""}))
    out: dict[tuple[str, str], ProcessRoute] = {}
    for r in db.scalars(stmt).all():
        out[(r.product, r.part)] = r  # uq_route_released 保证组合唯一
    return out


def _ref_state(
    tr: TaskRoute,
    route: ProcessRoute | None,
    current: ProcessRoute | None,
    notice: dict | None,
) -> dict | None:
    """一条引用是否需要人确认，以及为什么。

    四种要提醒的情形，前三种是「基准换了」，第四种是「换了但没人点头」：

    * `upgraded`    —— 本单还引用旧版，组合已经发布新版；
    * `draft`/`archived` —— 引用指向一条已经不当基准的版本（数据欠账）；
    * `unconfirmed` —— 引用已被系统切到新版，但本单没有人确认过。
    """
    if route is None:
        return {
            "pending": True,
            "reason": "missing",
            "label": STATE_LABEL["missing"],
            "message": "引用的路线已不存在，本单没有可用定额；请重新引用基线或拆副本",
        }

    base = {
        "ref_id": route.id,
        "ref_version": route.version,
        "ref_status": route.status,
        "current_id": current.id if current else None,
        "current_version": current.version if current else None,
        "notice": notice,
    }

    if route.kind != KIND_BASELINE:
        # 副本/手建路线本来就与基线无关，不参与升版提醒
        return None

    # 组合已经有新的生效基准 → 一律按「已升版」说，哪怕手上这版刚好被归档了。
    # 先判「归档」会把最该说的一句话吞掉：用户真正要知道的是「有新版了」。
    if current is not None and current.id != route.id:
        return {
            **base,
            "pending": True,
            "reason": "upgraded",
            "label": STATE_LABEL["upgraded"],
            "message": (
                f"该件名基线已升到 v{current.version}，本单仍按 v{route.version} 核对"
                + (f"（{_notice_brief(notice)}）" if notice else "")
            ),
        }

    if route.status != "released":
        state = "draft" if route.status == "draft" else "archived"
        tip = (
            f"本单引用的是草稿 v{route.version}，草稿不能作为核对基准"
            if state == "draft"
            else f"本单引用的 v{route.version} 已归档，当前没有可用的核对基准"
        )
        return {**base, "pending": True, "reason": state, "label": STATE_LABEL[state], "message": tip}

    if tr.seen_route_id != tr.route_id:
        return {
            **base,
            "pending": True,
            "reason": "unconfirmed",
            "label": STATE_LABEL["unconfirmed"],
            "message": (
                f"本单工艺已随基线升版切到 v{route.version}，尚未确认"
                + (f"（{_notice_brief(notice)}）" if notice else "")
            ),
        }
    return None


def _notice_brief(notice: dict | None) -> str:
    if not notice:
        return ""
    bits = [f"v{notice.get('from_version')} → v{notice.get('to_version')}"]
    if notice.get("actor"):
        bits.append(f"{notice['actor']} 发布")
    at = (notice.get("at") or "").replace("T", " ")[:16]
    if at:
        bits.append(at)
    delta = notice.get("unit_delta") or 0
    if abs(delta) >= 0.0001:
        bits.append(f"单件 {delta:+.3f}h".replace("+-", "-"))
    return " · ".join(bits)


def ref_states(db: Session, trs: list[TaskRoute]) -> dict[int, dict]:
    """批量算出一批引用路线的升版状态（只算 active 的基线引用）。"""
    from . import routes as route_service

    live = [t for t in trs if t.status == "active" and t.origin == ORIGIN_BASELINE]
    if not live:
        return {}

    pairs = {(t.product or "", t.part or "") for t in live}
    index = _release_index(db, pairs)
    notice_map = route_service.notices_by_pair(db, list(pairs))
    route_ids = {t.route_id for t in live}
    routes = {
        r.id: r
        for r in db.scalars(select(ProcessRoute).where(ProcessRoute.id.in_(route_ids))).all()
    }

    out: dict[int, dict] = {}
    for tr in live:
        route = routes.get(tr.route_id)
        pair = (tr.product or "", tr.part or "")
        if route is not None:
            pair = (route.product, route.part)
        current = index.get(pair)
        state = _ref_state(tr, route, current, notice_map.get(pair))
        if state:
            out[tr.id] = state
    return out


def version_alerts(db: Session, *, task_id: int | None = None) -> list[dict]:
    """待确认升版的引用清单（可按任务过滤），供待办与页面提醒。"""
    stmt = select(TaskRoute).where(
        TaskRoute.status == "active", TaskRoute.origin == ORIGIN_BASELINE
    )
    if task_id is not None:
        stmt = stmt.where(TaskRoute.task_id == task_id)
    trs = db.scalars(stmt.order_by(TaskRoute.task_id, TaskRoute.seq)).all()
    states = ref_states(db, list(trs))
    out: list[dict] = []
    for tr in trs:
        state = states.get(tr.id)
        if not state:
            continue
        task = db.get(WorkTask, tr.task_id)
        out.append(
            {
                "task_id": tr.task_id,
                "task_no": task.task_no if task else "",
                "task_status": task.status if task else "",
                "task_route_id": tr.id,
                "product": tr.product,
                "part": tr.part,
                **state,
            }
        )
    return out


def ack_version(
    db: Session, task: WorkTask, tr: TaskRoute, *, mode: str = "adopt", user: str = "operator"
) -> dict:
    """确认一条引用的升版。两种口径，对应现场两种真实选择：

    * `adopt` —— 认可新版：引用切到现行已发布版，并记住「本单确认到这一版」。
    * `keep`  —— 本单继续按现在这版做：把当前引用的内容**固化成副本**，
      从此与基线脱钩（副本不受升版影响，也不该再被提醒）。这条路径正是
      「已开工的单不能因为标准改了就被悄悄改定额」的出口。

    确认动作本身要留痕（谁、什么时候、确认到哪一版），否则下次升版提醒时
    谁也说不清上一版到底有没有人看过。
    """
    ensure_task_editable(task)
    route = db.get(ProcessRoute, tr.route_id)
    if route is None:
        raise ValueError("这条引用指向的路线已不存在，请先移除再重新引用基线")

    if mode == "keep":
        if tr.origin == ORIGIN_COPY:
            tr.seen_route_id = tr.route_id
            tr.seen_at = dt.datetime.now()
            tr.seen_by = user
            db.flush()
            return {"mode": "keep", "route_id": tr.route_id, "already_copy": True}
        # 「保留现版」要保住的是本单**当初按着干的那一版**（seen_route_id），
        # 不是系统刚重指过去的新版 —— 那正是这张单不接受的那一版。
        source = route
        if tr.seen_route_id:
            cand = db.get(ProcessRoute, tr.seen_route_id)
            if cand is not None and cand.kind == KIND_BASELINE:
                source = cand
        if source.id != tr.route_id:
            tr.route_id = source.id
            db.flush()
        copy = fork_route(db, tr, created_by=user, allow_archived=True)
        tr.seen_route_id = copy.id
        tr.seen_at = dt.datetime.now()
        tr.seen_by = user
        db.flush()
        return {
            "mode": "keep",
            "route_id": copy.id,
            "copy_version": copy.version,
            "from_version": copy.snapshot_version,
        }

    current = _baseline_for(db, tr.product or route.product, tr.part or route.part)
    if current is None:
        raise ValueError(
            "该产品×件名当前没有已发布的基线，无法切到新版；"
            "请先在「工艺及工时编排」发布，或选择「本单保留现版」（转副本）"
        )
    old_version = route.version
    tr.route_id = current.id
    tr.seen_route_id = current.id
    tr.seen_at = dt.datetime.now()
    tr.seen_by = user
    db.flush()
    return {
        "mode": "adopt",
        "route_id": current.id,
        "from_version": old_version,
        "version": current.version,
        "changed": current.id != route.id,
    }


def ack_all(db: Session, task: WorkTask, *, mode: str = "adopt", user: str = "operator") -> dict:
    """一键确认本任务下所有待确认的引用。"""
    alerts = version_alerts(db, task_id=task.id)
    done, failed = [], []
    for a in alerts:
        tr = db.get(TaskRoute, a["task_route_id"])
        if tr is None:
            continue
        try:
            out = ack_version(db, task, tr, mode=mode, user=user)
            done.append({"part": a["part"], **out})
        except ValueError as exc:
            failed.append({"part": a["part"], "reason": str(exc)})
    db.flush()
    return {"acked": len(done), "failed": failed, "items": done}


def rebuild_tasks(db: Session) -> dict:
    """按现有单据重算任务归集与聚合。幂等，不动手填的订单字段。"""
    created = attached = 0
    for sheet in db.scalars(select(Sheet).where(Sheet.status != "rejected")).all():
        before = sheet.task_id
        task, _warnings = sync_sheet_task(db, sheet)
        if task is None:
            continue
        if before is None:
            created += 1
        else:
            attached += 1
    for task in db.scalars(select(WorkTask)).all():
        refresh_task(db, task)
    db.flush()
    return {"created": created, "refreshed": attached}


# ------------------------------------------------------------------ 闭环待办
def business_todos(db: Session) -> dict:
    """把散在各部门的「没做完的事」收成一张清单。

    系统里每加一条规则，就会产生一类「不满足规则的数据」。如果这些只藏在
    各页面的角标里，规则就形同虚设 —— 谁也不会天天去翻工艺页找哪条没发布。
    所以这里统一回答：现在有哪些事必须有人处理，各自去哪处理。

    只统计**真的需要动手**的，不统计「可能想看看」的：
    待办里塞满可选项，人就会整体忽略它。
    """
    from . import routes as route_service

    items: list[dict] = []

    def add(key, title, count, desc, link, level="warn"):
        if count:
            items.append(
                {"key": key, "title": title, "count": int(count), "desc": desc, "link": link, "level": level}
            )

    # 1. 引用了未发布/非基线的路线：违反「只能引用已发布工艺」
    rows = db.execute(
        select(TaskRoute.origin, ProcessRoute.kind, ProcessRoute.status)
        .join(ProcessRoute, ProcessRoute.id == TaskRoute.route_id)
        .where(TaskRoute.status == "active")
    ).all()
    bad_ref = sum(
        1
        for origin, kind, status in rows
        if origin == ORIGIN_BASELINE and (kind != KIND_BASELINE or status != "released")
    )
    add(
        "unpublished_ref",
        "引用了未发布的工艺路线",
        bad_ref,
        "这些任务引用的路线不是「已发布基线」，实报没有合法核对基准。",
        "/routes",
        "error",
    )

    # 2. 在制任务没有任何工艺路线
    no_route = db.scalar(
        select(func.count())
        .select_from(WorkTask)
        .where(
            WorkTask.status.in_(("draft", "open")),
            ~WorkTask.id.in_(
                select(TaskRoute.task_id).where(TaskRoute.status == "active")
            ),
        )
    )
    add(
        "task_no_route",
        "在制任务没有工艺路线",
        no_route,
        "没有路线就没有定额，实报工时无法与标准对照。",
        "/tasks",
    )

    # 3. 已确认单据没归到任务
    orphan_sheet = db.scalar(
        select(func.count())
        .select_from(Sheet)
        .where(Sheet.status == "verified", Sheet.task_id.is_(None))
    )
    add("sheet_no_task", "已确认单据没有归属任务", orphan_sheet, "单据无法计入订单成本。", "/sheets")

    # 4. 副本与基线一致，可合并回引用
    copies = route_service.copies_list(db, limit=1000)
    add(
        "copy_mergeable",
        "与基线一致的副本可合并",
        copies["identical_count"],
        "这些副本和现行基线一模一样，留着只会让人以为这一单有特殊工艺。",
        "/routes",
        "info",
    )

    # 4.5 基线升版后，引用处还没人确认 —— 这条是「提醒不被淹没」的关键：
    # 各页面都会标红，但只有待办能回答「一共有多少、去处理」。
    alert_rows = version_alerts(db)
    add(
        "version_notice",
        "基线升版待确认",
        len({a["task_route_id"] for a in alert_rows}),
        "这些订单引用的工艺基线已经升版（或引用指向了非现行版本），"
        "定额可能已变；请到任务里确认采用新版，或让本单保留现版（转副本）。",
        "/tasks",
        "error",
    )

    # 5. 从没发布过的组合：这些零件根本没有可引用的标准
    published_pairs = {
        (r.product, r.part)
        for r in db.scalars(
            select(ProcessRoute).where(
                ProcessRoute.kind == KIND_BASELINE, ProcessRoute.status == "released"
            )
        ).all()
    }
    draft_pairs = {
        (r.product, r.part)
        for r in db.scalars(
            select(ProcessRoute).where(
                ProcessRoute.kind == KIND_BASELINE, ProcessRoute.status == "draft"
            )
        ).all()
    }
    add(
        "draft_only",
        "只有草稿、尚无可用标准的组合",
        len(draft_pairs - published_pairs),
        "草稿不能被任务引用；要么发布，要么删除。",
        "/routes",
    )

    # 6. 已发布却是空工序（理论上不该出现，出现了说明有人绕过接口写库）
    empty_released = 0
    for r in db.scalars(
        select(ProcessRoute).where(
            ProcessRoute.kind == KIND_BASELINE, ProcessRoute.status == "released"
        )
    ).all():
        if not r.steps:
            empty_released += 1
    add(
        "empty_released",
        "已发布但没有工序的路线",
        empty_released,
        "空基准会把单据里的每道工序都判成「多做的」。",
        "/routes",
        "error",
    )

    items.sort(key=lambda x: ({"error": 0, "warn": 1, "info": 2}.get(x["level"], 3), -x["count"]))
    return {
        "total": sum(x["count"] for x in items),
        "item_count": len(items),
        "items": items,
    }


# ------------------------------------------------------------------ 查询
def task_rows(
    db: Session,
    *,
    q: str = "",
    status: str = "",
    product: str = "",
    origin: str = "",
    only_no_route: bool = False,
    only_no_sheet: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """任务列表。一律以任务为主表，路线与单据的数据一次批量算完。"""
    stmt = select(WorkTask)
    if status:
        stmt = stmt.where(WorkTask.status == status)
    if product:
        stmt = stmt.where(
            or_(WorkTask.product == product, WorkTask.product_id == _as_int(product))
        )
    if q:
        like = f"%{q.strip()}%"
        task_ids_by_route = select(TaskRoute.task_id).where(
            or_(TaskRoute.product.like(like), TaskRoute.part.like(like))
        )
        stmt = stmt.where(
            or_(
                WorkTask.task_no.like(like),
                WorkTask.title.like(like),
                WorkTask.customer.like(like),
                WorkTask.product.like(like),
                WorkTask.id.in_(task_ids_by_route),
            )
        )
    if only_no_route:
        stmt = stmt.where(
            ~select(TaskRoute.id)
            .where(TaskRoute.task_id == WorkTask.id, TaskRoute.status == "active")
            .exists()
        )
    if only_no_sheet:
        stmt = stmt.where(WorkTask.sheet_count == 0)
    if origin:
        stmt = stmt.where(
            select(TaskRoute.id)
            .where(
                TaskRoute.task_id == WorkTask.id,
                TaskRoute.status == "active",
                TaskRoute.origin == origin,
            )
            .exists()
        )

    base = stmt.subquery()
    total = db.scalar(select(func.count()).select_from(base)) or 0
    rows = db.scalars(stmt.order_by(WorkTask.id.desc()).limit(limit).offset(offset)).all()
    ids = [t.id for t in rows]

    route_stat: dict[int, dict] = {}
    if ids:
        trs = db.scalars(
            select(TaskRoute)
            .where(TaskRoute.task_id.in_(ids), TaskRoute.status == "active")
            .order_by(TaskRoute.seq, TaskRoute.id)
        ).all()
        totals = _route_totals(db, [t.route_id for t in trs])
        # 待确认升版：列表页也要能一眼看出来，别等点进抽屉才发现
        states = ref_states(db, list(trs))
        for tr in trs:
            slot = route_stat.setdefault(
                tr.task_id,
                {"count": 0, "steps": 0, "hours": 0.0, "parts": [], "origins": set(),
                 "notices": 0, "notice_msg": "", "notice_part": ""},
            )
            cnt, hours = totals.get(tr.route_id, (0, 0.0))
            slot["count"] += 1
            slot["steps"] += cnt
            slot["hours"] = round(slot["hours"] + hours, 4)
            slot["origins"].add(tr.origin)
            if tr.part:
                slot["parts"].append(tr.part)
            state = states.get(tr.id)
            if state:
                slot["notices"] += 1
                if not slot["notice_msg"]:
                    slot["notice_msg"] = state.get("message", "")
                    slot["notice_part"] = tr.part or ""

    summary_actual = float(
        db.scalar(select(func.coalesce(func.sum(base.c.actual_hours), 0.0))) or 0.0
    )
    summary_pending = float(
        db.scalar(select(func.coalesce(func.sum(base.c.pending_hours), 0.0))) or 0.0
    )
    with_route = int(
        db.scalar(
            select(func.count(func.distinct(TaskRoute.task_id))).where(
                TaskRoute.task_id.in_(select(base.c.id)), TaskRoute.status == "active"
            )
        )
        or 0
    )

    # 选任务时要能一眼看出「这单和它是不是同一个月」，所以顺手带上期间
    _period_ids = {t.period_id for t in rows if t.period_id}
    _period_ym = (
        {
            p.id: p.ym
            for p in db.scalars(select(Period).where(Period.id.in_(_period_ids))).all()
        }
        if _period_ids
        else {}
    )

    items = []
    for t in rows:
        st = route_stat.get(
            t.id,
            {"count": 0, "steps": 0, "hours": 0.0, "parts": [], "origins": set(),
             "notices": 0, "notice_msg": "", "notice_part": ""},
        )
        items.append(
            {
                "id": t.id,
                "task_no": t.task_no,
                "title": t.title,
                "customer": t.customer,
                "product": t.product,
                "product_id": t.product_id,
                "period_id": t.period_id,
                "period_ym": _period_ym.get(t.period_id, ""),
                "order_date": t.order_date,
                "due_date": t.due_date,
                "qty": round(float(t.qty or 0), 4),
                "actual_hours": round(float(t.actual_hours or 0), 4),
                "sheet_count": t.sheet_count,
                # 待核对与实报分开：界面上要能看出「还有几张小单没入账」
                "pending_hours": round(float(t.pending_hours or 0), 4),
                "pending_count": t.pending_count or 0,
                "status": t.status,
                "source": t.source,
                "remark": t.remark,
                # 版本号与最近一次修订：列表上用 vN 标出来，hover 能说清是谁改的
                "version": int(t.version or 1),
                "revised_at": t.revised_at.isoformat() if t.revised_at else None,
                "revised_by": t.revised_by,
                "route_count": st["count"],
                "step_count": st["steps"],
                "route_hours": round(st["hours"], 4),
                "parts": st["parts"],
                # 有没有本地副本：有说明这单的工艺确实偏离了标准，值得看一眼
                "has_copy": ORIGIN_COPY in st["origins"],
                # 待确认升版的条数与第一条原因（hover 能看到具体是哪件、哪一版）
                "notice_count": st["notices"],
                "notice_msg": st["notice_msg"],
                "notice_part": st["notice_part"],
                "unclaimed": t.product_id is None,
            }
        )
    return {
        "total": int(total),
        "items": items,
        "summary": {
            "task_count": int(total),
            "with_route": with_route,
            "no_route": int(total) - with_route,
            "actual_hours": round(summary_actual, 4),
            "pending_hours": round(summary_pending, 4),
            "stale_tasks": sum(1 for v in route_stat.values() if v["notices"]),
            "open": int(
                db.scalar(
                    select(func.count()).select_from(base).where(base.c.status == "open")
                )
                or 0
            ),
        },
    }


def task_detail(db: Session, task: WorkTask) -> dict:
    """任务详情：路线清单（含工序）、单据清单、逐路线与基线的差异。"""
    from . import routes as route_service

    trs = db.scalars(
        select(TaskRoute)
        .where(TaskRoute.task_id == task.id)
        .order_by(TaskRoute.status, TaskRoute.seq, TaskRoute.id)
    ).all()
    active = [t for t in trs if t.status == "active"]
    route_ids = [t.route_id for t in trs]
    totals = _route_totals(db, route_ids)
    ref_counts = _route_ref_counts(db, route_ids)

    sheets = db.scalars(select(Sheet).where(Sheet.task_id == task.id).order_by(Sheet.id)).all()
    # 每张单据按件名汇总实报，用于核对「这条路线实际做了多少」
    actual_by_pair: dict[tuple[str, str], float] = defaultdict(float)
    for s in sheets:
        for it in s.items or []:
            key = (
                canonical_product(db, (it.product_name or "").strip()),
                (it.part_name or "").strip(),
            )
            actual_by_pair[key] = round(
                actual_by_pair[key] + float(it.total_hours or 0), 4
            )

    routes_payload = []
    states = ref_states(db, list(trs))
    seen_ids = {tr.seen_route_id for tr in trs if tr.seen_route_id}
    seen_versions = {
        r.id: r.version
        for r in db.scalars(select(ProcessRoute).where(ProcessRoute.id.in_(seen_ids))).all()
    } if seen_ids else {}
    for tr in trs:
        route = db.get(ProcessRoute, tr.route_id)
        cnt, hours = totals.get(tr.route_id, (0, 0.0))
        baseline = _baseline_for(db, tr.product, tr.part)
        diff = None
        if route is not None and baseline is not None and route.id != baseline.id:
            diff = route_service.diff_routes(db, baseline, route)
        routes_payload.append(
            {
                "id": tr.id,
                "route_id": tr.route_id,
                "origin": tr.origin,
                "status": tr.status,
                "seq": tr.seq,
                "product": tr.product,
                "part": tr.part,
                "batch_no": tr.batch_no,
                "qty": round(float(tr.qty or 0), 4),
                "note": tr.note,
                "step_count": cnt,
                "unit_hours": hours,
                "actual_hours": round(actual_by_pair.get((tr.product, tr.part), 0.0), 4),
                "route_status": route.status if route else "",
                "route_version": route.version if route else None,
                "route_kind": route.kind if route else "",
                "snapshot_of_id": route.snapshot_of_id if route else None,
                "snapshot_version": route.snapshot_version if route else None,
                "has_baseline": baseline is not None,
                "baseline_id": baseline.id if baseline else None,
                "baseline_version": baseline.version if baseline else None,
                # 这条路线被多少个在制任务引用：改基线前必须让人看见影响面
                "ref_count": ref_counts.get(tr.route_id, 0),
                # 升版提醒：待确认的引用要带着「为什么」到界面上，不能只给个红点
                "stale": states.get(tr.id),
                "seen_version": seen_versions.get(tr.seen_route_id),
                "diff": diff,
            }
        )

    notices = [
        {
            "task_route_id": tr.id,
            "part": tr.part,
            "product": tr.product,
            **states[tr.id],
        }
        for tr in trs
        if tr.id in states
    ]

    # 批次是路线级的（同一订单不同件名可能分批），表头展示去重后的写法
    batches: list[str] = []
    for tr in active:
        b = (tr.batch_no or "").strip()
        if b and b not in batches:
            batches.append(b)

    return {
        "id": task.id,
        "task_no": task.task_no,
        "title": task.title,
        "customer": task.customer,
        "product": task.product,
        "product_id": task.product_id,
        "period_id": task.period_id,
        "period_ym": _period_ym(db, task.period_id),
        "batch_no": " / ".join(batches),
        "order_date": task.order_date,
        "due_date": task.due_date,
        "qty": round(float(task.qty or 0), 4),
        "actual_hours": round(float(task.actual_hours or 0), 4),
        "sheet_count": task.sheet_count,
        "scan_count": _scan_stats(db, task.id)[0],
        "scan_hours": _scan_stats(db, task.id)[1],
        "pending_hours": round(float(task.pending_hours or 0), 4),
        "pending_count": task.pending_count or 0,
        "status": task.status,
        "source": task.source,
        "remark": task.remark,
        "created_by": task.created_by,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        # 版本号：v1 = 建单原样，每次改口径 +1，改了什么见 revisions
        "version": int(task.version or 1),
        "revised_at": task.revised_at.isoformat() if task.revised_at else None,
        "revised_by": task.revised_by,
        "revisions": task_revisions(db, task.id, limit=20),
        "routes": routes_payload,
        # 待确认升版清单：抽屉顶部要用红字全部列出来，不能指望用户逐行找红点
        "notices": notices,
        "summary": {
            "route_count": len(active),
            "removed_count": len(trs) - len(active),
            "step_count": sum(r["step_count"] for r in routes_payload if r["status"] == "active"),
            "unit_hours": round(
                sum(r["unit_hours"] for r in routes_payload if r["status"] == "active"), 4
            ),
            "actual_hours": round(float(task.actual_hours or 0), 4),
            "pending_hours": round(float(task.pending_hours or 0), 4),
            "pending_count": task.pending_count or 0,
            "copy_count": sum(1 for r in active if r.origin == ORIGIN_COPY),
            "no_baseline": sum(
                1 for r in routes_payload if r["status"] == "active" and not r["has_baseline"]
            ),
            "notice_count": len(notices),
        },
        "sheets": [
            {
                "id": s.id,
                "status": s.status,
                "total_hours": round(float(s.total_hours or 0), 4),
                "item_count": len(s.items or []),
                "verify_at": s.verify_at.isoformat() if s.verify_at else None,
                "verify_user": s.verify_user,
            }
            for s in sheets
        ],
    }


def route_candidates(db: Session, task_id: int, *, q: str = "", limit: int = 200) -> dict:
    """可加入任务的路线清单 —— **只列已发布的基线**。

    未发布的草稿不出现，理由和 add_routes 的校验一致：选得到的就能用，
    选了才报错是把规则藏在报错里。同时单独给出「还有多少个组合只有草稿」，
    让用户知道不是没数据，而是还没发布，并知道该去哪发布。

    已经在本任务里的也返回，用 `in_task` 标出来 —— 列表里消失比标灰更让人困惑。
    """
    stmt = select(ProcessRoute).where(
        ProcessRoute.kind == KIND_BASELINE, ProcessRoute.status == "released"
    )
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(ProcessRoute.product.like(like), ProcessRoute.part.like(like))
        )
    routes = db.scalars(stmt.order_by(ProcessRoute.product, ProcessRoute.part)).all()

    # 同一组合理论上只有一条 released（有唯一索引兜底），保险起见按版本取最高
    best: dict[tuple[str, str], ProcessRoute] = {}
    for r in routes:
        key = (r.product, r.part)
        kept = best.get(key)
        if kept is None or (r.version or 0) > (kept.version or 0):
            best[key] = r

    chosen = list(best.values())[:limit]
    totals = _route_totals(db, [r.id for r in chosen])
    ref_counts = {
        int(rid): int(n)
        for rid, n in db.execute(
            select(TaskRoute.route_id, func.count())
            .where(TaskRoute.route_id.in_([r.id for r in chosen]), TaskRoute.status == "active")
            .group_by(TaskRoute.route_id)
        ).all()
    }
    in_task = {
        int(rid)
        for (rid,) in db.execute(
            select(TaskRoute.route_id).where(
                TaskRoute.task_id == task_id, TaskRoute.status == "active"
            )
        ).all()
    }
    items = []
    for r in chosen:
        cnt, hours = totals.get(r.id, (0, 0.0))
        items.append(
            {
                "route_id": r.id,
                "product": r.product,
                "part": r.part,
                "status": r.status,
                "version": r.version,
                "step_count": cnt,
                "unit_hours": hours,
                "ref_count": ref_counts.get(r.id, 0),
                "in_task": r.id in in_task,
                "published_at": r.published_at.isoformat() if r.published_at else None,
                "published_by": r.published_by or "",
            }
        )

    # 只有草稿、没有已发布版的组合数：用于提示「N 个组合未发布，不能引用」
    def _pairs(status: str) -> set[tuple[str, str]]:
        return {
            (row[0], row[1])
            for row in db.execute(
                select(ProcessRoute.product, ProcessRoute.part)
                .where(ProcessRoute.kind == KIND_BASELINE, ProcessRoute.status == status)
                .distinct()
            ).all()
        }

    draft_only = _pairs("draft") - _pairs("released")
    return {
        "total": len(items),
        "items": items,
        "draft_only_count": len(draft_only),
        "hint": (
            f"另有 {len(draft_only)} 个「产品×件名」只有草稿、尚未发布，不能引用；"
            "请先在「工艺及工时编排」中发布"
            if draft_only
            else ""
        ),
    }


def _as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1
