"""工时组合：登记、部件层级、发布与撤回。

这个模块解决的是三件在旧实现里根本不存在的事：

1. **身份**。组合原本是 `standard_hour ∪ process_route` 每次现算的并集，
   没有主键、没有归属、没有状态。这里把它落成 `work_combo` 一行。
2. **对外可用**。只有 `status='released'` 的组合才会被识别校验、任务挂线、
   工艺卡读取。草稿一律不出现 —— 这是「工时组合默认均为草稿」的落点。
   发布只有**一步**（draft → released），不做两级审批。
3. **部件层级**。产品由若干部件总装而成，工艺组合必须能表达这层关系，
   否则工艺卡只能按零件散装订，看不出「这台机器是由哪几块组成的」。

层级怎么铺：不要求人先把 889 条理顺。迁移时按件名规则自动铺一层
（`整机装配/总装` 当根，`xx组件/xx总成/xx总包` 当部件挂根，其余零件挂根），
工艺员之后只把真正有子件的部件往下沉。`level_src` 区分自动与人工，
重铺时绝不动人工定过的。
"""
from __future__ import annotations

import datetime as dt
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    AuditLog,
    ProcessRoute,
    ProductNode,
    ProcessRouteStep,
    StandardHour,
    TaskRoute,
    WorkCombo,
    WorkItem,
    WorkTask,
)
from . import routes as route_service

# 件名里出现这些词就当作「整机/总装」那一层。宁可认不出（退回无根的两层），
# 也不要乱认 —— 认错了会把整个产品的挂载关系挂歪。
ROOT_HINTS = ("整机装配", "总装", "整机", "总成装配", "部件装配", "总体装配")
# 这些后缀说明该件名本身由若干零件组成，可以当中间层（部件）。
COMPONENT_SUFFIX = (
    "组件", "总成", "总包", "总装", "部件", "单元", "模块", "分组件", "部装",
    "组装", "组合", "机组", "装置",
)
MAX_DEPTH = 5


def _key(product: str | None, part: str | None) -> tuple[str, str]:
    return ((product or "").strip(), (part or "").strip())


def is_root_name(part: str) -> bool:
    name = (part or "").strip()
    return any(h in name for h in ROOT_HINTS)


def is_component_name(part: str) -> bool:
    name = (part or "").strip()
    return any(name.endswith(s) for s in COMPONENT_SUFFIX)


def _core(name: str) -> str:
    """件名去掉「组件/总成/总包…」后缀后的核心词，用来做前缀归组。"""
    for s in COMPONENT_SUFFIX:
        if name.endswith(s) and len(name) > len(s):
            return name[: -len(s)]
    return name


def _audit(
    db: Session, combo, field: str, old, new, user: str, entity: str = "combo"
) -> None:
    if str(old) == str(new):
        return
    db.add(
        AuditLog(
            entity=entity,
            entity_id=combo.id,
            sheet_id=None,
            field=field,
            old_value=str(old)[:500],
            new_value=str(new)[:500],
            user=user or "system",
        )
    )


# ---------------------------------------------------------------- 登记与同步


def ensure_node(db: Session, product: str, part: str, *, source: str = "auto") -> ProductNode | None:
    """确保结构里有这一件。新登记的组合要能立刻出现在结构里。

    以前结构和组合是同一行，登记组合就等于有了位置；拆表之后必须显式补这一行，
    否则工艺员在编排页左栏找不到刚建的件。新件挂在同产品的总装下；
    这个产品还没有总装就当顶层 —— 不为了「好看」去造一个总装。
    """
    product = (product or "").strip()
    part = (part or "").strip()
    if not part:
        return None
    row = db.scalar(
        select(ProductNode).where(ProductNode.product == product, ProductNode.part == part)
    )
    if row is not None:
        return row
    root = db.scalar(
        select(ProductNode)
        .where(ProductNode.product == product, ProductNode.level == 0)
        .order_by(ProductNode.seq)
    )
    pid = root.id if root else None
    sibs = db.scalars(
        select(ProductNode).where(ProductNode.product == product, ProductNode.parent_id == pid)
    ).all()
    row = ProductNode(
        product=product,
        part=part,
        parent_id=pid,
        level=1 if root else 0,
        qty_per_unit=1.0,
        seq=(max(int(x.seq or 0) for x in sibs) + 10) if sibs else 100,
        source=source,
    )
    # 计划量按上级派生（用量 1）：新件一进结构就带上「这一批要做几件」，
    # 否则结构树上这一列是空的，人会以为它不用做。
    row.plan_src = "auto"
    row.plan_qty = round(float(root.plan_qty or 0), 4) if root else 1.0
    db.add(row)
    db.flush()
    return row


def sync_combos(db: Session, *, actor: str = "migrate") -> dict:
    """把「历史工时里出现」或「已建路线」的组合补登记进 work_combo。

    迁移之后仍然需要它：录一张老单会带出新的产品×件名，工艺科得能在台账里
    看见并要求确认，而不是等某个人想起来才建。新登记的默认是草稿。
    """
    existing: dict[tuple[str, str], WorkCombo] = {
        (c.product, c.part): c for c in db.scalars(select(WorkCombo)).all()
    }
    created: list[tuple[str, str]] = []

    def ensure(product: str, part: str, product_id: int | None) -> None:
        key = _key(product, part)
        if not key[1]:  # 件名为空的组合没有身份可言，不进台账
            return
        if key in existing:
            row = existing[key]
            if row.product_id is None and product_id:
                row.product_id = product_id
                db.add(row)
            return
        row = WorkCombo(
            product=key[0],
            part=key[1],
            product_id=product_id,
            status="draft",
            created_by=actor,
            note="自动登记：首次出现在工时账或工艺路线里",
        )
        db.add(row)
        db.flush()
        existing[key] = row
        created.append(key)
        # 结构跟着登记：组合是「这件有工艺」，结构是「这件装在哪」，两边都要有
        ensure_node(db, key[0], key[1])

    for h in db.scalars(select(StandardHour)).all():
        ensure(h.product or "", h.part or "", h.product_id)

    for r in db.scalars(select(ProcessRoute).where(ProcessRoute.kind == "route")).all():
        ensure(r.product or "", r.part or "", r.product_id)

    db.flush()
    return {"created": len(created), "total": len(existing), "keys": created}


def backfill_product_ids(db: Session) -> int:
    """补产品外键。分类树靠它聚合，文本写法有 6 种变体，只按文本分会裂开。"""
    from .tasks import resolve_product

    n = 0
    for c in db.scalars(select(WorkCombo).where(WorkCombo.product_id.is_(None))).all():
        found = resolve_product(db, c.product) if c.product else None
        if found:
            c.product_id = found.id
            db.add(c)
            n += 1
    db.flush()
    return n


# ------------------------------------------------------- 空壳身份与残渣清理


def orphan_shell(db: Session, combo: WorkCombo) -> bool:
    """这条「工艺身份」是不是空壳：一个真实引用都没有。

    场景（现场踩过）：新建件 → 编工艺 → 发布 → 撤回发布 → 删掉那一版工艺，
    `process_route` 已经空了，`work_combo` 这一行却还在、`status` 还挂着
    draft。于是两处都不对劲：

    * 树上显示「草稿」，让人以为还有一份工艺躺在那里；
    * 回头删这个结构件，被「有工时组合（工艺身份）」拦住 —— 工艺明明删了。

    身份存在的意义是「这一件有工艺」，工艺没了、历史工时也没有、任务和报工
    都没引用过，这行就只是删路线留下的残渣，不该继续冒充身份。

    判断一律看**真实引用**，不看状态字段：状态是人对外的标签，引用才是事实。
    """
    if combo.status == "released":
        # 已发布却没有路线是另一种异常（数据不一致），留给台账报警，
        # 绝不在这里静默消灭 —— 那等于把「发布过什么」的证据抹掉。
        return False
    product = (combo.product or "").strip()
    part = (combo.part or "").strip()
    if not part:
        return True
    probes = (
        select(ProcessRoute.id).where(
            ProcessRoute.kind == "route",
            ProcessRoute.product == product,
            ProcessRoute.part == part,
        ),
        select(StandardHour.id).where(
            StandardHour.product == product, StandardHour.part == part
        ),
        select(TaskRoute.id).where(
            TaskRoute.product == product, TaskRoute.part == part
        ),
        select(WorkItem.id).where(
            WorkItem.product_name == product, WorkItem.part_name == part
        ),
    )
    return all(db.scalar(q.limit(1)) is None for q in probes)


