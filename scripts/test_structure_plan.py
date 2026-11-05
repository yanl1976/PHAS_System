"""产品结构「新增件」与「计划数量联动」的回归。

用法：python scripts/test_structure_plan.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

这两件事一起测，因为它们共用同一棵树：

* 结构原来没有任何新增入口，只能靠迁移/按件名重铺生成；
* 数量只有「每台用量」，没有「这一批做几件」，父子之间也不会互相影响。

钉住的规则：
  [1] 新增件：能挂到指定上级，level / 每台用量 / 计划量都算对
  [2] 新增的四种拒绝：重名、上级不存在、上级跨产品、用量 <= 0
  [3] 改父件计划量 → 全部 auto 后代按「父 × 用量」重算
  [4] 改子件计划量 → 上级按用量反推；除不尽向上取整并给 warning
  [5] 人工设定（manual）不会被上级变化冲掉，但它的下级仍按它推
  [6] 改「每台用量」后计划量跟着重算（不然两列数字互相矛盾）
  [7] 删件护栏：有下级 / 有工艺 一律拦住；干净的件能删
  [8] 建路线时自动补结构节点（以前建完路线结构里根本没有这一件）
  [9] 编排页左树能看到「结构里有、还没编工艺」的件，并带数量
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import AuditLog, ProcessRoute, ProductNode, WorkCombo  # noqa: E402

PROD = "ZZ计划产品"
PROD_B = "ZZ计划产品B"

ROOT = "整机装配"
COMP = "料仓组件"
LEAF = "料仓水套"
COVER = "料仓盖板"
FREE = "刀具"
OUTSIDE = "外购件"

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
    db.expire_all()
    return db.scalar(
        select(ProductNode).where(ProductNode.product == product, ProductNode.part == part)
    )


def make_node(db, product: str, part: str, *, parent: str | None = None, qty: float = 1.0):
    node = ProductNode(product=product, part=part, qty_per_unit=qty, source="manual")
    if parent:
        p = db.scalar(
            select(ProductNode).where(ProductNode.product == product, ProductNode.part == parent)
        )
        node.parent_id = p.id
        node.level = int(p.level or 0) + 1
    else:
        node.level = 0
    db.add(node)
    db.flush()
    return node


def cleanup() -> None:
    db = SessionLocal()
    try:
        for prod in (PROD, PROD_B):
            for n in db.scalars(select(ProductNode).where(ProductNode.product == prod)).all():
                for a in db.scalars(
                    select(AuditLog).where(AuditLog.entity == "node", AuditLog.entity_id == n.id)
                ).all():
                    db.delete(a)
                db.delete(n)
            for c in db.scalars(select(WorkCombo).where(WorkCombo.product == prod)).all():
                db.delete(c)
            for r in db.scalars(select(ProcessRoute).where(ProcessRoute.product == prod)).all():
                db.delete(r)
        db.commit()
    finally:
        db.close()


def main() -> int:
    init_db()
    cleanup()
    from app.main import app

    client = TestClient(app)
    db = SessionLocal()

    root = make_node(db, PROD, ROOT)
    comp = make_node(db, PROD, COMP, parent=ROOT)
    leaf = make_node(db, PROD, LEAF, parent=COMP, qty=2.0)
    cover = make_node(db, PROD, COVER, parent=COMP, qty=3.0)
    free = make_node(db, PROD, FREE, parent=ROOT)
    make_node(db, PROD_B, OUTSIDE)
    for n in (root, comp, leaf, cover, free):
        n.plan_qty = 1.0
        n.plan_src = "auto"
        db.add(n)
    db.commit()

    # ---------------------------------------------------------------- 1
    section("1 新增件")
    r = client.post(
        "/api/combos/node",
        json={
            "product": PROD,
            "part": "新增支架",
            "parent_part": COMP,
            "qty_per_unit": 2,
        },
    )
    check(r.status_code == 200, f"新增返回 200（{r.status_code}）")
    body = r.json() if r.status_code == 200 else {}
    row = node_of(db, "新增支架")
    check(row is not None, "结构里真有这一件")
    check(row is not None and row.parent_id == comp.id, "挂到了「料仓组件」下")
    check(row is not None and row.level == 2, f"层号 = 上级 + 1（{row and row.level}）")
    check(row is not None and abs(row.qty_per_unit - 2) < 1e-9, "每台用量 = 2")
    # 料仓组件计划 1 × 用量 2 = 2
    check(row is not None and abs(row.plan_qty - 2) < 1e-9, f"计划量按上级派生（{row and row.plan_qty}）")
    check(row is not None and row.plan_src == "auto", "没有人工指定 → plan_src=auto")

    # 挂顶层
    r2 = client.post("/api/combos/node", json={"product": PROD, "part": "顶层级部件"})
    top = node_of(db, "顶层级部件")
    check(r2.status_code == 200 and top is not None and top.parent_id is None, "parent 留空 → 挂顶层")
    check(top is not None and abs(top.plan_qty - 1) < 1e-9, "顶层默认计划 1 台")

    # 人工指定计划量：不被上级派生覆盖，也不反推上级
    before_root = node_of(db, ROOT).plan_qty
    r3 = client.post(
        "/api/combos/node",
        json={"product": PROD, "part": "人工定量件", "parent_part": COMP, "plan_qty": 7},
    )
    manual_row = node_of(db, "人工定量件")
    check(r3.status_code == 200 and manual_row.plan_src == "manual", "填了计划量 → manual")
    check(manual_row is not None and abs(manual_row.plan_qty - 7) < 1e-9, "人工值原样保留")
    check(abs(node_of(db, ROOT).plan_qty - before_root) < 1e-9, "新增不会顺手改上级计划量")

    # ---------------------------------------------------------------- 2
    section("2 新增的拒绝")
    dup = client.post("/api/combos/node", json={"product": PROD, "part": COMP})
    check(dup.status_code == 400 and "已经有" in dup.text, "重名拦住")
    ghost = client.post("/api/combos/node", json={"product": PROD, "part": "X件", "parent_part": "不存在的上级"})
    check(ghost.status_code == 400 and "不在" in ghost.text, "上级不存在拦住")
    cross = client.post("/api/combos/node", json={"product": PROD, "part": "Y件", "parent_part": OUTSIDE})
    check(cross.status_code == 400, "上级在别的产品里 → 拦住")
    bad = client.post("/api/combos/node", json={"product": PROD, "part": "Z件", "qty_per_unit": 0})
    check(bad.status_code == 400 and "大于 0" in bad.text, "用量 0 拦住")
    noname = client.post("/api/combos/node", json={"product": PROD, "part": "  "})
    check(noname.status_code == 400, "件名为空拦住")
    noprod = client.post("/api/combos/node", json={"product": "", "part": "无产品件"})
    check(noprod.status_code == 400, "产品为空拦住")

    # ---------------------------------------------------------------- 3
    section("3 改父件 → 下级按用量重算")
    # 整机 = 2 台；料仓组件用量 1 → 2；水套用量 2 → 4；盖板用量 3 → 6
    r = client.patch("/api/combos/plan", json={"product": PROD, "part": ROOT, "plan_qty": 2})
    check(r.status_code == 200, "改整机计划量返回 200")
    check(abs(node_of(db, COMP).plan_qty - 2) < 1e-9, "部件 = 2 × 1 = 2")
    check(abs(node_of(db, LEAF).plan_qty - 4) < 1e-9, f"水套 = 2 × 2 = 4（{node_of(db, LEAF).plan_qty}）")
    check(abs(node_of(db, COVER).plan_qty - 6) < 1e-9, "盖板 = 2 × 3 = 6")
    check(abs(node_of(db, FREE).plan_qty - 2) < 1e-9, "同层其它件也跟着变")
    check(abs(node_of(db, "新增支架").plan_qty - 4) < 1e-9, "新增件跟着重算")

    # ---------------------------------------------------------------- 4
    section("4 改子件 → 上级按用量反推")
    # 水套改成 3，用量 2 → 料仓组件 = ceil(1.5) = 2
    r = client.patch("/api/combos/plan", json={"product": PROD, "part": LEAF, "plan_qty": 3})
    check(r.status_code == 200, "改子件返回 200")
    data = r.json()
    check(abs(node_of(db, COMP).plan_qty - 2) < 1e-9, f"上级向上取整为 2（{node_of(db, COMP).plan_qty}）")
    check(any("取整" in w or "÷" in w for w in data.get("warnings", [])), "除不尽给了提示")
    # 上级 2 台 → 盖板 6、新增支架 4 重新对齐
    check(abs(node_of(db, COVER).plan_qty - 6) < 1e-9, "兄弟分支按新上级重算")
    check(abs(node_of(db, LEAF).plan_qty - 3) < 1e-9, "被改的那件保留 3（不再被父覆盖）")

    # 反推到根：水套 3 → 部件 2 → 整机 2（用量 1，不变）
    check(abs(node_of(db, ROOT).plan_qty - 2) < 1e-9, "反推一路推到根且保持自洽")

    # ---------------------------------------------------------------- 5
    section("5 人工设定不被冲掉")
    client.patch("/api/combos/plan", json={"product": PROD, "part": COVER, "plan_qty": 5})
    check(abs(node_of(db, COVER).plan_qty - 5) < 1e-9, "先把盖板定成 5（manual）")
    client.patch("/api/combos/plan", json={"product": PROD, "part": ROOT, "plan_qty": 3})
    check(abs(node_of(db, COVER).plan_qty - 5) < 1e-9, "父件改 3 台后，人工值仍是 5")
    check(abs(node_of(db, LEAF).plan_qty - 3) < 1e-9, "auto 的兄弟跟着父件走")
    check(node_of(db, COVER).plan_src == "manual", "plan_src 记住是人工的")

    # 人工件的下级仍按它自己推
    sub = node_of(db, "人工定量件")
    check(sub is not None and sub.parent_id == comp.id, "人工件挂在部件下")

    # ---------------------------------------------------------------- 6
    section("6 改每台用量 → 计划量重算")
    # 用 auto 的「刀具」来验：整机 3 台 × 用量 2 = 6
    r = client.patch(
        "/api/combos/level",
        json={"product": PROD, "part": FREE, "qty_per_unit": 2, "parent_part": ROOT},
    )
    check(r.status_code == 200, "改用量返回 200")
    check(abs(node_of(db, FREE).plan_qty - 6) < 1e-9, f"刀具 = 3 × 2 = 6（{node_of(db, FREE).plan_qty}）")
    # 水套是第 4 节人工定的 3 件，用量改了也不该被悄悄改掉
    r = client.patch(
        "/api/combos/level",
        json={"product": PROD, "part": LEAF, "qty_per_unit": 4, "parent_part": COMP},
    )
    check(r.status_code == 200, "改人工件的用量返回 200")
    check(abs(node_of(db, LEAF).plan_qty - 3) < 1e-9, "人工设定的计划量不随用量变")
    # 但界面必须能看出「按新用量该做 12 件」——结构树带 plan_expect
    tree = client.get("/api/combos/tree", params={"product": PROD, "structure": 1}).json()
    prod = next((p for p in tree["products"] if p["product"] == PROD), None)
    flat = {}

    def walk_nodes(ns):
        for x in ns or []:
            flat[x.get("part")] = x
            walk_nodes(x.get("children"))

    for rt in (prod or {}).get("roots", []):
        walk_nodes([rt])
    check(
        abs(float(flat.get(LEAF, {}).get("plan_expect") or 0) - 12) < 1e-9,
        f"人工值偏离时给出应有的数量 12（{flat.get(LEAF, {}).get('plan_expect')}）",
    )
    check(
        abs(float(flat.get(LEAF, {}).get("plan_qty") or 0) - 3) < 1e-9,
        "但显示的实际计划量仍是人工值 3",
    )

    # ---------------------------------------------------------------- 7
    section("7 删件护栏")
    block = client.delete("/api/combos/node", params={"product": PROD, "part": COMP})
    check(block.status_code == 400 and "下级" in block.text, "有下级的件删不掉")
    # 「有工艺」的判据是**有路线**，不是「有一行身份」。只挂一条空身份、
    # 一张路线都没有的，是删工艺留下的残渣，反过来要允许删并顺手清掉
    # （现场就是这么卡住的，见 test_route_delete_shell.py）。
    rt = client.post(
        "/api/routes",
        json={
            "product": PROD,
            "part": FREE,
            "steps": [{"seq": 10, "process_name": "铣", "unit_hours": 0.5}],
        },
    ).json()
    client.post(f"/api/routes/{rt['id']}/publish", json={"new_version": False})
    block2 = client.delete("/api/combos/node", params={"product": PROD, "part": FREE})
    check(
        block2.status_code == 400 and "工艺身份" in block2.text,
        f"有工艺的件删不掉（{block2.status_code}）",
    )
    # 只有身份行、没有任何路线的空壳：允许删，并且删件时一起收掉
    client.post(
        "/api/combos/node",
        json={"product": PROD, "part": "空壳身份件", "parent_part": ROOT},
    )
    db.add(WorkCombo(product=PROD, part="空壳身份件", status="draft", note="test-shell"))
    db.commit()
    r_shell = client.delete("/api/combos/node", params={"product": PROD, "part": "空壳身份件"})
    check(r_shell.status_code == 200, f"空壳身份不拦删件（{r_shell.status_code}）")
    db.expire_all()
    check(
        db.scalar(
            select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == "空壳身份件")
        )
        is None,
        "空壳身份顺手清掉了",
    )
    ok = client.delete("/api/combos/node", params={"product": PROD, "part": "顶层级部件"})
    check(ok.status_code == 200, "没有牵连的件可以删")
    check(node_of(db, "顶层级部件") is None, "删完结构里确实没了")

    # ---------------------------------------------------------------- 8
    section("8 建路线自动补结构节点")
    r = client.post(
        "/api/routes",
        json={"product": PROD, "part": "全新件", "steps": [{"seq": 10, "process_code": "C001", "unit_hours": 1.0}]},
    )
    check(r.status_code == 200, f"建路线返回 200（{r.status_code}）")
    new_row = node_of(db, "全新件")
    check(new_row is not None, "结构里同步补上了这一件（不再是孤儿）")
    check(new_row is not None and new_row.parent_id == root.id, "默认挂在整机装配下")
    check(new_row is not None and new_row.plan_src == "auto", "计划量按上级派生")

    # ---------------------------------------------------------------- 9
    section("9 编排页左树能看到没工艺的结构件")
    tree = client.get("/api/routes/tree").json()
    prod_node = next((p for p in tree["items"] if p["name"] == PROD), None)
    check(prod_node is not None, "左树里有这个产品")
    parts = {x["part"]: x for x in (prod_node or {}).get("parts", [])}
    check("全新件" in parts, "刚建的空工艺件出现在左树")
    check(parts.get(LEAF, {}).get("qty_per_unit") == 4, "左树带每台用量")
    check(abs(float(parts.get(LEAF, {}).get("plan_qty") or 0) - 3) < 1e-9, "左树带计划数量")

    # ---------------------------------------------------------------- 收尾
    print(f"\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for x in FAIL:
            print(f"  FAIL {x}")
    db.close()
    cleanup()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
