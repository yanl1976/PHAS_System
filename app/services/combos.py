"""工时组合（产品 × 件名）的维护。

「996 条总组合」不是一张独立的表，而是两处的并集：

    历史工时账 standard_hour 里的 product×part（有实测数据的）
    + 已建路线 process_route 里的 product×part（有数据但认定值已清的）

所以维护入口要同时看得见两类：有历史工时但还没建路线的（待办池），
和已经建了路线的（改口径时两边要一起看）。名字/停用在这里改，
建线与定额在「工艺及工时编排」里做——两边共用同一组键，改了这边那边立刻跟着变。

注意 StandardHour 是「历史实测统计」，手改属于纠正台账错误，不是重算标准：
改完值只影响校验比对与建线建议，不会反向修改已识别单据的工时。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import ProcessRoute, ProcessRouteStep, ProductNode, StandardHour, WorkCombo


def _product_id(db: Session, name: str) -> int | None:
    """产品名 → 主数据 id。认不出就 None（进待认领，不猜）。

    改组合名、新建组合之后都要重算一次：只改文本不改外键，
    P1 建立的「按产品统管」会在改名那一刻断掉，而且断得无声无息。
    """
    from .tasks import resolve_product

    found = resolve_product(db, name)
    return found.id if found else None


def _key(product: str | None, part: str | None) -> tuple[str, str]:
    return ((product or "").strip(), (part or "").strip())


def _route_state(rows: list[ProcessRoute]) -> tuple[str, ProcessRoute | None, int]:
    """组合的路线状态。已发布优先，其次草稿，再归档，都没有就是未建。"""
    released = [r for r in rows if r.status == "released"]
    drafts = [r for r in rows if r.status == "draft"]
    archived = [r for r in rows if r.status == "archived"]
    if released:
        return "released", released[0], len(released[0].steps)
    if drafts:
        return "draft", drafts[0], len(drafts[0].steps)
    if archived:
        return "archived", archived[0], len(archived[0].steps)
    return "none", None, 0


def list_combos(
    db: Session,
    *,
    q: str = "",
    state: str = "",
    only_issue: bool = False,
    include_disabled: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    """组合台账。主表是 `work_combo`——组合是身份，路线只是它的某一版。

    以前这里是「历史工时 ∪ 工艺路线」每次现算的并集，所以既答不出「谁确认过」，
    也拦不住未发布的工艺被引用。现在身份落表，状态只看 `work_combo.status`：
    草稿就是草稿，工艺科点了发布才会变成可引用的工艺。
    """
    # 台账从没铺过（老库刚升级/新部署）。这里**不再顺手 sync**：
    # 那是一次全量扫描 + 可能写库，放在 GET 里会让「翻回第 1 页」变慢，
    # 多进程下更变成 N 个进程抢同一把 SQLite 写锁（组合台账并发崩掉的主因）。
    # 现在只报告「需要同步」，由 /api/combos/sync 或识别落库后触发。
    empty_registry = not db.scalar(select(func.count()).select_from(WorkCombo))

    # 历史实测：只做对照口径（实测 vs 定额），不参与组合状态判断
    hist: dict[tuple[str, str], dict] = {}
    for h in db.scalars(select(StandardHour)).all():
        key = _key(h.product, h.part)
        bucket = hist.setdefault(key, {"processes": {}, "samples": 0, "active": False})
        if h.active:
            bucket["processes"][(h.process or "").strip()] = float(h.unit_hours or 0.0)
        bucket["samples"] = max(bucket["samples"], int(h.samples or 0))

    # 每个组合锚定一版路线：已发布优先，没有就用版本号最大的草稿。
    # 升序遍历后写覆盖，遇到 released 就锁住，不被更高版草稿顶掉。
    anchor: dict[tuple[str, str], ProcessRoute] = {}
    for r in db.scalars(
        select(ProcessRoute)
        .where(ProcessRoute.kind == "route")
        .order_by(ProcessRoute.version.asc())
        .execution_options(populate_existing=True)
    ).all():
        key = _key(r.product, r.part)
        cur = anchor.get(key)
        if cur is None or cur.status != "released":
            anchor[key] = r

    # 定额合计（路线工序工时之和）：与「历史实测」并列显示，
    # 工艺员要一眼看出「定额 12h、历史实测 8h」这种偏差，而不是自己心算。
    #
    # 工序数在同一趟 GROUP BY 里一起数出来。以前这里是 len(r.steps)，
    # 对每个组合的锚定路线各触发一次懒加载 —— 541 个组合就是 541 条 SQL，
    # 组合越多越慢（2000 条就是 2000 次），而且它出现在 GET 里，并发时会
    # 把 GIL 占满。一条聚合查询即可，语义完全不变。
    route_hours: dict[int, float] = {}
    route_steps: dict[int, int] = {}
    if anchor:
        for rid, usum, nstep in db.execute(
            select(
                ProcessRouteStep.route_id,
                func.coalesce(func.sum(ProcessRouteStep.unit_hours), 0.0),
                func.count(),
            )
            .where(ProcessRouteStep.route_id.in_([r.id for r in anchor.values()]))
            .group_by(ProcessRouteStep.route_id)
        ).all():
            route_hours[rid] = round(float(usum or 0.0), 4)
            route_steps[rid] = int(nstep or 0)

    combos = db.scalars(select(WorkCombo).order_by(WorkCombo.product, WorkCombo.part)).all()
    # 层级读结构表：结构与工艺已经分成两张表，台账这一列要显示的是「装在哪」，
    # 那是结构的事，不是工时组合的事。
    nodes = {
        (n.product or "", n.part or ""): n for n in db.scalars(select(ProductNode)).all()
    }
    node_by_id = {n.id: n for n in nodes.values()}

    items: list[dict] = []
    for c in combos:
        key = _key(c.product, c.part)
        h = hist.get(key) or {"processes": {}, "samples": 0, "active": False}
        r = anchor.get(key)
        unit_sum = round(sum(h["processes"].values()), 4)
        node = nodes.get(key)
        parent = node_by_id.get(node.parent_id) if (node and node.parent_id) else None
        items.append(
            {
                "id": c.id,
                "product": c.product,
                "part": c.part,
                "product_id": c.product_id,
                "level": node.level if node else None,
                "parent_id": node.parent_id if node else None,
                "parent_part": (parent.part or "") if parent else "",
                "level_src": node.source if node else "",
                "qty_per_unit": float(node.qty_per_unit or 0.0) if node else 0.0,
                "seq": node.seq if node else None,
                "status": c.status,
                # state 是旧字段名，前端与脚本还在用；值现在就是组合状态本身
                "state": c.status,
                "route_id": c.route_id or (r.id if r else None),
                "version": r.version if r else None,
                "step_count": route_steps.get(r.id, 0) if r else 0,
                "route_hours": route_hours.get(r.id, 0.0) if r else 0.0,
                "process_count": len(h["processes"]),
                "unit_sum": unit_sum,
                "samples": h["samples"],
                "value": round(unit_sum * max(1, h["samples"]), 2),
                # active = 这条组合还在不在用。判据是组合状态本身，不是历史工时行：
                # 停用会把组合置 archived，状态列必须同一刻跟着变，否则就是
                # 「点了停用、状态没动静」。
                "active": c.status != "archived",
                "history_active": bool(h["active"]),
                "issue": (not c.product) or (not c.part),
                "has_history": bool(h["processes"]),
                "has_route": r is not None,
                "route_status": r.status if r else "",
                "demoted_from": c.demoted_from,
                "note": c.note,
                "published_by": c.published_by,
                "published_at": c.published_at.isoformat(timespec="minutes")
                if c.published_at
                else None,
            }
        )

    if q.strip():
        kw = q.strip()
        items = [x for x in items if kw in x["product"] or kw in x["part"]]
    if state == "none":
        items = [x for x in items if not x["has_route"]]
    elif state:
        items = [x for x in items if x["status"] == state]
    if only_issue:
        items = [x for x in items if x["issue"]]
    if not include_disabled:
        items = [x for x in items if x["active"]]

    items.sort(key=lambda x: (-x["value"], x["product"], x["part"]))
    total = len(items)
    page = items[offset : offset + max(1, limit)]
    return {
        "items": page,
        "total": total,
        "filtered": total,
        # 台账还是空的：让前端提示「点同步」而不是给一个看起来正常的空表
        "needs_sync": empty_registry,
        "summary": {
            "total": total,
            "released": sum(1 for x in items if x["status"] == "released"),
            "draft": sum(1 for x in items if x["status"] == "draft"),
            "archived": sum(1 for x in items if x["status"] == "archived"),
            "unbuilt": sum(1 for x in items if not x["has_route"]),
            # 迁移降级遗留：曾经被标过已发布、但现在没有工艺科签字的
            "demoted": sum(1 for x in items if x["demoted_from"]),
            "ready": sum(
                1 for x in items if x["status"] == "draft" and x["has_route"] and x["step_count"]
            ),
            "issue": sum(1 for x in items if x["issue"]),
            "no_history": sum(1 for x in items if not x["has_history"]),
        },
    }


def detail(db: Session, product: str, part: str) -> dict:
    key = _key(product, part)
    rows = [
        h
        for h in db.scalars(select(StandardHour)).all()
        if _key(h.product, h.part) == key
    ]
    routes = [
        r
        for r in db.scalars(
            select(ProcessRoute)
            .where(ProcessRoute.kind == "route")
            .execution_options(populate_existing=True)
        ).all()
        if _key(r.product, r.part) == key
    ]
    state_name, anchor, _n = _route_state(routes)

    # 明细弹窗要能直接回答「这条能不能发布、为什么」，所以把校验一起带出来，
    # 不必让用户在弹窗和发布按钮之间来回猜。
    combo = db.scalar(
        select(WorkCombo).where(WorkCombo.product == key[0], WorkCombo.part == key[1])
    )
    issues: list[dict] = []
    if combo is not None:
        from .combo_registry import _latest_route, validate_combo

        issues = validate_combo(db, combo, _latest_route(db, combo))

    node = db.scalar(
        select(ProductNode).where(ProductNode.product == key[0], ProductNode.part == key[1])
    )
    return {
        "product": key[0],
        "part": key[1],
        "state": state_name,
        "combo": {
            "id": combo.id if combo else None,
            "status": combo.status if combo else "",
            "level": node.level if node else None,
            "parent_id": node.parent_id if node else None,
            "level_src": node.source if node else "",
            "qty_per_unit": float(node.qty_per_unit or 1) if node else None,
            "published_by": combo.published_by if combo else "",
            "published_at": combo.published_at.isoformat(timespec="minutes")
            if combo and combo.published_at
            else None,
            "demoted_from": combo.demoted_from if combo else "",
            "note": combo.note if combo else "",
        },
        "issues": issues,
        "can_publish": combo is not None
        and combo.status == "draft"
        and not any(x["level"] == "block" for x in issues),
        "route_id": anchor.id if anchor else None,
        "versions": [
            {"id": r.id, "version": r.version, "status": r.status} for r in routes
        ],
        "steps": [
            {
                "id": h.id,
                "process": h.process,
                "unit_hours": float(h.unit_hours or 0.0),
                "samples": int(h.samples or 0),
                "agreement": float(h.agreement or 0.0),
                "active": bool(h.active),
            }
            for h in sorted(rows, key=lambda x: (x.process or ""))
        ],
        "unit_sum": round(sum(float(h.unit_hours or 0.0) for h in rows if h.active), 4),
    }


def upsert_step(
    db: Session,
    product: str,
    part: str,
    process: str,
    unit_hours: float,
    samples: int | None = None,
) -> StandardHour:
    key = _key(product, part)
    name = (process or "").strip()
    if not key[1]:
        raise ValueError("件名不能为空")
    if not name:
        raise ValueError("工序不能为空")
    if unit_hours < 0:
        raise ValueError("单件工时不能为负")

    row = db.scalar(
        select(StandardHour).where(
            StandardHour.product == key[0],
            StandardHour.part == key[1],
            StandardHour.process == name,
        )
    )
    if row is None:
        # 同一组合下可能有多条同工序的脏数据（历史上按原始写法存的），
        # 先按去空格后的名字找一遍，找不到才新建
        for h in db.scalars(select(StandardHour)).all():
            if _key(h.product, h.part) == key and (h.process or "").strip() == name:
                row = h
                break
    if row is None:
        row = StandardHour(
            product=key[0],
            product_id=_product_id(db, key[0]),
            part=key[1],
            process=name,
            unit_hours=float(unit_hours),
            samples=int(samples or 0),
            agreement=0.0,
            active=True,
        )
    else:
        row.product_id = row.product_id or _product_id(db, key[0])
        row.unit_hours = float(unit_hours)
        if samples is not None:
            row.samples = int(samples)
        row.active = True
    db.add(row)
    db.flush()
    return row


def delete_step(db: Session, step_id: int) -> None:
    row = db.get(StandardHour, step_id)
    if not row:
        raise ValueError("工序行不存在")
    db.delete(row)
    db.flush()


def rename(
    db: Session,
    product: str,
    part: str,
    new_product: str | None = None,
    new_part: str | None = None,
    sync_routes: bool = True,
) -> dict:
    """改组合的产品名/件名。

    历史和路线都必须一起改：只改一边，同一组合在两处就对不上了，
    台账里会凭空多出一条「待建」。
    """
    old = _key(product, part)
    new = (
        (new_product or "").strip() if new_product is not None else old[0],
        (new_part or "").strip() if new_part is not None else old[1],
    )
    if not new[1]:
        raise ValueError("件名不能为空")
    if new == old:
        return {"changed": 0, "routes": 0}

    pid = _product_id(db, new[0])
    touched = 0
    for h in db.scalars(select(StandardHour)).all():
        if _key(h.product, h.part) == old:
            h.product, h.part = new
            h.product_id = pid
            db.add(h)
            touched += 1

    routes = 0
    if sync_routes:
        for r in db.scalars(select(ProcessRoute).where(ProcessRoute.kind == "route")).all():
            if _key(r.product, r.part) == old:
                r.product, r.part = new
                r.product_id = pid
                db.add(r)
                routes += 1

    # 组合身份跟着改。不改的话台账会留下旧名，而工时和路线都已经是新名，
    # 等于凭空多出一条「待建」加一条「遗留空壳」。
    for c in db.scalars(
        select(WorkCombo).where(WorkCombo.product == old[0], WorkCombo.part == old[1])
    ).all():
        c.product, c.part = new
        c.product_id = pid
        db.add(c)

    # 结构也要跟着改名。结构表以 (产品, 件名) 关联工艺，只改一边，
    # 这件在结构里就成了「有位置但没工艺」的孤儿，件号和用量都会算空。
    node_moved = 0
    for n in db.scalars(
        select(ProductNode).where(ProductNode.product == old[0], ProductNode.part == old[1])
    ).all():
        n.product, n.part = new
        db.add(n)
        node_moved += 1

    db.flush()
    return {"changed": touched, "routes": routes, "nodes": node_moved}


def set_active(db: Session, product: str, part: str, active: bool) -> dict:
    """停用 / 启用一条组合。

    停用落到 `WorkCombo.status='archived'`，不只是把历史工时行标成不用：
    台账的状态列、识别索引、任务挂线、工艺卡全都看组合状态，只翻工时行
    等于「点了没反应」—— 状态不变，工艺照样能被引用。
    启用则按路线状态回到已发布或草稿（见 `restore_combo`）。
    """
    from . import combo_registry as registry

    key = _key(product, part)
    n = 0
    for h in db.scalars(select(StandardHour).where(StandardHour.product == key[0])).all():
        if _key(h.product, h.part) == key:
            h.active = active
            db.add(h)
            n += 1

    combo = db.scalar(
        select(WorkCombo).where(WorkCombo.product == key[0], WorkCombo.part == key[1])
    )
    status = ""
    if combo is not None:
        res = (
            registry.restore_combo(db, combo, by="")
            if active
            else registry.archive_combo(db, combo, by="", note="停用")
        )
        status = res["status"]
        db.flush()
    return {"changed": n, "active": status != "archived", "status": status}



def create_combo(
    db: Session, product: str, part: str, steps: list[dict] | None = None
) -> dict:
    key = _key(product, part)
    if not key[1]:
        raise ValueError("件名不能为空")
    exists = any(
        _key(h.product, h.part) == key for h in db.scalars(select(StandardHour)).all()
    ) or any(
        _key(r.product, r.part) == key
        for r in db.scalars(select(ProcessRoute).where(ProcessRoute.kind == "route")).all()
    )
    if exists:
        raise ValueError("这个产品×件名已经存在")
    n = 0
    for s in steps or []:
        name = (s.get("process") or "").strip()
        if not name:
            continue
        upsert_step(db, key[0], key[1], name, float(s.get("unit_hours") or 0.0))
        n += 1
    # 身份表同步建一行，否则「新建了组合但台账里没有」这种鬼故事会立刻出现。
    # 新建的一律是草稿：没人能靠建一条组合就把工艺发布出去。
    if db.scalar(
        select(WorkCombo).where(WorkCombo.product == key[0], WorkCombo.part == key[1])
    ) is None:
        db.add(
            WorkCombo(
                product=key[0],
                part=key[1],
                product_id=_product_id(db, key[0]),
                status="draft",
                note="新建组合",
            )
        )
        db.flush()
    # 结构也要有这一件。只建组合不建结构节点，这件在结构树上不存在，
    # 工艺卡沿结构展开时看不到它，自动重铺还会把它当「没登记的件」漏掉。
    if key[0]:
        from .combo_registry import ensure_node

        ensure_node(db, key[0], key[1], source="manual")
    return {"product": key[0], "part": key[1], "steps": n}