def drop_shell(db: Session, combo: WorkCombo, *, user: str = "", why: str = "") -> None:
    """删掉一条空壳身份，并留下审计（谁、因为什么删的）。"""
    _audit(
        db,
        combo,
        "combo:drop_shell",
        f"status={combo.status}",
        why or "无路线/无历史工时/无任务与报工引用",
        user,
    )
    db.delete(combo)
    db.flush()


def cleanup_after_route_delete(
    db: Session, product: str, part: str, *, user: str = "", removed_id: int | None = None
) -> dict:
    """删掉一版路线之后收拾组合身份，别留删不掉的残渣。

    * 还有别的路线 → 只清悬空的 `route_id`，别的什么都不动（那不叫空壳）；
    * 已经没有任何路线、且这条身份一个引用都没有 → 连同身份一起删掉，
      这一件回到「结构里有、工艺还没编」的正常状态；
    * 有历史工时或任务引用 → 身份留着（那是真实存在过的工艺）。
    """
    product = (product or "").strip()
    part = (part or "").strip()
    left = db.scalar(
        select(ProcessRoute.id)
        .where(
            ProcessRoute.kind == "route",
            ProcessRoute.product == product,
            ProcessRoute.part == part,
        )
        .limit(1)
    )
    combo = db.scalar(
        select(WorkCombo).where(WorkCombo.product == product, WorkCombo.part == part)
    )
    if combo is None:
        return {"dropped": False, "kept": False}

    # 悬空引用：组合说「当前生效的是 v3」，v3 刚被删掉
    if combo.route_id and (
        combo.route_id == removed_id or db.get(ProcessRoute, combo.route_id) is None
    ):
        combo.route_id = None
        db.add(combo)
        db.flush()

    if left is not None:
        return {"dropped": False, "kept": False}
    if orphan_shell(db, combo):
        drop_shell(db, combo, user=user, why="最后一版工艺已删除")
        return {"dropped": True, "kept": False}
    return {"dropped": False, "kept": True}


# ---------------------------------------------------------------- 层级


def _match_component(part: str, components: list[ProductNode]) -> ProductNode | None:
    """零件名以某个部件核心词开头 → 认为它装在那个部件里。

    只做前缀匹配、且取最长核心词：`料仓水套` 挂 `料仓组件`，
    `料仓盖板` 也挂 `料仓组件`；`主轴` 匹配不上任何部件 → 留在根下。
    匹配不上的留在根下是对的 —— 少挂一层比挂错一层好查。
    """
    name = (part or "").strip()
    best: tuple[int, ProductNode] | None = None
    for c in components:
        core = _core(c.part)
        if len(core) < 2:
            continue
        if name.startswith(core) and name != c.part:
            if best is None or len(core) > best[0]:
                best = (len(core), c)
    return best[1] if best else None


def layout_levels(db: Session, product: str, *, only_auto: bool = True) -> dict:
    """按件名规则给一个产品铺层级。返回各层计数，便于预演时给人看。"""
    combos = db.scalars(
        select(ProductNode).where(ProductNode.product == product).order_by(ProductNode.part)
    ).all()
    if not combos:
        return {"product": product, "root": None, "components": 0, "leaves": 0, "skipped": 0}

    movable = [c for c in combos if (c.source != "manual") or not only_auto]
    skipped = len(combos) - len(movable)

    roots = [c for c in movable if is_root_name(c.part)]
    # 多个件名都像总装时只认一个：优先「整机装配」，其次名字最短的。
    root = None
    if roots:
        root = sorted(roots, key=lambda c: (0 if "整机" in c.part else 1, len(c.part)))[0]

    components = [
        c for c in movable if c is not root and is_component_name(c.part) and not is_root_name(c.part)
    ]
    leaves = [c for c in movable if c not in components and c is not root]

    if root:
        root.level = 0
        root.parent_id = None
        root.seq = 10
        db.add(root)

    for i, c in enumerate(sorted(components, key=lambda x: x.part), start=1):
        c.level = 1
        c.parent_id = root.id if root else None
        c.seq = 20 + i
        db.add(c)

    for i, c in enumerate(sorted(leaves, key=lambda x: x.part), start=1):
        target = _match_component(c.part, components)
        if target is not None:
            c.level = 2
            c.parent_id = target.id
        else:
            c.level = 1
            c.parent_id = root.id if root else None
        c.seq = 100 + i
        db.add(c)

    db.flush()
    return {
        "product": product,
        "root": root.part if root else "",
        "components": len(components),
        "leaves": len(leaves),
        "skipped": skipped,
    }


def layout_all(db: Session, *, only_auto: bool = True) -> list[dict]:
    products = [
        p for (p,) in db.execute(select(ProductNode.product).distinct()).all() if p
    ]
    return [layout_levels(db, p, only_auto=only_auto) for p in sorted(products)]


def validate_level(db: Session, combo: ProductNode, parent_id: int | None) -> str:
    """层级合法性校验，返回错误文案（空串=通过）。"""
    if parent_id is None:
        return ""
    if parent_id == combo.id:
        return "上级不能是它自己"
    parent = db.get(ProductNode, parent_id)
    if parent is None:
        return "上级组合不存在"
    # 只比产品，不能比整个 (产品, 件名) 键 —— 那个永远不相等，
    # 等于把所有挂载都判成跨产品
    if (parent.product or "").strip() != (combo.product or "").strip():
        return "上级必须属于同一个产品"
    # 成环检测：顺着 parent 链往上走，撞到自己就是环
    seen = {combo.id}
    cur = parent
    depth = 1
    while cur is not None and depth <= MAX_DEPTH + 1:
        if cur.id in seen:
            return "层级成环"
        seen.add(cur.id)
        depth += 1
        if depth > MAX_DEPTH:
            return f"层级超过 {MAX_DEPTH} 层"
        cur = db.get(ProductNode, cur.parent_id) if cur.parent_id else None
    return ""


def level_path(db: Session, combo: ProductNode) -> list[str]:
    """从总装到自己的件名链，改层级后回给前端做「改后位置」提示。

    只认真实 `parent_id`。链断了（上级被删/跨产品）就停在那儿，
    不补虚拟节点，免得提示里出现一个库里没有的名字。
    """
    out: list[str] = []
    cur: ProductNode | None = combo
    seen: set[int] = set()
    while cur is not None and len(out) <= MAX_DEPTH:
        if cur.id in seen:
            break
        seen.add(cur.id)
        out.append(cur.part or "")
        cur = db.get(ProductNode, cur.parent_id) if cur.parent_id else None
    return list(reversed(out))


def relevel_subtree(db: Session, combo: ProductNode) -> int:
    """把 combo 下面所有子孙的层号按新深度重算，返回改动的条数。

    只改 `level`，不动 `parent_id`：相对位置没变，是整支被抬起来或沉下去了。
    不重算的话，把「包板总包」从根沉到某部件下，它的子件层号还停在 2，
    工艺卡上会算出一个和实际深度对不上的件号。
    """
    rows = db.scalars(select(ProductNode).where(ProductNode.product == combo.product)).all()
    kids: dict[int, list[ProductNode]] = {}
    for c in rows:
        if c.parent_id:
            kids.setdefault(c.parent_id, []).append(c)

    changed = 0
    seen = {combo.id}
    stack = [(combo.id, int(combo.level or 0))]
    while stack:
        pid, plv = stack.pop()
        for k in kids.get(pid, []):
            if k.id in seen:
                continue
            seen.add(k.id)
            want = plv + 1
            if int(k.level or 0) != want:
                k.level = want
                db.add(k)
                changed += 1
            stack.append((k.id, want))
    db.flush()
    return changed


