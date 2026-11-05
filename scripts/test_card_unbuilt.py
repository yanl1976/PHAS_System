"""工艺卡：结构里新增的件（还没编工艺）要在层级里可见。

用法：python scripts/test_card_unbuilt.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

现场问题：在装配结构里新增 1112，工时组合台账看得到，但工艺卡的「部件层级」
没有它 —— 因为工艺卡的树只走「有已发布路线的组合」，纯结构节点根本不在其中。

修复口径（本脚本锁死）：
  * 层级里要出现，标 `has_route=False`，状态区分 none / draft / archived；
  * **绝不出卡**（没有工序可印，空白卡会让车间以为漏工序）；
  * 工时、编号、卡片张数都不受影响；
  * 占位件下面还有纯结构子件时，递归显示。

六组：
  [1] 纯结构件（无组合无路线）→ 层级可见、不出卡、工时为 0
  [2] 编号与张数不受占位影响
  [3] 只建草稿 → 状态变「草稿」，仍不出卡
  [4] 发布后 → 变成真正的卡，占位消失、编号补上
  [5] 占位件下还有占位子件 → 递归
  [6] 任务口径：单里的件下面同样能看到未建工艺的子件
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    ProcessRoute,
    ProcessRouteStep,
    ProductNode,
    WorkCombo,
)
from app.services import combo_registry as CR  # noqa: E402
from app.services import process_card as PC  # noqa: E402

PROD = "ZZ未建工艺卡产品"
PASS = 0
FAIL: list[str] = []


def check(cond, msg: str) -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  OK   {msg}")
    else:
        FAIL.append(msg)
        print(f"  FAIL {msg}")


def section(name: str) -> None:
    print(f"\n[{name}]")


def node_of(db, part: str, product: str = PROD) -> ProductNode | None:
    return db.scalar(
        select(ProductNode).where(ProductNode.product == product, ProductNode.part == part)
    )


def add_node(db, part: str, parent: str | None = None, qty: float = 1.0, seq: int = 100):
    """只建结构节点（没有组合、没有路线）—— 就是用户在结构里加的那一件。"""
    pid, level = None, 1
    if parent:
        p = node_of(db, parent)
        pid = p.id
        level = int(p.level or 0) + 1
    n = ProductNode(
        product=PROD,
        part=part,
        parent_id=pid,
        level=level,
        qty_per_unit=qty,
        seq=seq,
        source="manual",
    )
    db.add(n)
    db.flush()
    return n


def add_combo_route(db, part: str, hours: float = 1.0, released: bool = True):
    """建组合身份 + 一版路线；released=True 走真实发布。"""
    route = ProcessRoute(
        kind="route",
        product=PROD,
        part=part,
        version=1,
        status="draft",
        created_by="test",
        source="manual",
    )
    db.add(route)
    db.flush()
    db.add(
        ProcessRouteStep(
            route_id=route.id,
            seq=10,
            process_name="车",
            process_code="车",
            unit_hours=hours,
            setup_hours=0.0,
        )
    )
    combo = db.scalar(
        select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == part)
    )
    if combo is None:
        combo = WorkCombo(product=PROD, part=part, status="draft", note="test")
        db.add(combo)
    db.flush()
    if released:
        CR.release_combo(db, combo, by="test")
    db.commit()
    return route, combo


def find(node_list: list[dict], part: str) -> dict | None:
    for n in node_list:
        if (n.get("part") or "").strip() == part:
            return n
        hit = find(n.get("children") or [], part)
        if hit:
            return hit
    return None


def cleanup() -> None:
    db = SessionLocal()
    try:
        combos = db.scalars(select(WorkCombo).where(WorkCombo.product == PROD)).all()
        ids = [c.id for c in combos]
        nodes = db.scalars(select(ProductNode).where(ProductNode.product == PROD)).all()
        node_ids = [n.id for n in nodes]
        for r in db.scalars(select(ProcessRoute).where(ProcessRoute.product == PROD)).all():
            for st in db.scalars(
                select(ProcessRouteStep).where(ProcessRouteStep.route_id == r.id)
            ).all():
                db.delete(st)
            db.delete(r)
        # 先删子节点再删父节点：外键指着自己这张表
        for n in sorted(nodes, key=lambda x: -(x.level or 0)):
            db.delete(n)
        for c in combos:
            db.delete(c)
        for a in db.scalars(select(AuditLog).where(AuditLog.entity == "combo")).all():
            if a.entity_id in ids:
                db.delete(a)
        for a in db.scalars(select(AuditLog).where(AuditLog.entity == "node")).all():
            if a.entity_id in node_ids:
                db.delete(a)
        db.commit()
    finally:
        db.close()


def main() -> int:
    init_db()
    cleanup()
    db = SessionLocal()

    # ---- 两条有工艺的（整机装配 → 部件A），外加一件纯结构的新件 ----
    add_node(db, "整机装配", seq=100)
    db.commit()
    add_combo_route(db, "整机装配", hours=2.0)

    add_node(db, "部件A", parent="整机装配", seq=110)
    db.commit()
    add_combo_route(db, "部件A", hours=1.0)

    add_node(db, "新件X", parent="部件A", qty=2.0, seq=120)
    db.commit()
    db.expire_all()

    # ---------------------------------------------------------------- 1
    section("1 纯结构件：层级可见，但不出卡")
    res = PC.build_cards(db, product=PROD)
    x = find(res["tree"], "新件X")
    check(x is not None, "新件X 出现在工艺卡层级里")
    if x:
        check(x["has_route"] is False, "标记为「没有已发布工艺」")
        check(x["status"] == "none", f"状态是 none（没编过），实际 {x['status']}")
        check(x["code"] == "", "没有件号（不参与编号）")
        check(abs(float(x["own_hours"])) < 1e-9, "工时 0，不虚增定额")
        check(abs(float(x["qty_per_unit"]) - 2.0) < 1e-9, "每台用量照结构显示（2）")
    check(
        not any(c["part"] == "新件X" for c in res["cards"]),
        "不会为它生成空白卡",
    )
    check(res["summary"]["unbuilt"] == 1, f"未建计数 1（{res['summary']['unbuilt']}）")

    # ---------------------------------------------------------------- 2
    section("2 编号与合计不受占位影响")
    check(res["summary"]["printed"] == 2, f"出卡仍是 2 张（{res['summary']['printed']}）")
    codes = {c["part"]: c["code"] for c in res["cards"]}
    check(codes.get("整机装配") == "01", f"总装编号 01（{codes.get('整机装配')}）")
    check(codes.get("部件A") == "01-01", f"部件编号 01-01（{codes.get('部件A')}）")
    check(
        abs(res["summary"]["own_hours"] - 3.0) < 1e-9,
        f"本层工时合计 3.0（{res['summary']['own_hours']}）",
    )
    check(
        abs(res["summary"]["subtotal_hours"] - 3.0) < 1e-9,
        f"含子件合计 3.0（{res['summary']['subtotal_hours']}）",
    )

    # ---------------------------------------------------------------- 3
    section("3 只建草稿：状态要能区分「编了没发」")
    route, combo = add_combo_route(db, "新件X", released=False)
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    x = find(res["tree"], "新件X")
    check(x is not None and x["status"] == "draft", f"状态 draft（{x and x['status']}）")
    check(not any(c["part"] == "新件X" for c in res["cards"]), "草稿仍然不出卡")
    check(res["summary"]["unbuilt"] == 1, "未建计数仍算它（还没发布）")

    # 真实场景：草稿阶段**不登记组合身份**（create_route 只建路线），
    # 只看身份表会把「编了草稿」当成「没编」，工艺员会去重编一遍
    db.delete(combo)
    db.commit()
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    x = find(res["tree"], "新件X")
    check(
        x is not None and x["status"] == "draft",
        f"没有身份行、只有草稿路线，也认得出是草稿（{x and x['status']}）",
    )
    check(not any(c["part"] == "新件X" for c in res["cards"]), "仍然不出卡")
    # 恢复身份行，下一组要靠它发布（发布动作本身也是登记身份的地方）
    combo = WorkCombo(product=PROD, part="新件X", status="draft", note="test")
    db.add(combo)
    db.commit()
    db.expire_all()

    # ---------------------------------------------------------------- 4
    section("4 发布后变成真卡片")
    CR.release_combo(db, combo, by="test")
    db.commit()
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    x = find(res["tree"], "新件X")
    check(x is not None and x["has_route"] is True, "发布后 has_route=True")
    check(bool(x and x["code"]), f"补上编号（{x and x['code']}）")
    check(
        any(c["part"] == "新件X" for c in res["cards"]),
        "现在会出卡了",
    )
    check(res["summary"]["unbuilt"] == 0, "未建计数归零")
    check(
        abs(res["summary"]["own_hours"] - 4.0) < 1e-9,
        f"工时加上新件（{res['summary']['own_hours']}）",
    )

    # ---------------------------------------------------------------- 5
    section("5 占位件下面还有纯结构子件 → 递归")
    # 新件X 上面再退一把：先给它挂一个还没编工艺的子件
    add_node(db, "孙件Y", parent="新件X", seq=130)
    db.commit()
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    y = find(res["tree"], "孙件Y")
    check(y is not None, "孙件Y 也出现在层级里")
    check(y is not None and y["has_route"] is False, "孙件Y 标为未建")
    x = find(res["tree"], "新件X")
    check(
        x is not None and any(ch["part"] == "孙件Y" for ch in x["children"]),
        "挂在 新件X 下面（不是被提到根）",
    )
    check(res["summary"]["unbuilt"] == 1, "未建计 1（只有孙件Y）")

    # ---------------------------------------------------------------- 6
    section("6 兄弟件按结构顺序排，不会把占位甩到末尾")
    add_node(db, "中间件Z", parent="部件A", seq=115)
    db.commit()
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    a = find(res["tree"], "部件A")
    order = [ch["part"] for ch in (a["children"] if a else [])]
    check(
        order.index("中间件Z") < order.index("新件X"),
        f"seq 115 排在 seq 120 前面（{order}）",
    )

    section("清理")
    db.close()
    cleanup()
    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 60)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
