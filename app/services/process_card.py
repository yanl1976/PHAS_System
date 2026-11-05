"""工艺卡：把「已发布组合 + 部件层级」印成车间能照着做的卡。

为什么单独一个模块，而不是塞进 `combo_registry`：组合关心「这工艺能不能用」，
工艺卡关心「这一版怎么装、按什么数装」。前者是闸，后者是成品。混在一起后
每次调发布校验都会把卡片结构一起加载，而卡片的字段清单是给纸用的，跟着
工艺台账一起改会让两边都难维护。

三条口径定了就不再动：

1. **只印已发布**。草稿组合在车间眼里等于不存在，印出来就是给人一个错的标准。
   页面上会明说「另有 N 条未发布」，让人知道缺的是哪一边。
2. **编号跟着层级走**。根（整机装配）是 `01`，挂在它下面的部件是 `01-01`、
   `01-02`，部件下的子件是 `01-01-01`。编号只按已发布组合连续编，
   中间不会出现「01-03 空缺」——装订成本子时页码是连贯的。
3. **三个工时分开口径**：本层（这张卡自己的工序）、含子件（本层 + 所有下级）、
   产品单台（整个产品的合计）。报工只认「本层 × 合格件数」，
   另外两个数是给排产和报价看的，不能混成一行。
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ..models import (
    ProductNode,
    ProcessRoute,
    ProcessRouteStep,
    Sheet,
    TaskRoute,
    WorkCombo,
    WorkItem,
    WorkTask,
)
from .routes import step_out

# 层级的中文名。0 是产品的总装件，1 是直接装在总装上的部件/零件，
# 2 及更深是部件里的子件。
LEVEL_NAMES = {0: "总装", 1: "部件", 2: "子件", 3: "子件", 4: "子件", 5: "子件"}
MAX_DEPTH = 6


def level_name(level: int | None) -> str:
    # 不能用 `level or 1`：0 是合法层级（总装根），`0 or 1` 会把它变成「部件」，
    # 于是整机装配卡在卡头上写着「部件」——一眼就错，而且很难发现
    lv = 1 if level is None else int(level)
    return LEVEL_NAMES.get(lv, "子件")


def _key(product: str | None, part: str | None) -> tuple[str, str]:
    return ((product or "").strip(), (part or "").strip())


# ---------------------------------------------------------------- 编号


def _numbering(rows: list[WorkCombo]) -> dict[int, dict]:
    """给一批（同一产品的）已发布组合编号。

    返回 `{combo_id: {"code": "01-02", "order": (1, 2), "parent_id": ...}}`。
    只按**已发布**的父子链编号：如果某个部件的上级还是草稿，那这个部件在
    卡片本子里就是顶层，编号从 `01` 重新开始 —— 否则本子里会出现一张
    找不到上级的卡，装订的人第一反应是漏印了。

    成环防御不是多余的：`validate_level` 挡在写入口，但直接改库、导数据
    都可能绕过它。这里撞到环就停止下潜并留一个 `cycle=True`，宁可编号怪异
    也不要卡死接口。
    """
    ids = {c.id for c in rows}
    kids: dict[int | None, list[WorkCombo]] = defaultdict(list)
    for c in rows:
        parent = c.parent_id if c.parent_id in ids else None
        if parent == c.id:  # 自己挂自己
            parent = None
        kids[parent].append(c)

    out: dict[int, dict] = {}

    def walk(parent: int | None, prefix: str, order: tuple[int, ...], depth: int) -> None:
        siblings = sorted(
            kids.get(parent, []),
            key=lambda c: (int(c.seq or 100), (c.part or "")),
        )
        for i, c in enumerate(siblings, start=1):
            if c.id in out:  # 已经被别的支路认领 → 有环，跳过
                continue
            seg = f"{i:02d}"
            code = f"{prefix}-{seg}" if prefix else seg
            out[c.id] = {
                "code": code,
                "order": order + (i,),
                "parent_id": parent,
                "cycle": False,
            }
            if depth < MAX_DEPTH:
                walk(c.id, code, order + (i,), depth + 1)

    walk(None, "", (), 0)

    # 兜底：环里的节点不会被第一轮认领，全部提到根上，保证每张卡都有编号
    loose = [c for c in rows if c.id not in out]
    for i, c in enumerate(sorted(loose, key=lambda x: (x.seq or 100, x.part or "")), start=1):
        out[c.id] = {"code": f"9{i:02d}", "order": (900 + i,), "parent_id": None, "cycle": True}
    return out


# ---------------------------------------------------------------- 取数


def _released_routes(db: Session) -> dict[tuple[str, str], ProcessRoute]:
    """(产品, 件名) → 当前生效的已发布路线。

    一口气取全量再在内存里配对：889 个组合逐个 `active_route()` 就是 889 次
    查询，打印一整套卡会让人等到以为页面卡死。
    """
    routes = db.scalars(
        select(ProcessRoute)
        .where(ProcessRoute.kind == "route", ProcessRoute.status == "released")
        .execution_options(populate_existing=True)
    ).all()
    return {_key(r.product, r.part): r for r in routes}


def _steps_map(db: Session, route_ids: list[int]) -> dict[int, list[ProcessRouteStep]]:
    if not route_ids:
        return {}
    rows = db.scalars(
        select(ProcessRouteStep)
        .where(ProcessRouteStep.route_id.in_(route_ids))
        .order_by(ProcessRouteStep.route_id, ProcessRouteStep.seq)
    ).all()
    bucket: dict[int, list[ProcessRouteStep]] = defaultdict(list)
    for s in rows:
        bucket[s.route_id].append(s)
    return bucket


def _descendants(rows: list[WorkCombo]) -> dict[int, list[WorkCombo]]:
    ids = {c.id for c in rows}
    kids: dict[int | None, list[WorkCombo]] = defaultdict(list)
    for c in rows:
        kids[c.parent_id if c.parent_id in ids else None].append(c)
    return kids


def _hours(rows: list[WorkCombo], kids, own_of) -> dict[int, float]:
    """含子件工时：本层 + 所有下级（下级按「每台用量」加权）。记忆化 + 环防御。

    为什么要加权：装一台部件要用 4 个子件，那这个部件的含子件工时就是
    `本层 + 4 × 子件单件工时`。不加权的话，一台机器上重复装的零件在总工时里
    只算一次，报出去的定额天生偏低。库里绝大多数 `qty_per_unit` 是 1，
    但这一行代码要能对得起那少数几条不为 1 的。
    """
    memo: dict[int, float] = {}

    def calc(c: WorkCombo, trail: set[int]) -> float:
        if c.id in memo:
            return memo[c.id]
        if c.id in trail:  # 环，就地截断
            return own_of.get(c.id, 0.0)
        trail = trail | {c.id}
        total = own_of.get(c.id, 0.0)
        for ch in kids.get(c.id, []):
            total += float(ch.qty_per_unit or 1) * calc(ch, trail)
        memo[c.id] = round(total, 4)
        return memo[c.id]

    for c in rows:
        calc(c, set())
    return memo


# ---------------------------------------------------------------- 主入口


def _sheet_stats(db: Session) -> dict[tuple[str, str], dict]:
    """(产品, 件名) → 这张卡已经录进来的**加工路线单**统计。

    工艺卡和加工路线单是同一张纸的两个阶段：编排好、还没干活，印出来的是
    工艺卡（工序与定额预印，其余留白）；干完活录回来，同一个「产品×件名」
    就变成几张加工路线单。所以卡片上必须能看出「这件活已经报了 N 张单」——
    否则工艺员拿着卡不知道这活到底还没干、还是早就干完了。

    拒收的单据不计：它本身就是「这张没算数」。
    """
    rows = db.execute(
        select(WorkItem, Sheet)
        .join(Sheet, WorkItem.sheet_id == Sheet.id)
        .where(Sheet.status != "rejected")
        .order_by(Sheet.id)
    ).all()
    out: dict[tuple[str, str], dict] = {}
    for item, sheet in rows:
        key = _key(item.product_name, item.part_name)
        if not key[1]:
            continue
        b = out.setdefault(
            key,
            {"count": 0, "hours": 0.0, "last_no": "", "material": "",
             "sheet_ids": [], "scan_forms": []},
        )
        if sheet.id not in b["sheet_ids"]:
            b["sheet_ids"].append(sheet.id)
            b["count"] = len(b["sheet_ids"]) + len(b["scan_forms"])
        b["hours"] = round(b["hours"] + float(item.total_hours or 0), 4)
        # 后出现的覆盖前面的：查询已按 sheet.id 升序，留下的是最近一张
        if sheet.id >= (b.get("_last_id") or 0):
            b["_last_id"] = sheet.id
            b["last_no"] = sheet.id
            if (item.material or "").strip():
                b["material"] = item.material.strip()

    # 扫码报工没有 sheet，只有预印单号。不把它算进「已报 N」，
    # 工艺卡上就会一直写着「还没有」—— 活明明已经干完并发了工资。
    scan_rows = db.execute(
        select(WorkItem)
        .where(WorkItem.sheet_id.is_(None), WorkItem.print_form_no != "")
        .order_by(WorkItem.id)
    ).scalars()
    for item in scan_rows:
        key = _key(item.product_name, item.part_name)
        if not key[1]:
            continue
        b = out.setdefault(
            key,
            {"count": 0, "hours": 0.0, "last_no": "", "material": "",
             "sheet_ids": [], "scan_forms": []},
        )
        if item.print_form_no not in b["scan_forms"]:
            b["scan_forms"].append(item.print_form_no)
            b["count"] = len(b["sheet_ids"]) + len(b["scan_forms"])
        b["hours"] = round(b["hours"] + float(item.total_hours or 0), 4)
        b["last_form_no"] = item.print_form_no
        if (item.material or "").strip():
            b["material"] = item.material.strip()
    for b in out.values():
        b.pop("_last_id", None)
    return out


def tasks(db: Session) -> list[dict]:
    """生产任务清单（给「按任务出卡」的下拉）。

    只列还有件名挂着的任务：已经全部撤线的单子点进去只会看到空页面。
    每行给「能印几张卡 / 有几件缺工艺」，选之前就知道打出来是厚是薄。
    """
    combos = {_key(c.product, c.part): c for c in db.scalars(select(WorkCombo)).all()}
    trs = db.scalars(
        select(TaskRoute).where(TaskRoute.status == "active").order_by(TaskRoute.task_id)
    ).all()
    by_task: dict[int, list[TaskRoute]] = defaultdict(list)
    for tr in trs:
        by_task[tr.task_id].append(tr)

    items: list[dict] = []
    for t in db.scalars(select(WorkTask).order_by(WorkTask.id.desc())).all():
        rows = by_task.get(t.id) or []
        if not rows:
            continue
        released = sum(
            1
            for tr in rows
            if (c := combos.get(_key(tr.product, tr.part))) is not None and c.status == "released"
        )
        items.append(
            {
                "id": t.id,
                "task_no": t.task_no,
                "title": t.title,
                "product": t.product,
                "qty": float(t.qty or 0),
                "status": t.status,
                "period_id": t.period_id,
                "routes": len(rows),
                "released": released,
                "draft": len(rows) - released,
                "sheet_count": t.sheet_count or 0,
                "actual_hours": round(float(t.actual_hours or 0), 2),
            }
        )
    # 能整套印的任务排前面：工艺员来打印，十次有九次是打刚下的单
    items.sort(key=lambda x: (-x["released"], -x["id"]))
    return items


def _task_brief(db: Session, task_id: int) -> dict | None:
    t = db.get(WorkTask, task_id)
    if t is None:
        return None
    return {
        "id": t.id,
        "task_no": t.task_no,
        "title": t.title,
        "qty": float(t.qty or 0),
        "status": t.status,
        "batch_no": "",
        # 计调员留空：这张纸上签名的人得是真人。库里只有建单账号（admin/operator，
        # 都是系统账号），印上去等于替人签了个名，车间拿着也找不到是谁
        "dispatcher": "",
        "order_date": t.order_date or "",
        "due_date": t.due_date or "",
    }


def products(db: Session) -> list[dict]:
    """产品清单：每个产品有多少条已发布、多少条还没发、卡片工时合计。"""
    combos = db.scalars(select(WorkCombo)).all()
    routes = _released_routes(db)
    route_ids = list({r.id for r in routes.values()})
    steps = _steps_map(db, route_ids)
    own = {
        rid: round(sum(float(s.unit_hours or 0) for s in ss), 4)
        for rid, ss in steps.items()
    }

    buckets: dict[str, dict] = {}
    for c in combos:
        b = buckets.setdefault(
            c.product,
            {
                "product": c.product,
                "product_id": c.product_id,
                "released": 0,
                "draft": 0,
                "archived": 0,
                "released_with_route": 0,
                "hours": 0.0,
                "step_count": 0,
            },
        )
        if c.status == "released":
            b["released"] += 1
            r = routes.get(_key(c.product, c.part))
            if r is not None:
                b["released_with_route"] += 1
                b["hours"] += own.get(r.id, 0.0)
                b["step_count"] += len(steps.get(r.id, []))
        elif c.status == "draft":
            b["draft"] += 1
        else:
            # 停用（归档）单独记：它不出工艺卡，但不是「还没发布」——
            # 混进 draft 会让人以为把草稿发一发就有卡了
            b["archived"] += 1

    # 「产品名为空」那一桶永远排最后：它是 89 条还没认领产品的钳工序，
    # 首屏默认落在它上面的话，工艺员打开看到的就是一堆无主件名。
    items = sorted(
        buckets.values(),
        key=lambda x: (
            not x["product"],
            -x["hours"],
            -x["released"],
            -x["draft"],
            x["product"],
        ),
    )
    for it in items:
        it["hours"] = round(it["hours"], 2)
    return items


def build_cards(
    db: Session,
    *,
    product: str = "",
    part: str = "",
    task_id: int | None = None,
    include_children: bool = True,
    limit: int = 400,
    printed_by: str = "",
) -> dict:
    """生成工艺卡。

    两种入口，同一批卡：

    * **按产品**（`product`）—— 工艺科视角：这个产品的整机装配、各部件各一张，
      没挂生产任务，按**单台**口径印（定额是不是对的，跟哪一单无关）。
    * **按生产任务**（`task_id`）—— 车间视角：这一单要做哪几张卡，
      表头印投产台数、产品批号，定额合计按**本单件数**算。

    `product` 为空且没指定任务时自动选工时最高的产品（页面首屏不至于空白）。
    """
    prods = products(db)
    product = (product or "").strip()
    task = _task_brief(db, task_id) if task_id else None

    # 任务口径：这个任务挂了哪些「产品×件名」，每个件名本单要做几件。
    # task_route 上没有数量（=0）时退回「投产台数 × 每台用量」。
    tr_qty: dict[tuple[str, str], float] = {}
    if task_id:
        for tr in db.scalars(
            select(TaskRoute).where(
                TaskRoute.task_id == task_id, TaskRoute.status == "active"
            )
        ).all():
            tr_qty[_key(tr.product, tr.part)] = float(tr.qty or 0)
        if not product and task:
            product = (task.get("product") or "").strip()

    if not product and prods and not task_id:
        product = prods[0]["product"]

    combos = db.scalars(select(WorkCombo).order_by(WorkCombo.product, WorkCombo.part)).all()

    # ---- 层级以 product_node 为准 -------------------------------------------------
    # 结构与工艺拆成两张表之后，`work_combo` 上那几个层级列已经不再维护。
    # 这里把结构读出来，**在内存里**盖到组合对象上（`set_committed_value`，
    # 不是普通赋值），这样下面编号、父子、用量加权的逻辑一行都不用改，
    # 但这些值也不会被当成「改过的字段」写回 work_combo。
    node_by_id = {n.id: n for n in db.scalars(select(ProductNode)).all()}
    combo_by_key = {(c.product or "", c.part or ""): c for c in combos}
    for n in node_by_id.values():
        c = combo_by_key.get((n.product or "", n.part or ""))
        if c is None:
            continue
        parent = node_by_id.get(n.parent_id) if n.parent_id else None
        pc = combo_by_key.get((parent.product or "", parent.part or "")) if parent else None
        set_committed_value(c, "parent_id", pc.id if pc else None)
        set_committed_value(c, "level", int(n.level or 0))
        set_committed_value(c, "qty_per_unit", float(n.qty_per_unit or 1))
        set_committed_value(c, "seq", int(n.seq if n.seq is not None else 100))
        set_committed_value(c, "level_src", n.source or "auto")
    if task_id:
        combos = [c for c in combos if _key(c.product, c.part) in tr_qty]
    elif product:
        combos = [c for c in combos if (c.product or "") == product]

    released = [c for c in combos if c.status == "released"]
    routes = _released_routes(db)
    by_id = {c.id: c for c in released}
    stats = _sheet_stats(db)

    # 有身份但没生效工艺：理论上 release 时就挡住了，这里仍然单独留痕，
    # 免得打印出一张空白卡让人以为工艺就是空的。
    missing = [
        {
            "product": c.product,
            "part": c.part,
            "id": c.id,
            "reason": "已发布但取不到生效路线",
        }
        for c in released
        if _key(c.product, c.part) not in routes
    ]
    if task_id:
        # 任务上挂了、组合却没发布：这单该有的卡印不出来，必须当场说清楚，
        # 而不是让人数一数发现少了一张
        for key in tr_qty:
            c = next((x for x in combos if _key(x.product, x.part) == key), None)
            if c is None:
                missing.append(
                    {"product": key[0], "part": key[1], "id": None, "reason": "没有登记工时组合"}
                )
            elif c.status != "released":
                missing.append(
                    {
                        "product": key[0],
                        "part": key[1],
                        "id": c.id,
                        "reason": "工艺未发布（状态："
                        + ("停用" if c.status == "archived" else "草稿")
                        + "）",
                    }
                )
    printable = [c for c in released if _key(c.product, c.part) in routes]
    printable_keys = {_key(c.product, c.part) for c in printable}

    # 结构索引：只用来补「结构里有、工艺还没建」的占位件。
    # 为什么只补「已出卡部件的直接下级」，而不是把整棵结构铺出来：生产库
    # 889 个结构节点里有 348 个没编过工艺，全铺出来左栏会突然多出一大片
    # 「未建」，看着像工艺大面积缺失，反而没人看得懂。真正要回答的问题是
    # 「我刚在结构里加的那一件，为什么工艺卡上找不到」。
    struct_kids: dict[tuple[str, str], list[ProductNode]] = defaultdict(list)
    for n in node_by_id.values():
        if n.parent_id and n.parent_id in node_by_id:
            p = node_by_id[n.parent_id]
            struct_kids[(p.product or "", p.part or "")].append(n)
    for row_list in struct_kids.values():
        row_list.sort(key=lambda x: (int(x.seq if x.seq is not None else 100), x.part or ""))

    numbering = _numbering(printable)
    kids = _descendants(printable)

    # 草稿 / 停用的路线：(产品, 件名) → 状态。占位件要靠它区分「根本没编」和
    # 「编了草稿没发布」—— 草稿阶段不登记 `work_combo` 身份，只看身份表的话
    # 这两种情况在工艺卡上是同一个「未建」，工艺员会去重编一个已有草稿的件。
    unrel: dict[tuple[str, str], str] = {}
    for r in db.scalars(select(ProcessRoute).where(ProcessRoute.kind == "route")).all():
        k2 = _key(r.product, r.part)
        if r.status == "draft":
            unrel[k2] = "draft"
        elif r.status == "archived" and k2 not in unrel:
            unrel[k2] = "archived"

    steps_map = _steps_map(db, [routes[_key(c.product, c.part)].id for c in printable])
    own_of: dict[int, float] = {}
    setup_of: dict[int, float] = {}
    step_count: dict[int, int] = {}
    for c in printable:
        r = routes[_key(c.product, c.part)]
        ss = steps_map.get(r.id, [])
        own_of[c.id] = round(sum(float(s.unit_hours or 0) for s in ss), 4)
        setup_of[c.id] = round(sum(float(s.setup_hours or 0) for s in ss), 4)
        step_count[c.id] = len(ss)
    subtotal = _hours(printable, kids, own_of)
    # 产品单台工时 = 各根节点「含子件」之和。不能用简单 Σ本层：那样每台用
    # 4 件的零件在整机工时里只算一次，整机定额天生偏低。
    product_hours = round(
        sum(subtotal.get(root.id, own_of.get(root.id, 0.0)) for root in kids.get(None, [])), 4
    )

    # ---- 选出这一批要打印的卡 ----
    picked: list[WorkCombo]
    if part.strip():
        target = next((c for c in printable if (c.part or "").strip() == part.strip()), None)
        if target is None:
            picked = []
        elif include_children:
            wanted = {target.id}

            def collect(node: WorkCombo) -> None:
                for ch in kids.get(node.id, []):
                    if ch.id in wanted:
                        continue
                    wanted.add(ch.id)
                    collect(ch)

            collect(target)
            picked = [c for c in printable if c.id in wanted]
        else:
            picked = [target]
    else:
        picked = list(printable)

    picked.sort(key=lambda c: numbering[c.id]["order"])
    picked = picked[: max(1, min(limit, 2000))]

    def placeholder_node(n: ProductNode) -> dict:
        """结构里有、但取不到已发布工艺的件。

        只出现在层级里（`has_route=False`），**不会变成卡片**：没有工艺就没有
        工序可印，硬塞一张空白卡只会让车间以为漏了工序。它的工时为 0，
        不参与定额合计，也不打乱已有卡片的编号 —— 编号只在 printable 里排。

        `status` 取真实状态：有组合身份就取身份的，没有就看有没有草稿/停用路线。
        「编了草稿还没发布」和「根本没编」是两件事，前者该催发布，后者该催编工艺。
        """
        key2 = _key(n.product, n.part)
        c = combo_by_key.get(key2)
        state = c.status if c is not None else unrel.get(key2, "none")
        return {
            "combo_id": c.id if c is not None else None,
            "code": "",
            "part": n.part,
            "level": int(n.level or 0),
            "level_name": level_name(n.level),
            "seq": int(n.seq if n.seq is not None else 100),
            "qty_per_unit": round(float(n.qty_per_unit or 1), 4),
            "step_count": 0,
            "own_hours": 0.0,
            "subtotal_hours": 0.0,
            "version": None,
            "sheet_count": 0,
            "status": state,
            "has_route": False,
            "children": [
                placeholder_node(ch)
                for ch in struct_kids.get(key2, [])
                if _key(ch.product, ch.part) not in printable_keys
            ],
        }

    def brief(node_dict: dict) -> dict:
        """下级件的精简卡片：卡片列表和层级树共用一个口径。"""
        return {
            "combo_id": node_dict.get("combo_id"),
            "code": node_dict.get("code") or "",
            "part": node_dict.get("part"),
            "level_name": node_dict.get("level_name"),
            "qty_per_unit": node_dict.get("qty_per_unit"),
            "step_count": node_dict.get("step_count", 0),
            "own_hours": node_dict.get("own_hours", 0.0),
            "subtotal_hours": node_dict.get("subtotal_hours", 0.0),
            "seq": node_dict.get("seq") or 100,
            "status": node_dict.get("status") or "none",
            "has_route": bool(node_dict.get("has_route")),
        }

    def card_of(c: WorkCombo) -> dict:
        n = numbering[c.id]
        key2 = _key(c.product, c.part)
        r = routes[key2]
        ss = steps_map.get(r.id, [])
        parent = by_id.get(n["parent_id"]) if n["parent_id"] else None
        own = own_of.get(c.id, 0.0)
        sub = subtotal.get(c.id, own)
        # 下级 = 能出卡的子件 + 结构里有但没工艺的占位件，按结构序号混排。
        # 只列能出卡的，装配结构里新加的件在这里是隐形的 —— 用户会以为
        # 「结构改了、工艺卡没跟着变」。
        children = [
            brief(
                {
                    "combo_id": x.id,
                    "code": numbering[x.id]["code"],
                    "part": x.part,
                    "level_name": level_name(x.level),
                    "qty_per_unit": round(float(x.qty_per_unit or 1), 4),
                    "step_count": step_count.get(x.id, 0),
                    "own_hours": own_of.get(x.id, 0.0),
                    "subtotal_hours": subtotal.get(x.id, 0.0),
                    "seq": int(x.seq if x.seq is not None else 100),
                    "status": x.status,
                    "has_route": True,
                }
            )
            for x in kids.get(c.id, [])
        ] + [
            brief(placeholder_node(ch))
            for ch in struct_kids.get(key2, [])
            if _key(ch.product, ch.part) not in printable_keys
        ]
        children.sort(key=lambda x: (x["seq"], x["part"] or ""))

        # 本批要做几件：任务上写了数量就用它，否则「投产台数 × 每台用量」，
        # 再否则按单台。定额合计跟着这个数走，印错了整张卡的工时就是错的。
        per = round(float(c.qty_per_unit or 1), 4)
        tr_units = tr_qty.get(key2, 0.0) if task_id else 0.0
        if tr_units > 0:
            print_qty, qty_src = tr_units, "route"
        elif task and float(task.get("qty") or 0) > 0:
            print_qty, qty_src = round(float(task["qty"]) * per, 4), "task"
        else:
            print_qty, qty_src = per, "unit"

        st = stats.get(key2) or {}
        return {
            "combo_id": c.id,
            "code": n["code"],
            "product": c.product,
            "part": c.part,
            "level": c.level,
            "level_name": level_name(c.level),
            "level_src": c.level_src,
            "parent_code": numbering[parent.id]["code"] if parent else "",
            "parent_part": parent.part if parent else "",
            "qty_per_unit": per,
            "print_qty": print_qty,
            "qty_src": qty_src,
            "material": st.get("material", ""),
            "route_id": r.id,
            "version": r.version,
            "completeness": r.completeness,
            "batch_qty": r.batch_qty,
            "allowance_rate": r.allowance_rate,
            "remark": r.remark,
            "published_at": r.published_at.isoformat() if r.published_at else None,
            "published_by": r.published_by or "",
            "steps": [step_out(s) for s in ss],
            "step_count": len(ss),
            "pages": max(1, -(-len(ss) // 10)),
            "own_hours": round(own, 4),
            "setup_hours": round(setup_of.get(c.id, 0.0), 4),
            # 本批定额 = 本层单件定额 × 本批件数 + 准备工时
            "batch_hours": round(own * print_qty + setup_of.get(c.id, 0.0), 4),
            "subtotal_hours": round(sub, 4),
            "product_hours": product_hours,
            "share_pct": round(own / product_hours * 100, 1) if product_hours else 0.0,
            "task": task if task_id else None,
            "sheets": {
                "count": st.get("count", 0),
                "hours": st.get("hours", 0.0),
                "last_id": st.get("last_no") or None,
                "ids": st.get("sheet_ids") or [],
                # 扫码报工的单据（预印单号），点不进核对页 —— 它没有照片可核
                "scan_count": len(st.get("scan_forms") or []),
                "last_form_no": st.get("last_form_no") or "",
            },
            "child_count": len(children),
            "children": children,
            "note": c.note,
        }

    cards = [card_of(c) for c in picked]

    # 这批卡自己的合计：只累计「本批里的顶层卡」，否则含子件会被重复相加
    picked_ids = {c.id for c in picked}
    batch_hours = round(
        sum(
            subtotal.get(c.id, 0.0)
            for c in picked
            if numbering[c.id]["parent_id"] not in picked_ids
        ),
        4,
    )

    # ---- 左栏树：和卡片一一对应，看到的就是会印出来的 ----
    def node(c: WorkCombo) -> dict:
        n = numbering[c.id]
        key2 = _key(c.product, c.part)
        own_kids = [node(x) for x in kids.get(c.id, [])]
        # 结构里有、工艺还没建的直接下级 → 以「未建」占位出现在它下面。
        # 不参与编号、不印卡，只是让人看见「这件已经挂进装配结构了」。
        own_kids += [
            placeholder_node(ch)
            for ch in struct_kids.get(key2, [])
            if _key(ch.product, ch.part) not in printable_keys
        ]
        own_kids.sort(key=lambda x: (x.get("seq") or 100, x.get("part") or ""))
        return {
            "combo_id": c.id,
            "code": n["code"],
            "part": c.part,
            "level": c.level,
            "level_name": level_name(c.level),
            "seq": int(c.seq if c.seq is not None else 100),
            "qty_per_unit": round(float(c.qty_per_unit or 1), 4),
            "step_count": step_count.get(c.id, 0),
            "own_hours": own_of.get(c.id, 0.0),
            "subtotal_hours": subtotal.get(c.id, 0.0),
            "version": routes[key2].version,
            "sheet_count": (stats.get(key2) or {}).get("count", 0),
            "status": c.status,
            "has_route": True,
            "children": own_kids,
        }

    roots = [
        node(c)
        for c in sorted(
            kids.get(None, []),
            key=lambda x: numbering[x.id]["order"],
        )
    ]

    # 结构里有、但这一批出不了卡的件数（只数挂在已出卡部件下面的那些）。
    # 界面用它提示「不是漏印，是还没编工艺」—— 两个数混在一起会让人白找半天。
    def count_unbuilt(nodes: list[dict]) -> int:
        return sum(
            (0 if n.get("has_route") else 1) + count_unbuilt(n.get("children") or [])
            for n in nodes
        )

    unbuilt_n = count_unbuilt(roots)

    cur = next((p for p in prods if p["product"] == product), None)
    # 按任务出卡时没有「产品」这一维（一单可能跨产品），草稿/停用直接数本单挂着的组合；
    # 否则会显示「未发布 0」而实际缺了三张卡 —— 那两个数在摘要条上挨着「缺卡」，
    # 一假就让人以为只是没印完。
    if task_id:
        draft_n = sum(1 for c in combos if c.status == "draft")
        archived_n = sum(1 for c in combos if c.status == "archived")
    else:
        draft_n = cur["draft"] if cur else 0
        archived_n = cur["archived"] if cur else 0
    return {
        "product": product,
        "product_id": cur["product_id"] if cur else None,
        "products": prods,
        "task": task,
        "tasks": tasks(db),
        "tree": roots,
        "cards": cards,
        "missing": missing,
        "summary": {
            "product": product,
            "task_id": task_id,
            "released": len(released),
            "printed": len(cards),
            "pages": sum(c["pages"] for c in cards),
            "step_count": sum(c["step_count"] for c in cards),
            "own_hours": round(sum(c["own_hours"] for c in cards), 4),
            # 本批定额：按本批件数算，挂任务时才有意义；没挂任务等于单台合计
            "batch_hours": round(sum(c["batch_hours"] for c in cards), 4),
            "subtotal_hours": batch_hours,
            "product_hours": product_hours,
            "missing": len(missing),
            "draft": draft_n,
            "archived": archived_n,
            "with_children": sum(1 for c in cards if c["child_count"]),
            # 层级里出现、但没工艺所以不出卡的件（结构有、工艺没建）
            "unbuilt": unbuilt_n,
            "max_level": max((c["level"] for c in cards), default=0),
            # 已经录进来的加工路线单：卡片是「还没干」，单据是「干完了」
            "reported_cards": sum(1 for c in cards if c["sheets"]["count"]),
            "sheet_count": sum(c["sheets"]["count"] for c in cards),
            "sheet_hours": round(sum(c["sheets"]["hours"] for c in cards), 4),
        },
        "cover": {
            "product": product,
            "task": task,
            "items": [
                {
                    "code": c["code"],
                    "part": c["part"],
                    "level_name": c["level_name"],
                    "parent_code": c["parent_code"],
                    "qty_per_unit": c["qty_per_unit"],
                    "print_qty": c["print_qty"],
                    "version": c["version"],
                    "step_count": c["step_count"],
                    "pages": c["pages"],
                    "own_hours": c["own_hours"],
                    "batch_hours": c["batch_hours"],
                    "subtotal_hours": c["subtotal_hours"],
                    "published_at": c["published_at"],
                    "sheet_count": c["sheets"]["count"],
                }
                for c in cards
            ],
            "total_hours": batch_hours,
        },
        "printed_by": printed_by,
        "printed_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