def set_level(
    db: Session,
    combo: ProductNode,
    *,
    parent_part: str | None = None,
    qty_per_unit: float | None = None,
    seq: int | None = None,
    user: str = "",
) -> dict:
    """人工指定上级 / 每台用量 / 排序号。改过就打 `manual`，重铺不再动它。

    `parent_part` 三态：`None` = 不改；空串 = 提到最上层（不挂任何部件，
    层号回到 0）；其他 = 挂到同产品的那一件下。

    挂上/摘下之后要顺手给整支下级重算层号（`relevel_subtree`），否则
    「包板总包」沉下去、它的子件还停在原来的层级，工艺卡编号会算歪。
    """
    before = {
        "parent_id": combo.parent_id,
        "level": int(combo.level or 0),
        "qty_per_unit": float(combo.qty_per_unit or 0.0),
        "seq": combo.seq,
    }

    if parent_part is not None:
        parent_id = None
        name = (parent_part or "").strip()
        if name:
            parent = db.scalar(
                select(ProductNode).where(
                    ProductNode.product == combo.product, ProductNode.part == name
                )
            )
            if parent is None:
                # 件名在别的产品下存在，就不是「没有这一件」，而是「挂错产品了」：
                # 两种情形给的话得不一样，否则人会在自己的产品里找一个根本不在这儿的件
                if db.scalar(select(ProductNode.id).where(ProductNode.part == name)) is not None:
                    raise ValueError("上级必须属于同一个产品")
                raise ValueError(f"上级组合「{name}」不存在")
            err = validate_level(db, combo, parent.id)
            if err:
                raise ValueError(err)
            parent_id = parent.id
        combo.parent_id = parent_id
        combo.level = 0 if parent_id is None else int(db.get(ProductNode, parent_id).level or 0) + 1

    if qty_per_unit is not None:
        if float(qty_per_unit) <= 0:
            raise ValueError("每台用量必须大于 0")
        combo.qty_per_unit = float(qty_per_unit)
    if seq is not None:
        combo.seq = int(seq)

    combo.source = "manual"
    db.add(combo)
    db.flush()

    affected = relevel_subtree(db, combo) if parent_part is not None else 0
    for field, old in before.items():
        _audit(db, combo, field, old, getattr(combo, field), user, entity="node")
    db.flush()

    parent_row = db.get(ProductNode, combo.parent_id) if combo.parent_id else None
    return {
        "id": combo.id,
        "product": combo.product,
        "part": combo.part,
        "parent_id": combo.parent_id,
        "parent_part": (parent_row.part or "") if parent_row else "",
        "path": level_path(db, combo),
        "level": int(combo.level or 0),
        "qty_per_unit": float(combo.qty_per_unit or 0.0),
        "seq": combo.seq,
        "level_src": combo.source,
        "changed": {
            k: v for k, v in before.items() if str(v) != str(getattr(combo, k))
        },
        "affected": affected,
    }


def relevel_product(db: Session, product: str) -> int:
    """把一个产品整棵树的层号按 `parent_id` 重算一遍，返回改动条数。

    和 `relevel_subtree` 的区别：那个只顺着一支往下走，适合「移动一件」；
    批量挂载一次动几十件时，逐支重算既慢又容易漏，从根重算一遍最省心。

    成环或悬空的行不会出现在任何根下面，它们**保留原层号**：宁可让这几行
    露在外面等人修，也不要为了凑数给一个算不出来的层次。
    """
    rows = db.scalars(select(ProductNode).where(ProductNode.product == product)).all()
    by_id = {c.id: c for c in rows}
    kids: dict[int, list[ProductNode]] = {}
    for c in rows:
        pid = c.parent_id
        if pid in by_id and pid != c.id:
            kids.setdefault(pid, []).append(c)
    roots = [c for c in rows if not (c.parent_id in by_id and c.parent_id != c.id)]

    changed = 0
    seen: set[int] = set()
    stack = [(c, 0) for c in roots]
    while stack:
        node, lv = stack.pop()
        if node.id in seen:
            continue
        seen.add(node.id)
        if int(node.level or 0) != lv:
            node.level = lv
            db.add(node)
            changed += 1
        for k in kids.get(node.id, []):
            stack.append((k, lv + 1))
    db.flush()
    return changed


