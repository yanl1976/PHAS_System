"""重建一套「结构完整」的样板数据：10 个产品 × 完整结构 × 全量工艺 × 10 张生产任务。

用法：
    .venv/Scripts/python.exe scripts/seed_demo_10_products.py            # 预览（不写库）
    .venv/Scripts/python.exe scripts/seed_demo_10_products.py --apply    # 落库

**这是破坏性操作**。它会把现有 114 个产品的工艺域数据（结构 / 组合 / 路线 / 任务）
全部清掉，只保留这 10 个产品的一套完整数据，并顺手把没人用的字典行清掉。
跑之前请确认 `data/worktime.db` 已备份。

为什么可以这么做：库里现有 889 条组合里只有 88 条路线、11 张卡 —— 绝大多数件
「登记了却没工艺」，工艺域是半成品；而这些件的**实测工时**（`standard_hour`）
反而是齐的（10 个产品 100% 覆盖到件）。所以正确的做法不是编数据，而是
**把已有的实测工时补成完整工艺**：结构按件名语义补层级，工艺按实测工时补路线。

工时来源分两种，都在 `process_route.source` 上留了印记：
  * `history`  —— 来自 `standard_hour` 的实测工时（10 个产品全部件都命中）
  * `derived`  —— 历史账里没有总装记录的产品，整机总装按件工时合计推算
                 （总装 10% / 试机 3% / 检验 2%），仅在 9 个缺总装数据的产品上使用

保留不动的东西：OCR 识别那一半（image / sheet / work_item / batch）、人员与工序字典、
核算期。识别是另一条业务线，那些单据是真扫进来的，删了就回不来了。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import delete, select, update  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    PartDict,
    Period,
    ProcessRoute,
    ProcessRouteStep,
    ProductDict,
    ProductNode,
    RouteVersionNotice,
    Sheet,
    StandardHour,
    TaskRevision,
    TaskRoute,
    WorkCombo,
    WorkTask,
)
from app.services import combo_registry as CR  # noqa: E402
from app.services import process_card as PC  # noqa: E402
from app.services import routes as RS  # noqa: E402
from app.services import tasks as TS  # noqa: E402

ROOT = "整机装配"
TAIL = "标准件与杂项"

# 10 个产品：都用库里真实存在、且实测工时覆盖率 100% 的产品，
# 这样结构里的件名和工时都是真的，不需要编。
PRODUCTS: list[tuple[str, str, str]] = [
    # (产品名, 规格, 归口)
    ("YUJ-18BZ", "鱼式制丸机 18BZ", "制丸机"),
    ("YUJ-17BZ制丸机", "鱼式制丸机 17BZ", "制丸机"),
    ("拨条制丸机", "拨条制丸机", "制丸机"),
    ("SJ-2栓剂", "栓剂灌装联动线 2", "栓剂机"),
    ("SJ-3栓剂", "栓剂灌装联动线 3", "栓剂机"),
    ("SJ-7栓剂", "栓剂灌装联动线 7", "栓剂机"),
    ("SJ-15栓剂", "栓剂灌装联动线 15L", "栓剂机"),
    ("滚筒干燥90", "滚筒干燥机 90", "干燥机"),
    ("带式干燥", "带式干燥机", "干燥机"),
    ("提升机", "物料提升机", "辅机"),
]

# 部件分桶。**顺序即优先级**，四轮调下来的结论：
#   电气 → 工作件 → 灌装封口 → 气液 → 传动 → 机架钣金 → 杂项
# 关键在两条：①「工作件」（刀具/料仓/咀/溜槽）必须最前，否则「刀轮」被「轮」
# 带进传动、「料仓」被「仓」带进钣金；②「气液」必须排在「传动」前，
# 否则「风机叶轮轴」会因为「轴」字被算成传动件，而它其实是风机组的。
BUCKETS: list[tuple[str, str]] = [
    ("电气部件", r"电柜|电控|接线|光电|防爆|电机|铭牌|走线|下线|配盘|传感器|电晕"),
    (
        "工作部件",
        r"料仓|料包|料斗|料筒|料槽|出条|刀具|道具|刀轮|条轮|钢咀|细嘴|四氟|滴管|顺条|"
        r"推进器|翻板|翻版|收尾|喇叭口|搅拌|桨|挡手架|溜槽|壶|咀|嘴|槽|斗",
    ),
    (
        "灌装封口部件",
        r"冷却|灌装|封口|封尾|成型|剪粒|切刀|药轮|挡药|拨盘|拨药|拔药|柱塞|活塞|泵|"
        r"注药|注液|导膜|送膜|撑膜|膜盘|吹气|加热块|预热",
    ),
    ("气液管路部件", r"气缸|气控|气阀|风门|风机|风|管|接头|水路|密封|堵头|阀门|弹簧|盲板"),
    (
        "传动部件",
        r"轴|齿轮|链轮|联轴|连轴|法兰|铜套|压盖|滑套|挡套|轴承|涨紧|张紧|刹车|"
        r"丝杆|螺杆|调节杆|手柄|帽|轮|套筒",
    ),
    (
        "机架钣金部件",
        r"机架|车架|架|包板|板|门|盖|罩|槽钢|角钢|方管|钢|钣金|底板|衬板|托盘|"
        r"焊接|台|箱|杆|撑|立柱|柱|盒|小车|地脚|面|座|筒",
    ),
]

BUCKET_ORDER = [b for b, _ in BUCKETS] + [TAIL]

# 手工指定「这一件装在那一件下面」（形成三级：整机 → 部件 → 零件 → 子件）。
# 规则推不出来的才写在这里：编号变体（轴2 / 条轮3）由下面的 _base_of 自动挂。
CHILD_OVERRIDES: dict[tuple[str, str], str] = {
    ("YUJ-18BZ", "刀具Ф5.0"): "刀具",
    ("YUJ-18BZ", "刀具去刺打花"): "刀具",
    ("YUJ-17BZ制丸机", "机架用料"): "机架",
    ("SJ-2栓剂", "圆点板1"): "挡药板轴",
    ("SJ-7栓剂", "原点板1"): "挡药板轴B",
    ("滚筒干燥90", "下托辊轴管"): "下托辊轴",
    ("滚筒干燥90", "下托辊轴轴头"): "下托辊轴",
    ("滚筒干燥90", "下托辊轴承座端盖"): "下托辊轴承座",
    ("滚筒干燥90", "筒体辊轴轴头"): "筒体托辊轴组焊",
}

# 每台用量。默认 1；标准件、多咀件按常识给值，别让整机用量全是 1。
QTY_RULES: list[tuple[str, float]] = [
    (r"刀具Ф5\.0", 8.0),
    (r"钢咀|细嘴|四氟咀|四氟套|滴管", 4.0),
    (r"螺母|螺栓|销|垫|隔套|挡套|密封|堵头|涨紧块|张紧块|键", 2.0),
]

# 10 个生产任务
TASKS: list[tuple[str, str, str, int, str]] = [
    # (产品, 客户, 标题后缀, 台数, 下单日)
    ("YUJ-18BZ", "河北凯诺制药", "11月首批", 12, "2026-11-02"),
    ("YUJ-17BZ制丸机", "江西杏林白马", "扩产订单", 8, "2026-11-03"),
    ("拨条制丸机", "山东新华制药", "换型试制", 5, "2026-11-04"),
    ("SJ-2栓剂", "太极集团涪陵", "栓剂线二期", 6, "2026-11-05"),
    ("SJ-3栓剂", "华润三九", "补充订单", 4, "2026-11-06"),
    ("SJ-7栓剂", "云南白药", "新线投产", 10, "2026-11-07"),
    ("SJ-15栓剂", "广州白云山", "15L 机型", 3, "2026-11-09"),
    ("滚筒干燥90", "四川蜀中制药", "干燥工段", 6, "2026-11-10"),
    ("带式干燥", "江苏康缘药业", "带干替换", 5, "2026-11-11"),
    ("提升机", "浙江康恩贝", "配套辅机", 9, "2026-11-12"),
]

ACTOR = "seed"
NEW_PERIOD = ("2026-11", "2026年11月")


def qty_of(part: str) -> float:
    for pat, q in QTY_RULES:
        if re.search(pat, part):
            return q
    return 1.0


def bucket_of(part: str) -> str:
    for name, pat in BUCKETS:
        if re.search(pat, part):
            return name
    return TAIL


def _base_of(part: str, pool: set[str]) -> str | None:
    """编号变体归到本体：「条轮3」→「条轮」，「轴套2」→「轴套1」。

    只在本体**真的存在于同一产品**时才认，否则「轴2」会挂到一个不存在的「轴」上。
    两轮查找：先去掉末尾数字找本体；找不到再试「本体 + 1」——
    因为库里很多编号是从 1 起排的（轴套1/轴套2、隔套1/隔套2/隔套3），
    没有裸「轴套」这一件，得挂到「轴套1」下。
    """
    if not part or not part[-1].isdigit():
        return None
    stem = part[:-1]
    if stem and stem != part and stem in pool:
        return stem
    sibling = stem + "1"
    if sibling != part and sibling in pool:
        return sibling
    return None


def build_tree(product: str, parts: list[str]) -> dict:
    """把平铺件名组织成「整机 → 部件 → 零件 → 子件」的四层树。

    返回嵌套 dict：`{件名: {子件名: {...}}}`，顶层键恒为 `整机装配`。
    """
    pool = set(parts)
    # 1) 决定每件的父名：手工指定 > 编号变体归本体 > 归到它所属的部件桶
    parent: dict[str, str] = {}
    for p in parts:
        ov = CHILD_OVERRIDES.get((product, p))
        if ov and ov in pool:
            parent[p] = ov
            continue
        base = _base_of(p, pool)
        if base:
            parent[p] = base
            continue
        parent[p] = bucket_of(p)

    children_of: dict[str, list[str]] = defaultdict(list)
    for p in parts:
        children_of[parent[p]].append(p)

    # 2) 部件桶本身也是节点，挂在整机下。桶名是「电气部件」这种复合词，
    #    与真实件名不会撞，所以不必做同名合并。
    buckets = [b for b in BUCKET_ORDER if children_of.get(b)]
    loose = sorted(k for k in children_of.get(ROOT, []))
    children_of[ROOT] = buckets + loose
    for b in buckets:
        children_of[b] = sorted(children_of[b])

    def attach(name: str, seen: frozenset[str]) -> dict:
        out: dict[str, dict] = {}
        for k in children_of.get(name, []):
            if k in seen:
                continue
            out[k] = attach(k, seen | {k})
        return out

    return {ROOT: attach(ROOT, frozenset({ROOT}))}


def flatten_tree(tree: dict, depth: int = 0, parent: str | None = None) -> list[tuple]:
    """树 → 行清单：(件名, 层号, 父件名)。顺带按层给 seq。"""
    rows: list[tuple] = []
    for i, (name, kids) in enumerate(tree.items(), start=1):
        rows.append((name, depth, parent, i * 10))
        rows.extend(flatten_tree(kids, depth + 1, name))
    return rows


def preview(db) -> dict:
    """不写库，只把要建的东西算出来给人看。

    「有哪些件」一律以 **standard_hour**（历史实测工时）为准，不看 `product_node`。
    这样脚本可以反复跑：第二次跑时库里已经有上次造的部件桶（传动部件…），
    从 product_node 读就会把桶名也当成真实件，结果桶套桶、唯一约束直接撞车。
    """
    plan: dict[str, list[tuple]] = {}
    for product, _spec, _family in PRODUCTS:
        parts = {
            (h.part or "").strip()
            for h in db.scalars(
                select(StandardHour).where(
                    StandardHour.product == product, StandardHour.active
                )
            ).all()
        }
        parts = {
            p for p in parts if p and p != ROOT and p not in BUCKET_ORDER and p != TAIL
        }
        tree = build_tree(product, sorted(parts))
        plan[product] = flatten_tree(tree)
    return plan


def show(plan: dict) -> None:
    total = 0
    for product, rows in plan.items():
        total += len(rows)
        depth = defaultdict(int)
        for _n, lv, _p, _s in rows:
            depth[lv] += 1
        lv_txt = " / ".join(f"L{k}:{v}" for k, v in sorted(depth.items()))
        print(f"\n=== {product}  {len(rows)} 件  ({lv_txt})")
        for name, lv, parent, _seq in rows[:200]:
            if lv == 0:
                print(f"  {name}")
            elif lv == 1:
                print(f"    ├ {name}")
            else:
                pad = "    " + "│  " * (lv - 1)
                print(f"{pad}  - {name}" + (f"   ← {parent}" if lv >= 2 else ""))
    print(f"\n合计 {total} 个结构节点 / {len(plan)} 个产品")


def wipe(db) -> dict:
    """清掉工艺域的全部旧数据。识别域（单据/图片/明细）不动。"""
    stats = {}
    stats["sheet_untied"] = db.execute(
        update(Sheet).where(Sheet.task_id.isnot(None)).values(task_id=None)
    ).rowcount
    for model, label in (
        (ProcessRouteStep, "route_step"),
        (TaskRoute, "task_route"),
        (TaskRevision, "task_revision"),
        (RouteVersionNotice, "route_notice"),
        (ProcessRoute, "route"),
        (WorkTask, "task"),
        (WorkCombo, "combo"),
        (ProductNode, "node"),
    ):
        stats[label] = db.execute(delete(model)).rowcount
    stats["audit"] = db.execute(
        delete(AuditLog).where(AuditLog.entity.in_(("combo", "node")))
    ).rowcount
    db.flush()
    return stats


def ensure_products(db) -> dict[str, int]:
    """补齐 10 个产品的主数据，返回 {产品名: product_id}。

    库里原本只有 9 个（`拨条制丸机` 从没进过产品字典）。缺产品主数据不是小事：
    组合、路线、任务上都挂着 `product_id` 外键，为空的话「按产品统管」这类
    靠 id 聚合的视图会漏掉整个产品 —— 文本写法有 6 种变体，按文本分组必裂。
    """
    out: dict[str, int] = {}
    for name, spec, family in PRODUCTS:
        row = db.scalar(select(ProductDict).where(ProductDict.name == name))
        if row is None:
            row = ProductDict(
                name=name,
                code=name,
                aliases="",
                spec=spec,
                active=True,
                sort_order=0,
            )
            db.add(row)
            db.flush()
        out[name] = row.id
    return out


def seed_product(db, product: str, rows: list[tuple], product_id: int | None) -> dict:
    """建一个产品的：结构节点 → 组合 → 路线（实测工时）→ 发布。"""
    # 1) 结构
    node_by_name: dict[str, ProductNode] = {}
    for name, level, parent_name, seq in rows:
        node = ProductNode(
            product=product,
            part=name,
            level=level,
            qty_per_unit=qty_of(name),
            seq=seq,
            source="manual",
            note="seed",
        )
        db.add(node)
        db.flush()
        node_by_name[name] = node
    for name, level, parent_name, seq in rows:
        if parent_name:
            node_by_name[name].parent_id = node_by_name[parent_name].id
        db.add(node_by_name[name])
    db.flush()

    # 2) 工时底数：真实件查 standard_hour；部件桶不是真实件，按它下面的件合计
    own: dict[str, float] = {}
    for name, _l, _p, _s in rows:
        own[name] = sum(
            float(h.unit_hours or 0)
            for h in db.scalars(
                select(StandardHour).where(
                    StandardHour.product == product, StandardHour.part == name
                )
            ).all()
            if h.active
        )
    parent_of = {name: p for name, _l, p, _s in rows if p}
    subtree = dict(own)

    def roll(name: str, seen: frozenset[str] = frozenset()) -> float:
        if name in seen:
            return 0.0
        kids = [k for k, pa in parent_of.items() if pa == name]
        total = own.get(name, 0.0)
        for k in kids:
            total += roll(k, seen | {name})
        subtree[name] = total
        return total

    roll(ROOT)

    # 3) 组合 + 4) 路线 + 5) 发布
    released = 0
    derived: list[str] = []
    for name, level, parent_name, _seq in rows:
        combo = WorkCombo(
            product=product,
            part=name,
            product_id=product_id,
            status="draft",
            created_by=ACTOR,
            note="seed",
        )
        db.add(combo)
        db.flush()

        steps = RS.suggest_steps(db, product, name)
        source = "history"
        if not steps:
            # 整机、部件桶这类「装配单元」在历史账里没有工时记录，
            # 按它下面全部件的工时合计推算一道装配 + 一道检验
            steps = _derived_steps(product, name, subtree.get(name, 0.0), level)
            source = "derived"
            derived.append(name)

        route = RS.create_draft(
            db,
            product,
            name,
            kind="route",
            steps=steps,
            source=source,
            created_by=ACTOR,
            remark="seed" if source == "history" else "seed：历史账无该件工时，按下级合计推算",
        )
        CR.release_combo(db, combo, by=ACTOR, note="seed")
        released += 1
        assert combo.route_id == route.id
    db.flush()
    return {"parts": len(rows), "released": released, "derived": derived}


def _derived_steps(product: str, part: str, total: float, level: int) -> list[dict]:
    """历史账里没有这个装配单元的工时，按下级件合计推算。

    * 整机（第 0 层）：总装 10% / 试机 3% / 检验 2%
    * 部件（第 1 层）：装配 6% / 检验 1.5%

    系数是经验值：一台整机的总装工时通常相当于一个大件的量级，而部件的装配
    工时只是它下面全部零件的一小部分 —— 这样「含子件工时」的比例才不会失真。
    推算出来的路线 `source='derived'` 且每道工序 remark 写明了依据，
    日后有了真实总装/装配单据，按件名重铺即可覆盖。
    """
    if total <= 0:
        raise RuntimeError(f"{product} / {part} 下级也没有工时，无法推算")
    plan = (
        [("总装", 0.10), ("试机", 0.03), ("检验", 0.02)]
        if level == 0
        else [("装配", 0.06), ("检验", 0.015)]
    )
    return [
        {
            "seq": (i + 1) * 10,
            "process_code": name,
            "process_name": name,
            "op_type": "self",
            "category": "",
            "work_center": "",
            "setup_hours": 0.0,
            "unit_hours": round(total * ratio, 2),
            "hours_src": "derived",
            "samples": 0,
            "is_key": False,
            "inspect_required": False,
            "sop": "",
            "remark": f"按下级件工时合计 {total:g}h 的 {ratio:.0%} 推算",
            "order_source": "process_seq",
            "not_in_dict": False,
        }
        for i, (name, ratio) in enumerate(plan)
    ]


def seed_tasks(db, period_id: int, prod_ids: dict[str, int]) -> list[dict]:
    """10 个生产任务，每个挂上本产品全部已发布路线。"""
    out = []
    for i, (product, customer, suffix, qty, order_date) in enumerate(TASKS, start=1):
        code = product if product.isascii() else f"P{i:02d}"
        task = WorkTask(
            task_no=f"{code}-26-{i}-202611",
            title=f"{product} {suffix}",
            customer=customer,
            product=product,
            product_id=prod_ids.get(product),
            period_id=period_id,
            order_date=order_date,
            due_date="2026-12-20",
            qty=float(qty),
            status="open",
            source="manual",
            remark="seed",
            created_by=ACTOR,
        )
        db.add(task)
        db.flush()

        combos = db.scalars(
            select(WorkCombo).where(
                WorkCombo.product == product, WorkCombo.status == "released"
            )
        ).all()
        specs = [{"product": product, "part": c.part} for c in combos]
        res = TS.add_routes(db, task, specs, created_by=ACTOR, create_baseline=False)
        db.flush()
        out.append(
            {
                "task_no": task.task_no,
                "product": product,
                "qty": qty,
                "added": res.get("added", 0),
                "failed": len(res.get("failed") or []),
            }
        )
    return out


def relink_sheets(db) -> dict:
    """把识别出来的单据挂回新任务上。

    清库时为了不撞外键，先把 `sheet.task_id` 全置空了。但单据是真扫进来的历史记录，
    长期悬空的话任务页永远显示「无实报」、月度汇总也拿不到「已录单据数」。
    所以按**产品名精确匹配**重新挂回去：一张单里的产品名全部一致、且这个产品
    在 10 个任务里，才挂。

    不做模糊匹配（比如把「制丸机」挂到「YUJ-17BZ制丸机」）：那是猜，猜错了
    等于把 A 产品的工时记到 B 的订单上，比不挂更糟。剩下的单保持未关联，
    界面上就是「未关联任务」，看得见也能随时人工挂。
    """
    by_product = {t.product: t for t in db.scalars(select(WorkTask)).all()}
    linked, skipped = 0, []
    for sh in db.scalars(select(Sheet)).all():
        prods = {i.product_name for i in sh.items if (i.product_name or "").strip()}
        if len(prods) != 1:
            skipped.append((sh.id, "产品名不唯一或为空"))
            continue
        name = prods.pop().strip()
        task = by_product.get(name)
        if task is None:
            skipped.append((sh.id, f"产品「{name}」不在本次重建的 10 个产品里"))
            continue
        sh.task_id = task.id
        db.add(sh)
        linked += 1
    db.flush()
    # 单据挂上了，任务的「单据数 / 实报工时」这些物化字段必须跟着重算，
    # 否则任务页显示的实报工时是 0，与单据台账对不上
    for task in by_product.values():
        TS.refresh_task(db, task)
    db.flush()
    return {"linked": linked, "skipped": skipped}


def prune_dicts(db, keep_products: set[str], keep_parts: set[str]) -> dict:
    """清掉没人用的基础字典行。识别域要用的工序字典不动。"""
    stats = {}
    stats["product_dict"] = db.execute(
        delete(ProductDict).where(ProductDict.name.notin_(list(keep_products)))
    ).rowcount
    stats["part_dict"] = db.execute(
        delete(PartDict).where(PartDict.name.notin_(list(keep_parts)))
    ).rowcount
    # 标准工时只留「产品在这 10 个里、件名也确实在结构里」的行
    stale = [
        h.id
        for h in db.scalars(select(StandardHour)).all()
        if (h.product or "") not in keep_products or (h.part or "") not in keep_parts
    ]
    if stale:
        db.execute(delete(StandardHour).where(StandardHour.id.in_(stale)))
    stats["standard_hour"] = len(stale)
    db.flush()
    return stats


def ensure_period(db) -> int:
    ym, title = NEW_PERIOD
    p = db.scalar(select(Period).where(Period.ym == ym))
    if p is None:
        p = Period(ym=ym, title=title, status="open")
        db.add(p)
        db.flush()
    return p.id


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只预览）")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        plan = preview(db)
        show(plan)
        if not args.apply:
            print("\n[预览模式] 没有写库。确认无误后加 --apply。")
            return 0

        print("\n---- 开始重建 ----")
        print("清旧数据：", wipe(db))
        db.commit()

        prod_ids = ensure_products(db)
        db.commit()

        print("\n结构 + 工艺：")
        for product, rows in plan.items():
            res = seed_product(db, product, rows, prod_ids.get(product))
            db.commit()
            note = f"（总装/装配推算 {len(res['derived'])} 件）" if res["derived"] else ""
            print(f"  {product:16s} {res['parts']:3d} 件 → 已发布 {res['released']} 条{note}")

        period_id = ensure_period(db)
        db.commit()

        print("\n生产任务：")
        tasks = seed_tasks(db, period_id, prod_ids)
        db.commit()
        for t in tasks:
            print(
                f"  {t['task_no']:24s} {t['product']:16s} {t['qty']:>4.0f} 台  "
                f"挂线 {t['added']:3d} 条  失败 {t['failed']}"
            )

        keep_parts = {p for rows in plan.values() for p, _l, _pa, _s in rows}
        print("\n清字典：", prune_dicts(db, {p for p, _s, _f in PRODUCTS}, keep_parts))
        db.commit()

        print("\n单据挂回任务：")
        rel = relink_sheets(db)
        db.commit()
        print(f"  挂上 {rel['linked']} 张；未关联 {len(rel['skipped'])} 张")
        for sid, why in rel["skipped"]:
            print(f"    单{sid}：{why}")

        # 标准工时也要绑产品外键，否则「按产品对照定额」同样会漏
        fixed = 0
        for h in db.scalars(
            select(StandardHour).where(StandardHour.product_id.is_(None))
        ).all():
            pid = prod_ids.get(h.product or "")
            if pid:
                h.product_id = pid
                db.add(h)
                fixed += 1
        db.commit()
        print(f"标准工时补产品外键：{fixed} 行")

        # 单据上的告警是按「当时的路线」算出来的物化值。工艺全换了一遍，
        # 不重算的话库里就是「旧告警 + 新路线」的自相矛盾状态：
        # 界面看着有告警，其实早就不该报；而且一旦有人点了校验，这些值
        # 会集体跳变一次，看起来像系统出问题。所以这里统一重算一遍。
        from app.api.sheets import _revalidate

        changed = 0
        for sh in db.scalars(select(Sheet)).all():
            before_flags = sum(len(i.flags_json or []) for i in sh.items)
            _revalidate(db, sh)
            if sum(len(i.flags_json or []) for i in sh.items) != before_flags:
                changed += 1
        db.commit()
        print(f"单据告警重校验：{changed} 张单的告警数变化")

        print("\n工艺卡核对：")
        bad = 0
        for product, _s, _f in PRODUCTS:
            cards = PC.build_cards(db, product=product)
            codes = [c["code"] for c in cards["cards"]]
            dup = len(codes) != len(set(codes))
            ok = len(cards["cards"]) == len(plan[product]) and not dup
            bad += 0 if ok else 1
            print(
                f"  {product:16s} 卡 {len(cards['cards']):3d} 张 / 结构 {len(plan[product]):3d} 件  "
                f"页 {cards['summary']['pages']:3d}  编号重复 {'有' if dup else '无'}  "
                f"{'OK' if ok else '!! 不一致'}"
            )
        print(f"\n完成。工艺卡核对异常的产品：{bad} 个")
        return 1 if bad else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
