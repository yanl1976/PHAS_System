"""工时组合「部件层级」的回归。

用法：python scripts/test_combo_level.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

这个脚本存在的理由：层级（哪件装在哪件下面）以前只能靠迁移时按件名规则
自动铺，台账里连个入口都没有 —— 文档写着「工艺员点改层级」，按钮却不存在。
补入口时最容易被改坏的是下面这几条，所以逐条钉住：

八组：
  [1] 层级树只认真实 parent_id，且自己不掺虚拟分组
  [2] 禁选集 = 自己 + 全部子孙（界面据此点灰，不能等提交才报错）
  [3] 改层级：挂到别的件下，parent/level/path 都对，并标 manual
  [4] 提到最上层：parent 清空、层号回 0
  [5] 整支下沉时下级层号跟着重算（affected），否则工艺卡编号会歪
  [6] 拒绝：挂自己、挂自己下级（成环）、跨产品、上级不存在
  [7] 按件名重铺不冲掉人工定过的
  [8] 台账清单直接带上级件名，且改层级留了审计
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import AuditLog, ProductNode, WorkCombo  # noqa: E402
from app.services import combo_registry as CR  # noqa: E402

PROD = "ZZ层级产品"
PROD_B = "ZZ层级产品B"

ROOT = "整机装配"
COMP = "料仓组件"
LEAF = "料仓水套"
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


def make_combo(db, product: str, part: str, *, seq: int = 100) -> ProductNode:
    """建一件：工时组合（工艺身份）+ 结构节点（装在哪）。

    两张表分开以后，「层级」只存在结构表里；组合表只剩工艺身份和发布状态。
    这里返回**结构节点**，因为本脚本断言的 parent_id / level 都是结构字段。
    """
    combo = db.scalar(
        select(WorkCombo).where(WorkCombo.product == product, WorkCombo.part == part)
    )
    if combo is None:
        combo = WorkCombo(product=product, part=part, status="draft", note="test-level")
        db.add(combo)
        db.flush()
    node = db.scalar(
        select(ProductNode).where(ProductNode.product == product, ProductNode.part == part)
    )
    if node is None:
        node = ProductNode(product=product, part=part, level=1, seq=seq, source="auto")
        db.add(node)
        db.flush()
    node.seq = seq
    db.add(node)
    db.flush()
    return node


def bind(db, child: str, parent: str) -> None:
    """直接改库铺出初始层级（不经过接口，接口本身是被测对象）。"""
    c = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == child))
    p = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == parent))
    c.parent_id = p.id
    c.level = (p.level or 0) + 1
    db.add(c)
    db.flush()


def row_of(db, part: str, product: str = PROD) -> ProductNode:
    db.expire_all()
    return db.scalar(
        select(ProductNode).where(ProductNode.product == product, ProductNode.part == part)
    )


def cleanup() -> None:
    db = SessionLocal()
    try:
        for prod in (PROD, PROD_B):
            for n in db.scalars(select(ProductNode).where(ProductNode.product == prod)).all():
                for a in db.scalars(
                    select(AuditLog).where(
                        AuditLog.entity == "node", AuditLog.entity_id == n.id
                    )
                ).all():
                    db.delete(a)
                db.delete(n)
            for c in db.scalars(select(WorkCombo).where(WorkCombo.product == prod)).all():
                for a in db.scalars(
                    select(AuditLog).where(
                        AuditLog.entity == "combo", AuditLog.entity_id == c.id
                    )
                ).all():
                    db.delete(a)
                db.delete(c)
        db.commit()
    finally:
        db.close()


def main() -> int:
    init_db()
    cleanup()
    from app.main import app

    client = TestClient(app)
    db = SessionLocal()

    root = make_combo(db, PROD, ROOT, seq=10)
    root.level = 0
    root.parent_id = None
    comp = make_combo(db, PROD, COMP, seq=20)
    leaf = make_combo(db, PROD, LEAF, seq=30)
    free = make_combo(db, PROD, FREE, seq=40)
    outside = make_combo(db, PROD_B, OUTSIDE, seq=10)
    outside.level = 0
    bind(db, COMP, ROOT)
    bind(db, LEAF, COMP)
    bind(db, FREE, ROOT)
    db.commit()

    # ---------------------------------------------------------------- 1
    section("1 层级树：真实层级 + 自身信息")
    r = client.get("/api/combos/level-tree", params={"product": PROD, "part": FREE})
    check(r.status_code == 200, f"接口可用 → {r.status_code}")
    tree = r.json()
    check(tree["self"]["parent_part"] == ROOT, f"自己现在装在「{tree['self']['parent_part']}」")
    check(tree["self"]["path"] == [ROOT, FREE], f"路径 = {' › '.join(tree['self']['path'])}")
    check(len(tree["roots"]) == 1 and tree["roots"][0]["part"] == ROOT, "只有一个根：整机装配")
    names = [c["part"] for c in tree["roots"][0]["children"]]
    check(COMP in names and FREE in names, f"根下挂着部件和零件（{names}）")
    comp_node = next(c for c in tree["roots"][0]["children"] if c["part"] == COMP)
    check(
        [x["part"] for x in comp_node["children"]] == [LEAF],
        "「料仓水套」在「料仓组件」下，不是平铺到根上",
    )
    check(client.get("/api/combos/level-tree", params={"product": PROD}).status_code == 200,
          "不带 part 也能取整棵树（新增组合时选上级要用）")
    check(
        client.get("/api/combos/level-tree", params={"product": ""}).status_code == 400,
        "不指定产品直接拒：层级只在同一产品内成立",
    )

    # ---------------------------------------------------------------- 2
    section("2 禁选集 = 自己 + 全部子孙")
    tree = client.get("/api/combos/level-tree", params={"product": PROD, "part": COMP}).json()
    blocked = set(tree["blocked"])
    ids = {
        "root": root.id,
        "comp": comp.id,
        "leaf": leaf.id,
        "free": free.id,
    }
    check(blocked == {ids["comp"], ids["leaf"]}, f"料仓组件禁选它自己 + 水套（{len(blocked)} 个）")
    check(ids["free"] not in blocked, "平级的「刀具」仍可选")
    check(ids["root"] not in blocked, "上级「整机装配」仍可选（可以把部件提上去）")


    def node_blocked(nodes, part):
        for n in nodes:
            if n["part"] == part:
                return n["blocked"]
            hit = node_blocked(n["children"], part)
            if hit is not None:
                return hit
        return None


    check(node_blocked(tree["roots"], COMP) is True, "树里「料仓组件」自己标了 blocked")
    check(node_blocked(tree["roots"], LEAF) is True, "它的下级也标了 blocked")
    check(node_blocked(tree["roots"], FREE) is False, "无关件没有标 blocked")

    # ---------------------------------------------------------------- 3
    section("3 改层级：把「刀具」挂到「料仓组件」下")
    r = client.patch(
        "/api/combos/level",
        json={
            "product": PROD,
            "part": FREE,
            "parent_part": COMP,
            "qty_per_unit": 2,
            "seq": 25,
        },
    )
    check(r.status_code == 200, f"保存 → {r.status_code}")
    body = r.json()
    check(body["parent_part"] == COMP, f"返回新上级：{body['parent_part']}")
    check(body["level"] == 2, f"层号按上级推出来（部件=1 → 子件=2，实得 {body['level']}）")
    check(body["path"] == [ROOT, COMP, FREE], f"路径 = {' › '.join(body['path'])}")
    check(body["level_src"] == "manual", "标成人工指定，重铺不再动它")
    check(body["qty_per_unit"] == 2, "每台用量一起存下来")
    check(body["seq"] == 25, "排序号一起存下来")
    row = row_of(db, FREE)
    check(row.parent_id == comp.id, "库里 parent_id 指向料仓组件")

    # ---------------------------------------------------------------- 4
    section("4 提到最上层")
    r = client.patch("/api/combos/level", json={"product": PROD, "part": FREE, "parent_part": ""})
    check(r.status_code == 200, f"保存 → {r.status_code}")
    body = r.json()
    check(body["parent_id"] is None, "parent_id 清空")
    check(body["level"] == 0, f"层号回 0（最上层，实得 {body['level']}）")
    check(body["path"] == [FREE], f"路径只剩自己（{' › '.join(body['path'])}）")

    # ---------------------------------------------------------------- 5
    section("5 整支下沉：下级层号跟着重算")
    # 先摆回：整机装配(0) → 料仓组件(1) → 料仓水套(2)
    client.patch("/api/combos/level", json={"product": PROD, "part": FREE, "parent_part": ROOT})
    client.patch("/api/combos/level", json={"product": PROD, "part": COMP, "parent_part": ROOT})
    check(row_of(db, LEAF).level == 2, f"水套现在是子件（level {row_of(db, LEAF).level}）")
    # 把整支抬上去：料仓组件 → 最上层，水套该跟着从 2 变 1
    r = client.patch("/api/combos/level", json={"product": PROD, "part": COMP, "parent_part": ""})
    body = r.json()
    check(body["level"] == 0, "料仓组件升到最上层")
    check(body["affected"] >= 1, f"报出跟着调整的下级条数（{body['affected']}）")
    check(row_of(db, LEAF).level == 1, f"水套层号跟着从 2 变 1（实得 {row_of(db, LEAF).level}）")

    # ---------------------------------------------------------------- 6
    section("6 拒绝：自己 / 下级 / 跨产品 / 不存在")
    client.patch("/api/combos/level", json={"product": PROD, "part": COMP, "parent_part": ROOT})
    r = client.patch("/api/combos/level", json={"product": PROD, "part": ROOT, "parent_part": ROOT})
    check(r.status_code == 400 and "自己" in r.json()["detail"], f"挂自己 → {r.status_code} {r.text[:40]}")
    r = client.patch(
        "/api/combos/level", json={"product": PROD, "part": ROOT, "parent_part": LEAF}
    )
    check(r.status_code == 400 and "环" in r.json()["detail"], f"挂到自己的下级 → {r.text[:40]}")
    r = client.patch(
        "/api/combos/level", json={"product": PROD, "part": FREE, "parent_part": OUTSIDE}
    )
    check(r.status_code in (400, 404), f"跨产品挂了「{OUTSIDE}」→ {r.status_code}")
    r = client.patch(
        "/api/combos/level", json={"product": PROD, "part": FREE, "parent_part": "没有这个件"}
    )
    check(r.status_code == 400 and "不存在" in r.json()["detail"], f"上级不存在 → {r.text[:40]}")
    check(row_of(db, ROOT).parent_id is None, "被拒之后原层级没被改坏")

    # ---------------------------------------------------------------- 7
    section("7 按件名重铺不冲掉人工定过的")
    client.patch("/api/combos/level", json={"product": PROD, "part": FREE, "parent_part": COMP})
    check(row_of(db, FREE).source == "manual", "刀具是人工挂的")
    r = client.post("/api/combos/relayout", params={"product": PROD})
    check(r.status_code == 200, f"重铺 → {r.status_code}")
    detail = r.json()["detail"][0]
    check(detail["skipped"] >= 1, f"重铺跳过 {detail['skipped']} 条人工的")
    row = row_of(db, FREE)
    check(row.parent_id == comp.id, "刀具仍挂在料仓组件下，没被件名规则拉回根")
    check(row_of(db, COMP).parent_id == root.id, "没人工定过的部件按件名规则挂回总装")

    # ---------------------------------------------------------------- 8
    section("8 台账清单带上级 + 审计留痕")
    lst = client.get("/api/combos", params={"q": FREE, "include_disabled": True}).json()
    hit = next((x for x in lst["items"] if x["part"] == FREE and x["product"] == PROD), None)
    check(hit is not None, "台账里找得到这条")
    check(hit and hit["parent_part"] == COMP, f"清单直接给上级件名（{hit and hit['parent_part']}）")
    check(hit and hit["qty_per_unit"] == 2, "每台用量也在清单里")
    db.expire_all()
    logs = [
        a
        for a in db.scalars(
            select(AuditLog).where(
                AuditLog.entity == "node", AuditLog.entity_id == row_of(db, FREE).id
            )
        ).all()
        if a.field == "parent_id"
    ]
    check(len(logs) >= 1, f"改上级留了 {len(logs)} 条审计")
    check(logs and logs[-1].user not in ("", None), "审计里记了操作人")

    print(f"\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for x in FAIL:
            print(f"  FAIL {x}")
    db.close()
    cleanup()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