def attach_many(
    db: Session,
    product: str,
    parts: list[str],
    *,
    parent_part: str = "",
    seq: int | None = None,
    user: str = "",
) -> dict:
    """把一批件一次性挂到同一个上级下（`parent_part` 空串＝提到最上层）。

    这是替代「一件一件开弹窗改」的主路径：树上拖一批、或者勾一批点一次，
    都落到这一个调用上。单件失败不影响其他件 —— 一批 40 件里有 2 件名对不上，
    不该整批退回让操作人重来。
    """
    product = (product or "").strip()
    if not product:
        raise ValueError("先指定产品：层级只在同一个产品内成立")
    parent_part = (parent_part or "").strip()

    parent: ProductNode | None = None
    if parent_part:
        parent = db.scalar(
            select(ProductNode).where(ProductNode.product == product, ProductNode.part == parent_part)
        )
        if parent is None:
            # 和 set_level 一样分两种情形：跨产品是「挂错产品」，不是「没这件」
            if db.scalar(select(ProductNode.id).where(ProductNode.part == parent_part)) is not None:
                raise ValueError("上级必须属于同一个产品")
            raise ValueError(f"上级组合「{parent_part}」不存在")

    rows = {c.part: c for c in db.scalars(select(ProductNode).where(ProductNode.product == product)).all()}
    want_parent_id = parent.id if parent else None

    moved: list[str] = []
    stayed: list[str] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for raw in parts or []:
        name = (raw or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        combo = rows.get(name)
        if combo is None:
            skipped.append({"part": name, "reason": "这个产品下没有这件"})
            continue
        err = validate_level(db, combo, want_parent_id)
        if err:
            skipped.append({"part": name, "reason": err})
            continue
        if (combo.parent_id or None) == want_parent_id:
            stayed.append(name)
            continue
        _audit(db, combo, "parent_id", combo.parent_id, want_parent_id, user, entity="node")
        combo.parent_id = want_parent_id
        combo.level = 0 if want_parent_id is None else int(parent.level or 0) + 1
        combo.source = "manual"
        if seq is not None:
            # 批量落位时按给定顺序依次加 1，避免一拖多件全挤在同一个序号上
            combo.seq = int(seq) + len(moved)
        db.add(combo)
        moved.append(name)

    affected = relevel_product(db, product)
    db.flush()
    return {
        "product": product,
        "parent_part": parent_part,
        "moved": moved,
        "stayed": stayed,
        "skipped": skipped,
        "affected": affected,
    }


def _nest_parent(name: str, others: list[ProductNode]) -> ProductNode | None:
    """件名以另一个件名为前缀 → 认为装在那件里，取最长前缀。

    「刀具Ф5.0」「刀具去刺打花」都以「刀具」开头，就都挂到「刀具」下；
    「刀轮」和「刀具」没有前缀关系，认不出来 —— 认不出就**不猜**，
    留给人在树上拖一下。乱猜出来的层级比空着更难查。
    """
    best: tuple[int, int, ProductNode] | None = None
    for c in others:
        for core in {c.part, _core(c.part)}:
            core = (core or "").strip()
            if len(core) < 2 or core == name:
                continue
            if name.startswith(core):
                # 先比前缀长短，再比「是不是整名匹配」：都叫「料仓」时，
                # 优先挂到名字正好是「料仓」的那件，而不是「料仓组件」
                score = (len(core), 1 if core == (c.part or "") else 0)
                if best is None or score > (best[0], best[1]):
                    best = (score[0], score[1], c)
    return best[2] if best else None


def auto_nest(
    db: Session,
    product: str = "",
    *,
    dry_run: bool = True,
    only: list[str] | None = None,
    user: str = "",
) -> dict:
    """按件名包含关系，把「明显属于某个部件」的件一次性归位。

    只处理严格前缀、且前缀至少两个字的情形；已经挂在正确位置的不重复写。
    默认预演，人看过清单才落库 —— 这条规则是猜的，猜错了要能一眼看出来。

    `only` 给「清单里勾了几条就只落这几条」用：预演时看好编号，应用时只交
    选中的件名，中间不再重算一遍让人对着两份不同的清单发愣。
    """
    keep = {(x or "").strip() for x in (only or []) if (x or "").strip()}
    products = [product] if (product or "").strip() else [
        p for (p,) in db.execute(select(ProductNode.product).distinct()).all() if p
    ]

    items: list[dict] = []
    skipped: list[dict] = []
    for prod in sorted(products):
        rows = db.scalars(select(ProductNode).where(ProductNode.product == prod)).all()
        by_id = {c.id: c for c in rows}
        for c in sorted(rows, key=lambda x: x.part or ""):
            if is_root_name(c.part):
                continue  # 总装那一层永远在最上面，不往别处塞
            parent = _nest_parent(c.part or "", [x for x in rows if x.id != c.id])
            if parent is None:
                continue
            old = by_id.get(c.parent_id) if c.parent_id else None
            if old is not None and old.id == parent.id:
                continue
            err = validate_level(db, c, parent.id)
            if err:
                skipped.append({"product": prod, "part": c.part, "reason": err})
                continue
            items.append(
                {
                    "product": prod,
                    "part": c.part,
                    "parent": parent.part,
                    "from": old.part if old else "",
                    "level": int(parent.level or 0) + 1,
                }
            )

    if not dry_run and items:
        moved = [it for it in items if not keep or it["part"] in keep]
        for it in moved:
            c = db.scalar(
                select(ProductNode).where(
                    ProductNode.product == it["product"], ProductNode.part == it["part"]
                )
            )
            p = db.scalar(
                select(ProductNode).where(
                    ProductNode.product == it["product"], ProductNode.part == it["parent"]
                )
            )
            if c is None or p is None:
                continue
            _audit(db, c, "parent_id", c.parent_id, p.id, user, entity="node")
            c.parent_id = p.id
            c.level = int(p.level or 0) + 1
            # 人工确认过的结果，重铺时不许被冲掉
            c.source = "manual"
            db.add(c)
        for prod in {it["product"] for it in moved}:
            relevel_product(db, prod)
        db.flush()
    else:
        moved = []

    return {
        "dry_run": bool(dry_run),
        "count": len(items),
        "applied": len(moved),
        "items": items[:400],
        "products": len({it["product"] for it in items}),
        "skipped": skipped[:80],
    }


def _siblings(db: Session, product: str, parent_id: int | None, exclude: int | None = None) -> list[ProductNode]:
    """同父下的兄弟，按 (seq, 件名) 排。序号重复时靠件名兜底，顺序才稳定。"""
    rows = db.scalars(select(ProductNode).where(ProductNode.product == product)).all()
    out = [
        c
        for c in rows
        if (c.parent_id or None) == (parent_id or None) and c.id != exclude and c.id != c.parent_id
    ]
    out.sort(key=lambda c: (int(c.seq or 0), c.part or ""))
    return out


def _move_checks(combo: ProductNode) -> tuple[str, str]:
    return ((combo.product or "").strip(), (combo.part or "").strip())


def move_combo(db: Session, combo: ProductNode, action: str, *, user: str = "") -> dict:
    """方向键式的层级 / 顺序调整。四种动作，一次只动一件。

    之所以放在后端算：`seq` 和 `parent_id` 必须一起改，前端只发「往哪走」，
    落位规则（排到第几、序号取多少）只有一处，就不会两边算出不同结果。

      up      同一层里往前挪一位（和其他兄弟交换序号）
      down    同一层里往后挪一位
      indent  往右：挂到「上面最近的那个同级兄弟」下面，成为它的子件
      outdent 往左：从当前父里退出来，排在原父的后面

    分不出目标时只报错、不改数据：往上没有兄弟可挂、已经是最上层、
    或者会成环，都要原样退回并说清原因，不能悄悄挪到一个「差不多」的位置。
    """
    action = (action or "").strip().lower()
    product = (combo.product or "").strip()
    if not product:
        raise ValueError("产品名为空，层级没有归属可谈；先改名补上产品")
    before = {
        "parent_id": combo.parent_id,
        "level": int(combo.level or 0),
        "seq": combo.seq,
    }
    # 撤销用的原始快照。move 是「按下即入库」，没有保存按钮兜底，
    # 就必须让「这一步改了什么」原样退得回去：先把要动的行的旧值抄下来，
    # 之后无论怎么改，照着这份抄本写回即可。
    snap: list[dict] = []

    if action in ("up", "down"):
        sibs = _siblings(db, product, combo.parent_id)
        pos = next((i for i, c in enumerate(sibs) if c.id == combo.id), None)
        if pos is None:
            raise ValueError("这一件不在它父级的兄弟列表里，层级可能已经坏掉，先重铺一次")
        other_pos = pos - 1 if action == "up" else pos + 1
        if other_pos < 0:
            raise ValueError("已经在同一层的最前面了")
        if other_pos >= len(sibs):
            raise ValueError("已经在同一层的最后面了")
        other = sibs[other_pos]
        collided = int(other.seq or 0) == int(combo.seq or 0)
        # 撞车时整组序号都会被重编号，撤销要退就得整组一起退
        snap = _snapshot(sibs if collided else [combo, other])
        if collided:
            # 两条序号撞在一起时交换等于没动，先把整组按显示顺序重编号再换
            for i, s in enumerate(sibs, start=1):
                s.seq = i * 10
                db.add(s)
        other_seq, combo_seq = int(other.seq or 0), int(combo.seq or 0)
        _audit(db, other, "seq", other_seq, combo_seq, user, entity="node")
        _audit(db, combo, "seq", combo_seq, other_seq, user, entity="node")
        other.seq, combo.seq = combo_seq, other_seq
        db.add(other)
        db.add(combo)
        db.flush()
        return _move_result(db, combo, action, before, affected=0, snap=snap)

    if action == "indent":
        sibs = _siblings(db, product, combo.parent_id, exclude=combo.id)
        prev = None
        for s in sibs:
            if (int(s.seq or 0), s.part or "") < (int(combo.seq or 0), combo.part or ""):
                prev = s
        if prev is None:
            raise ValueError("它上面没有同级的件，没有可以挂进去的对象；先把它往下挪一格")
        err = validate_level(db, combo, prev.id)
        if err:
            raise ValueError(err)
        kids = _siblings(db, product, prev.id)
        # 抄本必须在改之前拍：改完再拍，记下来的就是「改后的值」，
        # 撤销等于把改动又写一遍。
        snap = _snapshot([combo])
        combo.parent_id = prev.id
        combo.level = int(prev.level or 0) + 1
        combo.seq = (max(int(k.seq or 0) for k in kids) + 10) if kids else 100
    elif action == "outdent":
        parent = db.get(ProductNode, combo.parent_id) if combo.parent_id else None
        if parent is None:
            raise ValueError("它已经在最上层了，没有更外的一层可去")
        grand = db.get(ProductNode, parent.parent_id) if parent.parent_id else None
        err = validate_level(db, combo, grand.id if grand else None)
        if err:
            raise ValueError(err)
        snap = _snapshot([combo])
        combo.parent_id = grand.id if grand else None
        combo.level = (int(grand.level or 0) + 1) if grand else 0
        # 从父里退出来时紧跟在父后面，视觉上就是「原地退一格」，不会跳到队尾
        combo.seq = int(parent.seq or 0) + 1
    else:
        raise ValueError(f"不认识的移动方向：{action or '空'}")

    if not snap:
        snap = _snapshot([combo])
    combo.source = "manual"
    db.add(combo)
    db.flush()
    affected = relevel_product(db, product)
    for field, old in before.items():
        _audit(db, combo, field, old, getattr(combo, field), user, entity="node")
    db.flush()
    return _move_result(db, combo, action, before, affected=affected, snap=snap)


def _snapshot(rows: list[ProductNode]) -> list[dict]:
    """抄下这几行的原始位置，供「撤销」写回。"""
    out: list[dict] = []
    seen: set[int] = set()
    for r in rows:
        if r.id in seen:
            continue
        seen.add(r.id)
        out.append(
            {
                "id": r.id,
                "part": r.part or "",
                "parent_id": r.parent_id,
                "level": int(r.level or 0),
                "seq": r.seq,
            }
        )
    return out


def undo_move(db: Session, product: str, rows: list[dict], *, user: str = "") -> dict:
    """把一次 move 的原样退回去。

    `rows` 就是 `move_combo()` 返回的 `undo` 快照（前端原封不动传回来）。
    只改 `parent_id` 和 `seq`；层号不照抄快照，改完统一 `relevel_product()`
    重算 —— 抄快照会把「当时的下级层号」也搬回来，而子树可能已经不是当时那支了。
    """
    product = (product or "").strip()
    if not product:
        raise ValueError("产品名为空，没有可撤销的对象")
    if not rows:
        raise ValueError("没有可撤销的改动")

    all_rows = db.scalars(select(ProductNode).where(ProductNode.product == product)).all()
    by_id = {c.id: c for c in all_rows}
    todo = []
    pending: dict[int, int | None] = {}
    for it in rows:
        cid = it.get("id")
        combo = by_id.get(cid)
        if combo is None:
            raise ValueError("要撤销的那一行已经不在这个产品里了，可能被删了")
        pid = it.get("parent_id")
        if pid:
            if pid == cid:
                raise ValueError("撤销目标指向自己，数据不对，已中止")
            if pid not in by_id:
                raise ValueError("撤销目标的上级不在这个产品里，已中止")
        todo.append((combo, pid or None, it.get("seq")))
        pending[cid] = pid or None

    # 前端传回来的是普通 JSON，谁都能拼一份。整批改动互相指向时（A 挂 B、B 挂 A）
    # 单看一行都合法，合起来就是一个环 —— 必须按「改后」的全貌再过一遍。
    for cid in pending:
        seen = {cid}
        cur = pending[cid]
        while cur:
            if cur in seen:
                raise ValueError("这批撤销会绕成环，已中止（数据没改）")
            seen.add(cur)
            cur = pending.get(cur, (by_id[cur].parent_id if cur in by_id else None))

    for combo, pid, seq in todo:
        _audit(db, combo, "parent_id", combo.parent_id, pid, user or "撤销", entity="node")
        _audit(db, combo, "seq", combo.seq, seq, user or "撤销", entity="node")
        combo.parent_id = pid
        if seq is not None:
            combo.seq = int(seq)
        db.add(combo)
    db.flush()
    affected = relevel_product(db, product)
    db.flush()
    return {
        "restored": len(todo),
        "product": product,
        "affected": affected,
        "parts": [c.part or "" for c, _, _ in todo],
    }


def _move_result(
    db: Session,
    combo: ProductNode,
    action: str,
    before: dict,
    *,
    affected: int,
    snap: list[dict] | None = None,
) -> dict:
    parent = db.get(ProductNode, combo.parent_id) if combo.parent_id else None
    return {
        "action": action,
        "id": combo.id,
        "part": combo.part,
        "product": combo.product,
        "parent_id": combo.parent_id,
        "parent_part": (parent.part or "") if parent else "",
        "level": int(combo.level or 0),
        "seq": combo.seq,
        "path": level_path(db, combo),
        "level_src": combo.source,
        "affected": affected,
        "changed": {
            k: v for k, v in before.items() if str(v) != str(getattr(combo, k))
        },
        # 前端留着它做「撤销 / Ctrl+Z」，原样传回 /undo-move 即可
        "undo": snap or [],
    }


def level_tree(db: Session, product: str, part: str = "") -> dict:
    """一个产品的真实层级树，专供「改层级」弹窗点选上级。

    与 `combo_tree()` 的区别：**只认 `parent_id`**，不掺按件名前缀聚出来的
    虚拟分组 —— 那种分组不落库，拿它当上级会挂到空气上。

    每个节点带 `blocked`：自己 + 自己的全部子孙。把上级选成自己或自己的
    下级就成环，界面上要直接点不动，不能等提交了再报错。
    """
    product = (product or "").strip()
    rows = db.scalars(select(ProductNode).where(ProductNode.product == product)).all()
    by_id = {c.id: c for c in rows}
    # 结构节点本身不带状态和工艺路线 —— 那是组合（工艺身份）的事，
    # 按 (产品, 件名) 配一份映射过来；没登记组合的件在这一层显示 none
    combos = {
        (c.part or "").strip(): c
        for c in db.scalars(select(WorkCombo).where(WorkCombo.product == product)).all()
    }

    target: ProductNode | None = None
    part = (part or "").strip()
    if part:
        target = next((c for c in rows if (c.part or "").strip() == part), None)
        if target is None:
            raise ValueError("组合不存在，请先在台账里登记")

    blocked: set[int] = set()
    if target is not None:
        kids: dict[int, list[int]] = {}
        for c in rows:
            if c.parent_id:
                kids.setdefault(c.parent_id, []).append(c.id)
        stack = [target.id]
        while stack:
            cur = stack.pop()
            if cur in blocked:
                continue
            blocked.add(cur)
            stack.extend(kids.get(cur, []))

    # 悬空 parent（指向不存在的行、或指向别的产品）一律当顶层：
    # 宁可让这一支露在根上让人看见，也不要整棵树缺一块
    children: dict[int | None, list[ProductNode]] = {}
    for c in rows:
        parent = c.parent_id if (c.parent_id in by_id and c.parent_id != c.id) else None
        children.setdefault(parent, []).append(c)

    def build(c: ProductNode, depth: int) -> dict:
        sub = (
            []
            if depth >= MAX_DEPTH
            else [
                build(x, depth + 1)
                for x in sorted(
                    children.get(c.id, []), key=lambda x: (int(x.seq or 100), x.part or "")
                )
            ]
        )
        combo = combos.get((c.part or "").strip())
        return {
            "id": c.id,
            "part": c.part,
            "level": c.level,
            "parent_id": c.parent_id,
            "seq": c.seq,
            "qty_per_unit": float(c.qty_per_unit or 0.0),
            "status": combo.status if combo else "none",
            "has_route": bool(combo and combo.route_id),
            "is_self": target is not None and c.id == target.id,
            "blocked": c.id in blocked,
            "children": sub,
        }

    roots = [
        build(c, 0)
        for c in sorted(children.get(None, []), key=lambda x: (int(x.seq or 100), x.part or ""))
    ]

    me = None
    if target is not None:
        parent = by_id.get(target.parent_id) if target.parent_id else None
        combo = combos.get((target.part or "").strip())
        me = {
            "id": target.id,
            "part": target.part,
            "level": target.level,
            "parent_id": target.parent_id,
            "parent_part": (parent.part or "") if parent else "",
            "path": level_path(db, target),
            "qty_per_unit": float(target.qty_per_unit or 0.0),
            "seq": target.seq,
            "level_src": target.source,
            "status": combo.status if combo else "none",
        }

    return {
        "product": product,
        "count": len(rows),
        "self": me,
        "roots": roots,
        "blocked": sorted(blocked),
    }


# ---------------------------------------------------------------- 发布校验


def validate_combo(db: Session, combo: WorkCombo, route: ProcessRoute | None) -> list[dict]:
    """发布前的检查。block 挡发布，warn 只提示。

    刻意不检查「工序是否齐全」：路线允许是 `completeness='partial'` 的片段，
    只登记已知工序是常态，拿齐全度卡发布等于让人放弃登记。
    """
    issues: list[dict] = []

    def add(level: str, code: str, message: str) -> None:
        issues.append({"level": level, "code": code, "message": message})

    if not (combo.product or "").strip():
        add("block", "NO_PRODUCT", "产品名为空，无法归到任何产品下")
    if route is None:
        add("block", "NO_ROUTE", "还没有建工艺路线，先点「建线」")
        return issues

    steps = list(route.steps)
    if not steps:
        add("block", "NO_STEP", "路线没有工序，空路线不能发布")
        return issues

    names = [(s.process_name or s.process_code or "").strip() for s in steps]
    dup = sorted({n for n in names if n and names.count(n) > 1})
    if dup:
        add("block", "DUP_STEP", f"工序重复：{'、'.join(dup[:4])}")

    unit_sum = round(sum(float(s.unit_hours or 0) for s in steps), 4)
    if unit_sum <= 0:
        add("block", "ZERO_HOURS", "单件工时合计为 0，定额没填不能当标准")

    if any((not (s.process_name or s.process_code or "").strip()) for s in steps):
        add("block", "EMPTY_PROCESS", "有工序行没填工序名")

    if not (combo.part or "").strip():
        add("block", "NO_PART", "件名为空")

    # 与历史实测偏差过大：不挡发布（工艺科可能就是在纠偏），但必须让人看见。
    hist = [
        h
        for h in db.scalars(
            select(StandardHour).where(StandardHour.product == combo.product)
        ).all()
        if _key(h.product, h.part) == _key(combo.product, combo.part) and h.active
    ]
    hist_sum = round(sum(float(h.unit_hours or 0) for h in hist), 4)
    if hist_sum > 0 and unit_sum > 0:
        ratio = unit_sum / hist_sum
        if ratio > 1.5 or ratio < 0.5:
            add(
                "warn",
                "DEVIATE_HISTORY",
                f"定额合计 {unit_sum:g}h 与历史实测 {hist_sum:g}h 偏差 {(ratio - 1) * 100:+.0f}%，"
                "建议在备注里写清依据",
            )
    return issues


def _latest_route(db: Session, combo: WorkCombo) -> ProcessRoute | None:
    """组合当前该被发布的那一版：已发布优先，其次版本号最大的草稿。"""
    rows = db.scalars(
        select(ProcessRoute)
        .where(
            ProcessRoute.kind == "route",
            ProcessRoute.product == combo.product,
            ProcessRoute.part == combo.part,
        )
        .order_by(ProcessRoute.version.desc())
        .execution_options(populate_existing=True)
    ).all()
    for r in rows:
        if r.status == "released":
            return r
    for r in rows:
        if r.status == "draft":
            return r
    return rows[0] if rows else None


# ---------------------------------------------------------------- 发布 / 撤回


def release_combo(
    db: Session,
    combo: WorkCombo,
    *,
    by: str = "",
    note: str = "",
    force: bool = False,
) -> dict:
    """确认并发布（一步）：草稿 → 已发布，路线同时置 released。

    为什么不拆「确认」和「发布」两级：这台系统上一个月的工艺单据量还不到
    需要两人会签的规模，两级审批只会让人把两边都点一遍。字段留了
    `published_by`，真要追责追得到人；需要时会签再加状态。
    """
    if combo.status == "released":
        raise ValueError("这个组合已经是已发布状态")
    if combo.status == "archived":
        raise ValueError("这个组合已停用（归档），不能发布；请先在台账里启用")

    route = _latest_route(db, combo)
    issues = validate_combo(db, combo, route)
    blocks = [x for x in issues if x["level"] == "block"]
    if blocks and not force:
        raise ValueError("；".join(x["message"] for x in blocks))

    assert route is not None  # blocks 为空时必然有路线
    if route.status != "released":
        # 走既有的 publish()：它会归档同组合的其它已发布版、记升版通知。
        route_service.publish(db, route, published_by=by or "system", action="publish", note=note)

    combo.status = "released"
    combo.route_id = route.id
    combo.published_at = dt.datetime.now()
    combo.published_by = (by or "").strip()
    if note:
        combo.note = note[:255]
    combo.demoted_from = ""
    db.add(combo)
    db.flush()
    _audit(db, combo, "status", "draft", "released", by)
    return {
        "combo": {"product": combo.product, "part": combo.part, "status": combo.status},
        "route_id": route.id,
        "version": route.version,
        "warnings": [x for x in issues if x["level"] == "warn"],
    }


def inflight_refs(db: Session, combo: WorkCombo) -> list[dict]:
    """引用这个组合的在制任务。撤回发布前必须点名，否则它们会静默失去基准。"""
    rows = db.execute(
        select(TaskRoute, WorkTask, ProcessRoute)
        .join(WorkTask, WorkTask.id == TaskRoute.task_id)
        .join(ProcessRoute, ProcessRoute.id == TaskRoute.route_id)
        .where(
            TaskRoute.status == "active",
            TaskRoute.product == combo.product,
            TaskRoute.part == combo.part,
        )
    ).all()
    return [
        {
            "task_id": t.id,
            "task_no": t.task_no,
            "title": t.title,
            "route_id": r.id,
            "route_status": r.status,
        }
        for _tr, t, r in rows
    ]


def withdraw_combo(
    db: Session,
    combo: WorkCombo,
    *,
    by: str = "",
    reason: str = "",
    force: bool = False,
) -> dict:
    """撤回发布：已发布 → 草稿。

    与「升版」的区别：撤回会让这个组合暂时没有生效工艺，识别校验和工艺卡
    都不再取它。所以被在制任务引用时必须先列单号，再由人决定 `force`。
    """
    if combo.status != "released":
        raise ValueError("只有已发布的组合可以撤回")
    if not (reason or "").strip():
        raise ValueError("撤回必须填写原因")
    refs = inflight_refs(db, combo)
    if refs and not force:
        raise ValueError(
            f"被 {len(refs)} 个在制任务引用（{'、'.join(x['task_no'] or str(x['task_id']) for x in refs[:5])}），"
            "撤回会让它们失去核对基准；确实要撤请确认后重试"
        )

    route = db.get(ProcessRoute, combo.route_id) if combo.route_id else None
    if route is None:
        route = _latest_route(db, combo)
    if route is not None and route.status == "released":
        route.status = "draft"
        route.updated_at = dt.datetime.now()
        db.add(route)
    db.flush()

    combo.status = "draft"
    combo.route_id = None
    combo.demoted_from = "released"
    combo.note = reason[:255]
    db.add(combo)
    db.flush()
    _audit(db, combo, "status", "released", "draft", by)
    _audit(db, combo, "withdraw_reason", "", reason, by)
    return {"combo": {"product": combo.product, "part": combo.part}, "refs": len(refs)}


def archive_combo(db: Session, combo: WorkCombo, *, by: str = "", note: str = "") -> dict:
    """停用组合 = 归档。

    只翻 `StandardHour.active` 是不够的 —— 台账「状态」列读的是组合状态，
    停用后那一格纹丝不动，人看不出这条组合已经下架；更糟的是它照样留在
    `released_index()` 里：识别还能匹配、任务还能挂线、工艺卡照样印得出来。
    所以停用必须是组合层面的动作，历史工时行跟着停只是顺带的副作用。

    路线不动：已发布的那一版保持 released。停用是「下架」，不是「撤回」，
    已经挂在在制任务上的引用不该因为停用而失去基准。
    """
    before = combo.status
    if before != "archived":
        combo.status = "archived"
        if note:
            combo.note = note[:255]
        db.add(combo)
        db.flush()
        _audit(db, combo, "status", before, "archived", by)
    return {"status": combo.status, "was": before}


def restore_combo(db: Session, combo: WorkCombo, *, by: str = "") -> dict:
    """启用 = 回到停用前的那一档。

    不另存 `prev_status`：路线状态本身就是权威。`route_id` 指向的那版还是
    released，就回已发布；路线已经被撤、被归档或者根本没有，就回草稿。
    这样「停用期间有人撤了工艺」不会被启用悄悄抹掉。
    """
    if combo.status != "archived":
        return {"status": combo.status, "was": combo.status}
    route = db.get(ProcessRoute, combo.route_id) if combo.route_id else None
    back = "released" if (route is not None and route.status == "released") else "draft"
    if back != "released":
        # 路线已经不作数了，别让 route_id 继续指着它 —— 那会变成
        # 「状态是草稿、却有一条生效路线」的鬼数据
        combo.route_id = None
    combo.status = back
    db.add(combo)
    db.flush()
    _audit(db, combo, "status", "archived", back, by)
    return {"status": back, "was": "archived"}


def batch_release(
    db: Session,
    *,
    ids: list[int] | None = None,
    product: str = "",
    by: str = "",
    limit: int = 200,
    dry_run: bool = True,
) -> dict:
    """批量确认发布。默认预演，界面先看清单再落库。

    跳过而不是报错：一批里总有几条没建路线或定额为 0 的，一条条卡住
    批量就没意义了。每条跳过都带原因返回。
    """
    stmt = select(WorkCombo).where(WorkCombo.status == "draft")
    if ids:
        stmt = stmt.where(WorkCombo.id.in_(ids))
    if product:
        stmt = stmt.where(WorkCombo.product == product)
    rows = db.scalars(stmt.order_by(WorkCombo.product, WorkCombo.part)).all()

    done: list[dict] = []
    skipped: list[dict] = []
    for c in rows:
        if len(done) >= max(1, limit):
            break
        route = _latest_route(db, c)
        blocks = [x for x in validate_combo(db, c, route) if x["level"] == "block"]
        item = {"id": c.id, "product": c.product, "part": c.part}
        if blocks:
            skipped.append({**item, "reason": "；".join(x["message"] for x in blocks[:2])})
            continue
        done.append(item)
        if not dry_run:
            release_combo(db, c, by=by, note="批量发布")
    return {
        "dry_run": dry_run,
        "released": done,
        "skipped": skipped,
        "available": len(rows),
    }


# ---------------------------------------------------------------- 对外索引


def released_combos(db: Session) -> dict[tuple[str, str], WorkCombo]:
    rows = db.scalars(
        select(WorkCombo)
        .where(WorkCombo.status == "released")
        .execution_options(populate_existing=True)
    ).all()
    return {_key(c.product, c.part): c for c in rows}


def active_route(db: Session, product: str, part: str) -> ProcessRoute | None:
    """某组合当前生效的工艺路线。只认已发布组合 —— 这是「未发布不可引用」的闸。"""
    combo = db.scalar(
        select(WorkCombo).where(
            WorkCombo.product == (product or "").strip(),
            WorkCombo.part == (part or "").strip(),
            WorkCombo.status == "released",
        )
    )
    if combo is None or not combo.route_id:
        return None
    route = db.get(ProcessRoute, combo.route_id)
    if route is None or route.status != "released":
        return None
    return route


# ---------------------------------------------------------------- 树


def _lcp(names: list[str]) -> str:
    """最长公共前缀。中文按字切，件名短，直接逐字比。"""
    if not names:
        return ""
    lo, hi = min(names), max(names)
    i = 0
    while i < len(lo) and i < len(hi) and lo[i] == hi[i]:
        i += 1
    return lo[:i]


def cluster_siblings(nodes: list[dict], min_group: int = 2, max_prefix: int = 4) -> list[dict]:
    """把同一层里共享前缀的散件聚成「部件分组」，仅用于展示。

    为什么需要：件名基本停在零件级（下托辊轴、下托辊轴承座、料斗移动滑套、
    压盖10…），库里并没有「下托辊组件」这条组合。但工艺卡和识别都要能看出
    「这台机器的下托辊是由哪几个零件做的」，全平铺在根下等于没说。

    为什么不在库里造分组：造了组合总数就从 889 变成 895，从此台账对不上账，
    而且这些「部件」永远不会有自己的工序和工时。所以分组是纯展示结构
    （`virtual=True`），不落库、不改 parent_id、不计入组合数。

    认领顺序从长前缀到短前缀：先认出「下托辊」（3 字），才不会让「下托」
    这样的两字前缀把本该分散的东西糊成一坨。同名件名（如「料仓」与
    「料仓水套」并存）直接拿那个真实件名当分组头，不额外造节点。
    """
    remaining = list(nodes)
    groups: list[dict] = []
    for depth in range(max_prefix, 1, -1):
        buckets: dict[str, list[dict]] = {}
        for n in remaining:
            part = n.get("part") or ""
            if len(part) > depth:
                buckets.setdefault(part[:depth], []).append(n)
        used: set[int] = set()
        for prefix, members in sorted(buckets.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            members = [m for m in members if id(m) not in used]
            if len(members) < min_group:
                continue
            # 组名取组内最长公共前缀：比固定 2~4 字更贴人话
            # （「上拉杆 + 上拉杆端盖」应该叫「上拉杆」，不是「上拉」）
            name = _lcp([m.get("part") or "" for m in members])
            if len(name) < 2:
                name = prefix
            head = next((m for m in members if (m.get("part") or "") == name), None)
            if head is not None:
                others = [m for m in members if m is not head]
                if not others:
                    continue
                head["children"] = list(head.get("children") or []) + sorted(
                    others, key=lambda m: m.get("part") or ""
                )
                head["group"] = True
                head["group_size"] = len(members)
                # 必须把它放回结果集：head 已经从 remaining 里移除，
                # 这里不 append 就会整个节点连同它的子件一起消失（曾经丢过 4 个件名）
                groups.append(head)
            else:
                groups.append(
                    {
                        "id": None,
                        "virtual": True,
                        "part": name,
                        "level": 1,
                        "status": "",
                        "qty_per_unit": 1,
                        "seq": 0,
                        "unit_sum": round(
                            sum(float(m.get("unit_sum") or 0) for m in members), 4
                        ),
                        "step_count": sum(int(m.get("step_count") or 0) for m in members),
                        "group": True,
                        "group_size": len(members),
                        "children": sorted(members, key=lambda m: m.get("part") or ""),
                    }
                )
            used.update(id(m) for m in members)
        remaining = [n for n in remaining if id(n) not in used]
    remaining.sort(key=lambda n: (n.get("level") or 1, n.get("seq") or 0, n.get("part") or ""))
    groups.sort(key=lambda g: (g["part"] or ""))
    return groups + remaining


def _node(row: ProductNode, combo: WorkCombo | None, stats: dict) -> dict:
    """结构节点 + 挂在这件上的工艺（可能没有）。

    `combo is None` 不是异常：结构里可以有「还没编工艺的件」。以前不行 ——
    层级字段长在工时组合行上，没有组合就没有结构。这正是拆表要解决的问题。
    """
    st = stats.get(row.id, {})
    return {
        "id": row.id,
        "product": row.product,
        "part": row.part,
        "level": row.level,
        "status": combo.status if combo else "none",
        "level_src": row.source,
        "parent_id": row.parent_id,
        "qty_per_unit": round(float(row.qty_per_unit or 1), 4),
        # 计划生产数量：结构树上要能直接看到「这一批做几件」，
        # 并且分清哪些是派生来的、哪些是人定的
        "plan_qty": round(float(row.plan_qty or 0), 4),
        "plan_src": row.plan_src or "auto",
        "seq": row.seq,
        "route_id": combo.route_id if combo else None,
        "version": st.get("version"),
        "step_count": st.get("steps", 0),
        "unit_sum": st.get("unit_sum", 0.0),
        "samples": st.get("samples", 0),
        "demoted_from": combo.demoted_from if combo else "",
        "note": combo.note if combo else "",
        # 前端据此区分「有工艺但没编」和「压根没登记组合」
        "has_combo": combo is not None,
        "children": [],
    }


def combo_tree(
    db: Session,
    *,
    product: str = "",
    status: str = "",
    q: str = "",
    include_children: bool = True,
    cluster: bool = True,
) -> dict:
    """产品分类 → 部件层级 的树。组合台账与工艺编排左栏共用。

    **结构读 `product_node`，工艺读 `work_combo`。** 两张表以 (产品, 件名)
    关联：结构决定「谁挂在谁下面」，工艺决定「这件怎么做」。结构里有、工艺
    里没有的件照样出现，只是状态显示「无工艺」。

    没有「整机装配」这一层的产品不造假根，直接把最上面那批件名当根
    （`virtual_root=True`），前端据此少画一层 —— 造一条假的组合记录
    会让组合总数 889 变 890，从此说不清。

    `cluster=False` 时不掺按件名前缀聚出来的虚拟分组，只留真实的
    `parent_id` 结构 —— 调整层级的界面必须用这一份：虚拟分组不落库，
    在它上面按键或拖拽都没有可写的目标。
    """
    rows = db.scalars(
        select(ProductNode).order_by(
            ProductNode.product, ProductNode.level, ProductNode.seq, ProductNode.part
        )
    ).all()
    combos = {(c.product or "", c.part or ""): c for c in db.scalars(select(WorkCombo)).all()}

    def combo_of(row: ProductNode) -> WorkCombo | None:
        return combos.get((row.product or "", row.part or ""))

    if product:
        rows = [n for n in rows if n.product == product]
    if status:
        rows = [n for n in rows if combo_of(n) is not None and combo_of(n).status == status]
    if q.strip():
        kw = q.strip()
        rows = [n for n in rows if kw in (n.product or "") or kw in (n.part or "")]

    # 每个件「该显示的那一版」：已发布优先，没有就用版本号最大的草稿。
    # 升序遍历、后写覆盖，遇到 released 就锁住不再被更高版草稿顶掉。
    anchor: dict[tuple[str, str], ProcessRoute] = {}
    for r in db.scalars(
        select(ProcessRoute)
        .where(ProcessRoute.kind == "route", ProcessRoute.status.in_(("released", "draft")))
        .order_by(ProcessRoute.version.asc())
    ).all():
        k = _key(r.product, r.part)
        cur = anchor.get(k)
        if cur is None or cur.status != "released":
            anchor[k] = r

    # 工序数与工时合计一次聚合查完，逐条 lazy load 会把树拖成 N+1
    route_stats: dict[int, dict] = {}
    route_ids = [r.id for r in anchor.values()]
    if route_ids:
        for rid, cnt, usum in db.execute(
            select(
                ProcessRouteStep.route_id,
                func.count(ProcessRouteStep.id),
                func.coalesce(func.sum(ProcessRouteStep.unit_hours), 0.0),
            )
            .where(ProcessRouteStep.route_id.in_(route_ids))
            .group_by(ProcessRouteStep.route_id)
        ).all():
            route_stats[rid] = {"steps": int(cnt or 0), "unit_sum": round(float(usum or 0.0), 4)}

    stats: dict[int, dict] = {}
    for n in rows:
        c = combo_of(n)
        r = anchor.get(_key(n.product, n.part)) if c is not None else None
        if r is None:
            stats[n.id] = {"version": None, "steps": 0, "unit_sum": 0.0, "samples": 0}
        else:
            st = route_stats.get(r.id, {})
            stats[n.id] = {
                "version": r.version,
                "steps": st.get("steps", 0),
                "unit_sum": st.get("unit_sum", 0.0),
                "samples": 0,
            }

    by_id = {n.id: n for n in rows}
    nodes = {n.id: _node(n, combo_of(n), stats) for n in rows}

    # 「按当前上级本该做几件」。人工设定过的件可能与它对不上，
    # 界面据此标黄 —— 需求不足比多做的危害大得多（缺件停线）。
    for n in rows:
        parent = by_id.get(n.parent_id) if n.parent_id else None
        nd = nodes[n.id]
        if parent is None:
            nd["plan_expect"] = nd["plan_qty"]
        else:
            nd["plan_expect"] = round(
                float(parent.plan_qty or 0) * float(n.qty_per_unit or 1), 4
            )

    # 父节点在筛选结果之外时，这个节点在当前视图里就是顶层
    orphan: set[int] = set()
    for n in rows:
        parent = by_id.get(n.parent_id) if n.parent_id else None
        if parent is not None:
            nodes[parent.id]["children"].append(nodes[n.id])
        else:
            orphan.add(n.id)

    products: dict[str, dict] = {}
    for n in rows:
        c = combo_of(n)
        p = products.setdefault(
            n.product,
            {
                "product": n.product,
                "product_id": c.product_id if c else None,
                "total": 0,
                "released": 0,
                "draft": 0,
                "value": 0.0,
                "roots": [],
                "virtual_root": False,
            },
        )
        p["total"] += 1
        if c is not None and c.status == "released":
            p["released"] += 1
        elif c is not None and c.status == "draft":
            p["draft"] += 1
        p["value"] += float(stats.get(n.id, {}).get("unit_sum") or 0.0)

    for name, p in products.items():
        same = [n for n in rows if n.product == name and n.id in orphan]
        roots = [nodes[n.id] for n in same if (n.level or 0) == 0]
        if roots:
            p["roots"] = roots
            p["virtual_root"] = False
        else:
            # 没有「整机装配」这类件名的产品（114 个产品里绝大多数如此）：
            # 把产品节点本身当根。不造一条假的组合记录——那会让 889 变 890，
            # 台账从此对不上账；虚拟根只存在于这棵树上。
            tops = sorted(
                (nodes[n.id] for n in same),
                key=lambda x: (x["level"], x["seq"], x["part"]),
            )
            p["roots"] = [
                {
                    "id": None,
                    "virtual": True,
                    "virtual_root": True,
                    "part": name or "(未指定产品)",
                    "level": 0,
                    "status": "",
                    "qty_per_unit": 1,
                    "seq": 0,
                    "unit_sum": 0.0,
                    "step_count": 0,
                    "children": tops,
                }
            ]
            p["virtual_root"] = True

    items = sorted(products.values(), key=lambda x: (-x["value"], x["product"]))
    for p in items:
        p["value"] = round(p["value"], 2)

    # 根下散件按前缀聚成部件分组。只处理没有子节点的叶子 ——
    # 已经有真实子件的部件（料仓组件 → 料仓水套）本身就是层级，不该被再聚一次。
    # structure 视图（调层级用）跳过这一步：虚拟分组没有可写的上级。
    if cluster:
        for p in items:
            for root in p["roots"]:
                kids = root.get("children") or []
                leaves = [k for k in kids if not k.get("children")]
                fixed = [k for k in kids if k.get("children")]
                if leaves:
                    root["children"] = fixed + cluster_siblings(leaves)

    # 根节点的工时也要能算「含子件」：递归汇总，工艺卡与覆盖看板都用这一份
    def total_hours(node: dict) -> float:
        kids = node.get("children") or []
        own = float(node.get("unit_sum") or 0.0)
        # 虚拟分组自己没工序，工时全在子件上；真实部件则是「本层 + 子件」
        return round(own + sum(total_hours(ch) for ch in kids), 4)

    def walk(node: dict) -> None:
        node["subtotal_hours"] = total_hours(node)
        for ch in node.get("children") or []:
            walk(ch)

    for p in items:
        for n in p["roots"]:
            walk(n)

    return {
        "products": items,
        "summary": {
            "products": len(items),
            "combos": len(rows),
            "released": sum(1 for n in rows if (combo_of(n) is not None and combo_of(n).status == "released")),
            "draft": sum(1 for n in rows if (combo_of(n) is not None and combo_of(n).status == "draft")),
            "no_route": sum(1 for n in rows if not stats.get(n.id, {}).get("version")),
            "deep": sum(1 for n in rows if (n.level or 0) >= 2),
        },
    }
