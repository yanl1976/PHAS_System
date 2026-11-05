"""产品结构的「计划生产数量」与结构新增/删除。

两件事放在一起，是因为它们共用同一棵树、同一套父子校验：

1. **计划数量联动**。结构上原来只有 `qty_per_unit`（装一台上级用几件），
   它回答「设计上要几件」，回答不了「这一批做几件」。现在多一个 `plan_qty`：

       子件计划量 = 父件计划量 × 本级每台用量

   父件改了，下面自动重算；子件改了，按用量把上级反推上去（反推不整除时
   向上取整 —— 计划是「要做几台」，不能是 1.5 台）。两边都能改，
   这就是车间说的「反过来同样的道理」。

2. **结构新增**。`product_node` 原来没有任何新增入口：结构只能靠迁移和
   按件名重铺生成，工艺员想补一个漏登记的件只能去改库。这里补上，
   并且只认「结构」这一层 —— 新件要不要编工艺，是下一步的事。

**人工设定优先**：`plan_src='manual'` 的件不会被上级变化冲掉。派生值只是
默认值，人一旦明确指定，就以人的为准；界面上会把这些件标出来。
"""
from __future__ import annotations

import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, ProductNode, WorkCombo

MAX_DEPTH = 5  # 与 combo_registry 一致：再深人就看不清了


def _key(product: str | None, part: str | None) -> tuple[str, str]:
    return ((product or "").strip(), (part or "").strip())


def _audit(db: Session, node: ProductNode, field: str, old, new, user: str) -> None:
    if str(old) == str(new):
        return
    db.add(
        AuditLog(
            entity="node",
            entity_id=node.id,
            sheet_id=None,
            field=field,
            old_value=str(old)[:500],
            new_value=str(new)[:500],
            user=user or "system",
        )
    )


# ---------------------------------------------------------------- 树工具


def _load(db: Session, product: str) -> tuple[dict[int, ProductNode], dict[int, list[ProductNode]]]:
    """把一个产品的结构一次读进来：id → 节点、父 id → 子节点（按 seq 排）。"""
    rows = db.scalars(select(ProductNode).where(ProductNode.product == product)).all()
    by_id = {n.id: n for n in rows}
    kids: dict[int, list[ProductNode]] = {}
    for n in rows:
        if n.parent_id is not None:
            kids.setdefault(n.parent_id, []).append(n)
    for lst in kids.values():
        lst.sort(key=lambda n: (int(n.seq or 0), n.part or ""))
    return by_id, kids


def roots_of(by_id: dict[int, ProductNode], kids: dict[int, list[ProductNode]]) -> list[ProductNode]:
    """根 = 没有上级，或上级已经不在这棵树里（孤儿件按根处理，不能让它凭空消失）。"""
    return [n for n in by_id.values() if n.parent_id is None or n.parent_id not in by_id]


def recalc_subtree(node: ProductNode, kids: dict[int, list[ProductNode]]) -> list[int]:
    """自 `node` 往下重算派生计划量，返回真正被改动的节点 id。

    只改 `plan_src='auto'` 的：人工设定过的件保留原值，但它下面的 auto 后代
    仍按它自己的计划量继续推 —— 人的决定只影响这一件，不该影响整枝。
    """
    changed: list[int] = []
    stack = [node]
    seen: set[int] = set()
    while stack:
        cur = stack.pop()
        if cur.id in seen:  # 防御：库里真出现环时不至于把进程转死
            continue
        seen.add(cur.id)
        for child in kids.get(cur.id, []):
            if (child.plan_src or "auto") == "auto":
                want = round(float(cur.plan_qty or 0) * float(child.qty_per_unit or 1), 4)
                if abs(want - float(child.plan_qty or 0)) > 1e-9:
                    child.plan_qty = want
                    changed.append(child.id)
            stack.append(child)
    return changed


def recalc_product(db: Session, product: str) -> list[int]:
    """整棵产品结构重算一遍。改用量、挪层级、加删件之后都要跑。"""
    by_id, kids = _load(db, product)
    changed: list[int] = []
    for root in roots_of(by_id, kids):
        changed.extend(recalc_subtree(root, kids))
    if changed:
        db.flush()
    return changed


def _find(db: Session, product: str, part: str) -> ProductNode | None:
    return db.scalar(
        select(ProductNode).where(
            ProductNode.product == product, ProductNode.part == part
        )
    )


def plan_payload(node: ProductNode) -> dict:
    """给前端的一行计划量。`derived` 方便界面标出「这行是人工定的」。"""
    return {
        "id": node.id,
        "product": node.product,
        "part": node.part,
        "parent_id": node.parent_id,
        "level": node.level,
        "qty_per_unit": round(float(node.qty_per_unit or 1), 4),
        "plan_qty": round(float(node.plan_qty or 0), 4),
        "plan_src": node.plan_src or "auto",
        "derived": (node.plan_src or "auto") == "auto",
    }


# ---------------------------------------------------------------- 改计划量


def set_plan(db: Session, product: str, part: str, plan_qty: float, *, user: str = "") -> dict:
    """设定一个件的计划数量，并把整棵树调成自洽。

    规则（写在这里，因为它是这一页唯一的口径）：

    * **向下**：本件的 auto 后代 = 本件计划量 × 各级用量，逐层推。
    * **向上**：本件的上级按「本件计划量 ÷ 本件用量」反推；不整除时向上取整
      （计划是台数，不能是 1.5 台），并把取整前后都写进 warnings 让人看见。
      上级改了，它的其它 auto 分支跟着重算，然后继续往上 —— 一直推到根，
      否则「零件 3 个、部件 1 台」这种自相矛盾会留在库里。
    * **人工设定优先**：反推停在一个 `manual` 且数值已经对得上的上级，
      不再继续往上动它。
    """
    product, part = _key(product, part)
    try:
        qty = float(plan_qty)
    except (TypeError, ValueError) as exc:
        raise ValueError("计划数量要是数字") from exc
    if qty < 0:
        raise ValueError("计划数量不能是负数")

    by_id, kids = _load(db, product)
    node = _find(db, product, part)
    if node is None:
        raise ValueError("结构里没有这一件，先把它加进结构")

    old_qty = round(float(node.plan_qty or 0), 4)
    node.plan_qty = round(qty, 4)
    node.plan_src = "manual"
    _audit(db, node, "plan_qty", old_qty, node.plan_qty, user)
    touched = [node.id]
    touched.extend(recalc_subtree(node, kids))

    warnings: list[str] = []
    cur = node
    seen: set[int] = {node.id}
    while cur.parent_id and cur.parent_id in by_id:
        parent = by_id[cur.parent_id]
        if parent.id in seen:
            break
        seen.add(parent.id)
        per = float(cur.qty_per_unit or 1) or 1.0
        want = float(cur.plan_qty or 0) / per
        exact = round(want, 4)
        if abs(want - round(want)) > 1e-9:
            want = float(math.ceil(want - 1e-9))
            warnings.append(
                f"「{cur.part}」计划 {cur.plan_qty:g} 件 ÷ 每台用量 {per:g} = {exact:g}，"
                f"已把「{parent.part}」向上取整为 {want:g}"
            )
        if abs(want - float(parent.plan_qty or 0)) <= 1e-9:
            # 上级本来就对得上：它之上的层也就都不用动
            break
        old = float(parent.plan_qty or 0)
        parent.plan_qty = round(want, 4)
        parent.plan_src = "manual"
        _audit(db, parent, "plan_qty", old, parent.plan_qty, user)
        touched.append(parent.id)
        # 上级变了，它下面别的 auto 分支也要跟着变
        touched.extend(recalc_subtree(parent, kids))
        cur = parent

    db.flush()
    changed = list(dict.fromkeys(touched))
    return {
        "node": plan_payload(node),
        "updated": len(changed),
        "updated_ids": changed,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- 新增 / 删除


def create_node(
    db: Session,
    product: str,
    part: str,
    *,
    parent_part: str | None = None,
    qty_per_unit: float = 1.0,
    plan_qty: float | None = None,
    seq: int | None = None,
    user: str = "",
) -> dict:
    """在现有结构里新增一件。

    `parent_part` 为空 = 挂在顶层。新件的计划量默认按「上级计划量 × 用量」算；
    人填了就以人填的为准（plan_src='manual'），并且**不反推上级** ——
    刚补登记一个件就顺手改掉整机计划量，是没人预料得到的副作用。
    """
    product, part = _key(product, part)
    if not product:
        raise ValueError("产品不能为空：结构必须归属某个产品")
    if not part:
        raise ValueError("件名不能为空")

    by_id, kids = _load(db, product)
    if any(n.part == part for n in by_id.values()):
        raise ValueError(f"「{product}」的结构里已经有「{part}」了")

    parent: ProductNode | None = None
    if parent_part and parent_part.strip():
        parent = _find(db, product, parent_part.strip())
        if parent is None:
            raise ValueError(f"上级「{parent_part}」不在「{product}」的结构里")
        if int(parent.level or 0) + 1 > MAX_DEPTH:
            raise ValueError(f"层级太深了（超过 {MAX_DEPTH} 层），先把上级理顺")
    try:
        per = float(qty_per_unit if qty_per_unit is not None else 1.0)
    except (TypeError, ValueError) as exc:
        raise ValueError("每台用量要是数字") from exc
    if per <= 0:
        raise ValueError("每台用量必须大于 0")

    if parent is not None:
        sibs = kids.get(parent.id, [])
    else:
        # 顶层：没有上级的，加上「上级已不在结构里」的孤儿（它们同样按根显示）
        sibs = [n for n in by_id.values() if n.parent_id is None or n.parent_id not in by_id]
    next_seq = seq if seq is not None else (
        (max(int(x.seq or 0) for x in sibs) + 10) if sibs else 100
    )

    node = ProductNode(
        product=product,
        part=part,
        parent_id=parent.id if parent else None,
        level=(int(parent.level or 0) + 1) if parent else 0,
        qty_per_unit=per,
        seq=int(next_seq),
        source="manual",
        plan_qty=1.0,
    )
    if plan_qty is None:
        node.plan_src = "auto"
        node.plan_qty = round(float(parent.plan_qty or 0) * per, 4) if parent else 1.0
    else:
        try:
            pv = float(plan_qty)
        except (TypeError, ValueError) as exc:
            raise ValueError("计划数量要是数字") from exc
        if pv < 0:
            raise ValueError("计划数量不能是负数")
        node.plan_src = "manual"
        node.plan_qty = round(pv, 4)

    db.add(node)
    db.flush()
    _audit(
        db,
        node,
        "node:create",
        "",
        f"{product}/{part} parent={(parent.part if parent else '顶层')} qty={per:g}",
        user,
    )
    return {"node": plan_payload(node), "parent": parent.part if parent else ""}


def delete_guards(db: Session, node: ProductNode, *, shell_combo: bool = False) -> list[str]:
    """删件前的护栏。宁可拦严：删掉一个已被工艺/报工引用的件，
    工时和工资表就会指向一个不存在的位置。

    `shell_combo=True` 表示这条「工艺身份」是空壳（没路线、没历史、没引用），
    此时它不构成拦截理由 —— 拦它等于「工艺删了、件却删不掉」，
    下面 process_route / task_route / work_item 三条已经覆盖了真实引用。
    """
    from ..models import ProcessRoute, TaskRoute, WorkItem

    reasons: list[str] = []
    kids = db.scalars(
        select(ProductNode).where(ProductNode.parent_id == node.id)
    ).all()
    if kids:
        reasons.append(f"它还挂着 {len(kids)} 个下级件，先把下级挪走或删掉")
    n_combo = db.scalar(
        select(WorkCombo).where(
            WorkCombo.product == node.product, WorkCombo.part == node.part
        )
    )
    if n_combo is not None and not shell_combo:
        reasons.append("它还有工艺身份（工时组合），先把工艺删干净再删结构")
    n_route = db.scalars(
        select(ProcessRoute).where(
            ProcessRoute.kind == "route",
            ProcessRoute.product == node.product,
            ProcessRoute.part == node.part,
        )
    ).all()
    if n_route:
        reasons.append(f"它编过 {len(n_route)} 版工艺路线")
    n_task = db.scalars(
        select(TaskRoute).where(
            TaskRoute.product == node.product, TaskRoute.part == node.part
        )
    ).all()
    if n_task:
        reasons.append(f"有 {len(n_task)} 个订单引用了它")
    n_item = db.scalars(
        select(WorkItem).where(
            WorkItem.product_name == node.product, WorkItem.part_name == node.part
        )
    ).all()
    if n_item:
        reasons.append(f"有 {len(n_item)} 条报工明细记在它名下")
    return reasons


def delete_node(db: Session, product: str, part: str, *, user: str = "") -> dict:
    product, part = _key(product, part)
    node = _find(db, product, part)
    if node is None:
        raise ValueError("结构里没有这一件")

    # 空壳身份（删完最后一版工艺留下的残渣）不该拦住删件，删件时顺手收掉。
    # 这是历史数据的自愈口：旧版本删路线不会清理 work_combo。
    from . import combo_registry as CR

    combo = db.scalar(
        select(WorkCombo).where(WorkCombo.product == product, WorkCombo.part == part)
    )
    shell = combo is not None and CR.orphan_shell(db, combo)

    reasons = delete_guards(db, node, shell_combo=shell)
    if reasons:
        raise ValueError("这一件不能删：" + "；".join(reasons))
    if shell and combo is not None:
        CR.drop_shell(db, combo, user=user, why="删结构件时清掉空壳工艺身份")
    parent_id = node.parent_id
    _audit(db, node, "node:delete", f"{product}/{part}", "", user)
    db.delete(node)
    db.flush()
    if parent_id:
        by_id, kids = _load(db, product)
        parent = by_id.get(parent_id)
        if parent is not None:
            recalc_subtree(parent, kids)
            db.flush()
    return {"deleted": {"product": product, "part": part}}
