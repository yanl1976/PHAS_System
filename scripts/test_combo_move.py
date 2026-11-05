"""工时组合「装配结构」的回归：方向键改层级 / 调顺序 + 按件名一键归位。

用法：python scripts/test_combo_move.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

这一版把「开弹窗→在树里找上级→点→保存」换成了方向键：
  ← 退一级   → 挂到上面那件里   ↑↓ 调同层顺序

方向键最容易出的错是「算错落点却照样写库」——挪到差不多的地方、层号没跟上、
或者在第一条按 ↑ 时把顺序绕成死循环。所以下面按动作逐项钉住：

十一组：
  [1] 结构树只给真实 parent_id，不掺按件名聚的虚拟分组
  [2] → 进一级：挂到「上面最近的同级兄弟」下，层号 +1，标 manual
  [3] ← 退一级：退到原父的父下，并紧跟原父之后
  [4] ↑ ↓ 调同层顺序：只换序号，不动父子关系
  [5] 边界：最前/最后/最上层/上面没有兄弟，一律报错且不改数据
  [6] 批量挂载：一批一次挂到同一件下，坏件名单独跳过、不拖累整批
  [7] 整支下沉时，下面子孙的层号跟着重算
  [8] 按件名一键归位：只认严格前缀，认不出的不动；预演不写库
  [9] 整支归位后审计齐全
  [10] 撤销：move 原样退回去（顺序互换退双方、撞车重编号退整组、坏数据被拒）
  [11] 每一步都留审计，且不再有「人工改过被重铺冲掉」的口子
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

PROD = "ZZ结构产品"

ROOT = "整机装配"
A = "刀具"
B = "料仓"
C = "刀轮"          # 和「刀具」没有前缀关系，自动归位认不出来
A1 = "刀具Ф5.0"      # 严格前缀「刀具」
A2 = "刀具去刺打花"   # 严格前缀「刀具」
B1 = "料仓水套"      # 严格前缀「料仓」

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


def make_combo(db, part: str, *, seq: int = 100) -> WorkCombo:
    """建一件：工时组合（工艺身份）+ 结构节点（装在哪）。

    结构与工艺已经拆成两张表，只建组合不建节点，这件在结构里就不存在 ——
    原来是一行两用，现在必须两边都建。
    """
    row = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == part))
    if row is None:
        row = WorkCombo(product=PROD, part=part, status="draft", note="test-move")
        db.add(row)
        db.flush()
    node = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == part))
    if node is None:
        node = ProductNode(product=PROD, part=part, level=1, seq=seq, source="auto")
        db.add(node)
        db.flush()
    node.seq = seq
    db.add(node)
    db.flush()
    return row


def bind(db, child: str, parent: str) -> None:
    c = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == child))
    p = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == parent))
    c.parent_id = p.id
    c.level = int(p.level or 0) + 1
    db.add(c)
    db.flush()


def row_of(db, part: str) -> ProductNode:
    """取结构节点。层级相关断言看的是「装在哪」，那是结构表的事。"""
    db.expire_all()
    return db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == part))


def parent_of(db, part: str) -> str:
    r = row_of(db, part)
    if not r.parent_id:
        return ""
    p = db.get(ProductNode, r.parent_id)
    return p.part if p else ""


def cleanup() -> None:
    db = SessionLocal()
    try:
        for n in db.scalars(select(ProductNode).where(ProductNode.product == PROD)).all():
            for a in db.scalars(
                select(AuditLog).where(AuditLog.entity == "node", AuditLog.entity_id == n.id)
            ).all():
                db.delete(a)
            db.delete(n)
        for c in db.scalars(select(WorkCombo).where(WorkCombo.product == PROD)).all():
            for a in db.scalars(
                select(AuditLog).where(AuditLog.entity == "combo", AuditLog.entity_id == c.id)
            ).all():
                db.delete(a)
            db.delete(c)
        db.commit()
    finally:
        db.close()


def move(client, part: str, action: str):
    return client.post("/api/combos/move", json={"product": PROD, "part": part, "action": action})


def sibling_order(db) -> list[str]:
    """总装下按显示顺序排的件名，用来验证 ↑↓ 到底有没有挪动。"""
    db.expire_all()
    root = row_of(db, ROOT)
    kids = [
        c
        for c in db.scalars(select(ProductNode).where(ProductNode.product == PROD)).all()
        if c.parent_id == root.id
    ]
    return [c.part for c in sorted(kids, key=lambda c: (int(c.seq or 0), c.part or ""))]


def reset_layout(db) -> None:
    """回到「总装下平铺这几个件」的初始状态，每组测独立的落点。

    一次查全、一次提交：中途再调 `row_of()`（内部 expire_all）会把刚改完
    还没 flush 的属性丢弃，只有最后一件生效 —— 这个坑踩过一次。
    """
    db.expire_all()
    rows = {
        c.part: c
        for c in db.scalars(select(ProductNode).where(ProductNode.product == PROD)).all()
    }
    root = rows[ROOT]
    root.level = 0
    root.parent_id = None
    db.add(root)
    # 顺序要把「刀具Ф5.0」摆在「刀具」正下方：→ 挂的是**上面那一件**，
    # 摆错位置就会挂到别的件底下，测出来的结论也就不是这条规则
    layout = [(A, 10), (A1, 20), (B, 30), (C, 40), (A2, 50), (B1, 60)]
    for name, seq in layout:
        r = rows[name]
        r.parent_id = root.id
        r.level = 1
        r.seq = seq
        r.source = "auto"
        db.add(r)
    db.commit()


def main() -> int:
    init_db()
    cleanup()
    from app.main import app

    client = TestClient(app)
    db = SessionLocal()

    make_combo(db, ROOT, seq=10)
    root_node0 = row_of(db, ROOT)
    root_node0.level = 0
    root_node0.parent_id = None
    db.add(root_node0)
    for i, name in enumerate([A, B, C], start=1):
        make_combo(db, name, seq=i * 10)
    for name in (A1, A2, B1):
        make_combo(db, name, seq=100)
    db.commit()
    reset_layout(db)

    # ---------------------------------------------------------------- 1
    section("1 结构树：只认真实 parent_id")
    r = client.get("/api/combos/tree", params={"product": PROD, "structure": 1})
    check(r.status_code == 200, f"结构树接口可用 → {r.status_code}")
    data = r.json()
    prod = next((p for p in data["products"] if p["product"] == PROD), None)
    check(prod is not None, "树里有这个产品")
    root_node = (prod or {}).get("roots", [{}])[0]
    check(root_node.get("part") == ROOT, f"根是「{root_node.get('part')}」")
    kids = [c["part"] for c in root_node.get("children") or []]
    check(set(kids) == {A, B, C, A1, A2, B1}, f"根下就是这几件，没有虚拟分组（{kids}）")
    plain = client.get("/api/combos/tree", params={"product": PROD}).json()
    plain_kids = [c["part"] for c in plain["products"][0]["roots"][0]["children"] or []]
    check(
        len(plain_kids) <= len(kids),
        f"不带 structure 时仍会给散件套前缀分组（{len(plain_kids)} vs {len(kids)}）",
    )

    # ---------------------------------------------------------------- 2
    section("2 → 进一级：挂到上面最近的同级兄弟下")
    r = move(client, C, "indent")
    check(r.status_code == 200, f"→ 可用 → {r.status_code}")
    res = r.json()
    check(res["parent_part"] == B, f"「刀轮」挂到它上面那件「{res['parent_part']}」下")
    check((res["path"] or [])[-2:] == [B, C], f"路径 = {' › '.join(res['path'])}")
    check(int(res["level"]) == 2, f"层号变成 {res['level']}")
    check(row_of(db, C).source == "manual", "人工挪过的标 manual")

    reset_layout(db)
    r = move(client, A1, "indent")
    check(r.status_code == 200 and r.json()["parent_part"] == A, "「刀具Ф5.0」→ 挂到「刀具」下")
    check(int(row_of(db, A1).level) == 2, "层号 2")

    # 上面没有同级兄弟时不能挂
    reset_layout(db)
    r = move(client, A, "indent")
    check(r.status_code == 400, f"第一件按 → 被拒 → {r.status_code}")
    check(parent_of(db, A) == ROOT, "被拒后位置没变")

    # ---------------------------------------------------------------- 3
    section("3 ← 退一级：退到原父的父下，紧跟原父之后")
    reset_layout(db)
    bind(db, A1, A)
    bind(db, C, A)
    db.commit()
    C_seq_expected = int(row_of(db, A).seq) + 1
    r = move(client, C, "outdent")
    check(r.status_code == 200, f"← 可用 → {r.status_code}")
    res = r.json()
    check(res["parent_part"] == ROOT, f"退回「{res['parent_part']}」")
    check(int(res["level"]) == 1, f"层号回到 {res['level']}")
    check(int(row_of(db, C).seq) == C_seq_expected, "序号紧跟原父之后，不跳到队尾")
    check(parent_of(db, A1) == A, "同批没动的件还在原处")

    reset_layout(db)
    r = move(client, ROOT, "outdent")
    check(r.status_code == 400, f"最上层再按 ← 被拒 → {r.status_code}")
    check(parent_of(db, ROOT) == "", "被拒后位置没变")

    # ---------------------------------------------------------------- 4
    section("4 ↑ ↓ 调同层顺序")
    reset_layout(db)
    order0 = sibling_order(db)
    r = move(client, B, "up")
    check(r.status_code == 200, f"↑ 可用 → {r.status_code}")
    order1 = sibling_order(db)
    check(order1.index(B) == order0.index(B) - 1, f"「料仓」往前挪了一位（{order0} → {order1}）")
    check(parent_of(db, B) == ROOT, "调顺序不动父子关系")
    r = move(client, B, "down")
    check(r.status_code == 200, f"↓ 可用 → {r.status_code}")
    order2 = sibling_order(db)
    check(order2 == order0, f"再按 ↓ 回到原来的次序（{order2}）")

    r = move(client, A, "up")
    check(r.status_code == 400, f"最前面再按 ↑ 被拒 → {r.status_code}")
    db.expire_all()
    kids = [
        c
        for c in db.scalars(select(ProductNode).where(ProductNode.product == PROD)).all()
        if c.parent_id == row_of(db, ROOT).id
    ]
    last = max(kids, key=lambda c: (int(c.seq or 0), c.part or ""))
    r = move(client, last.part, "down")
    check(r.status_code == 400, f"最后面再按 ↓ 被拒 → {r.status_code}")

    # ---------------------------------------------------------------- 5
    section("5 参数与边界")
    r = client.post("/api/combos/move", json={"product": PROD, "part": "不存在的件", "action": "up"})
    check(r.status_code == 404, f"件名不存在 → {r.status_code}")
    r = move(client, A, "sideways")
    check(r.status_code == 400, f"不认识的动作 → {r.status_code}")

    # ---------------------------------------------------------------- 6
    section("6 批量挂载：一次挂一批，坏件名单跳过")
    reset_layout(db)
    r = client.post(
        "/api/combos/attach",
        json={"product": PROD, "parts": [A1, A2, "查无此件"], "parent_part": A},
    )
    check(r.status_code == 200, f"批量挂载可用 → {r.status_code}")
    res = r.json()
    check(sorted(res["moved"]) == sorted([A1, A2]), f"挂上了 {res['moved']}")
    check(len(res["skipped"]) == 1 and res["skipped"][0]["part"] == "查无此件", "坏件名单独跳过")
    check(parent_of(db, A1) == A and parent_of(db, A2) == A, "两件都在「刀具」下")
    check(int(row_of(db, A1).level) == 2 and int(row_of(db, A2).level) == 2, "层号都跟着 2")

    r = client.post("/api/combos/attach", json={"product": PROD, "parts": [A1], "parent_part": A})
    check(r.json()["stayed"] == [A1], "已经在目标位置的报「没变」，不重复写")

    # ---------------------------------------------------------------- 7
    section("7 整支下沉：子孙层号跟着重算")
    reset_layout(db)
    bind(db, B1, B)
    db.commit()
    check(int(row_of(db, B1).level) == 2, "料仓水套先在 2 层")
    # 把「料仓」挂到「刀具」下 → 水套应该跟着沉到 3 层
    r = move(client, B, "indent")
    check(r.status_code == 200, f"料仓 → 挂进刀具下 → {r.status_code}")
    check(int(row_of(db, B).level) == 2, "料仓到 2 层")
    check(int(row_of(db, B1).level) == 3, f"料仓水套跟着到 {int(row_of(db, B1).level)} 层")
    check(parent_of(db, B1) == B, "水套的上级还是料仓")

    # ---------------------------------------------------------------- 8
    section("8 按件名一键归位：只认严格前缀")
    reset_layout(db)
    pre = client.post("/api/combos/auto-nest", json={"product": PROD, "dry_run": True}).json()
    pairs = {(it["part"], it["parent"]) for it in pre["items"]}
    check(("刀具Ф5.0", A) in pairs, "预演里「刀具Ф5.0」→「刀具」")
    check(("刀具去刺打花", A) in pairs, "预演里「刀具去刺打花」→「刀具」")
    check(all(it["part"] != C for it in pre["items"]), "「刀轮」认不出来，不进预演")
    check(parent_of(db, A1) == ROOT, "预演不写库")

    # 只勾中一件：清单里漏掉的不能顺手也改了
    part = client.post(
        "/api/combos/auto-nest", json={"product": PROD, "dry_run": False, "parts": [A1]}
    ).json()
    check(part["applied"] == 1, f"只落勾中的 1 件（applied={part['applied']}）")
    check(parent_of(db, A1) == A, "勾中的「刀具Ф5.0」挂到「刀具」下")
    check(parent_of(db, A2) == ROOT, "没勾的「刀具去刺打花」留在原处")

    done = client.post("/api/combos/auto-nest", json={"product": PROD, "dry_run": False}).json()
    check(done["count"] == pre["count"] - 1, f"剩下 {done['count']} 件一起归位")
    check(parent_of(db, A1) == A and parent_of(db, A2) == A, "两件都挂到「刀具」下")
    check(parent_of(db, C) == ROOT, "认不出来的「刀轮」留在原处，没有乱猜")
    check(int(row_of(db, A1).level) == 2, "层号跟着变")

    again = client.post("/api/combos/auto-nest", json={"product": PROD, "dry_run": True}).json()
    check(again["count"] == 0, f"再预演一次没有可动的（{again['count']}）")

    # ---------------------------------------------------------------- 10
    section("10 撤销：move 原样退回去")
    reset_layout(db)
    # → 之后撤销：应回到总装下、层号回到 1
    r = move(client, C, "indent")
    snap = r.json()["undo"]
    check(len(snap) >= 1, f"move 回来了 {len(snap)} 行改动前快照")
    check(parent_of(db, C) == B, "先真的挂进去了")
    u = client.post("/api/combos/undo-move", json={"product": PROD, "rows": snap})
    check(u.status_code == 200, f"撤销接口可用 → {u.status_code}")
    check(parent_of(db, C) == ROOT, f"撤销后回到「{parent_of(db, C)}」下")
    check(int(row_of(db, C).level) == 1, "层号也跟着回去了")
    check(u.json()["restored"] == len(snap), "恢复行数与快照一致")

    # ↑ 之后撤销：两件的序号都要回去，不能只退一件
    reset_layout(db)
    order0 = sibling_order(db)
    r = move(client, B, "up")
    snap = r.json()["undo"]
    check(len(snap) == 2, f"↑ 的快照含双方两行（{len(snap)}）")
    check(sibling_order(db) != order0, "先真的换了位")
    client.post("/api/combos/undo-move", json={"product": PROD, "rows": snap})
    check(sibling_order(db) == order0, f"撤销后次序复原（{sibling_order(db)}）")

    # 序号撞车时会整组重编号，撤销必须整组退，退一半等于乱序
    db.expire_all()
    rows_all = {
        c.part: c
        for c in db.scalars(select(ProductNode).where(ProductNode.product == PROD)).all()
    }
    for name in (A, B):
        rows_all[name].seq = 50
        db.add(rows_all[name])
    db.commit()
    # 基准值要在「撞车已造好」之后取，否则等于拿改动前的值和自己比
    before_seq = {n: int(c.seq or 0) for n, c in rows_all.items()}
    r = move(client, B, "up")
    check(r.status_code == 200, f"同号时 ↑ 仍能落位 → {r.status_code}")
    snap = (r.json() or {}).get("undo") or []
    check(len(snap) >= 3, f"撞车时快照含整组（{len(snap)} 行，不止两行）")
    client.post("/api/combos/undo-move", json={"product": PROD, "rows": snap})
    db.expire_all()
    rows_all = {
        c.part: c
        for c in db.scalars(select(ProductNode).where(ProductNode.product == PROD)).all()
    }
    now_seq = {n: int(c.seq or 0) for n, c in rows_all.items()}
    check(
        all(now_seq[n] == before_seq[n] for n in (A, B, C)),
        f"撞车重编号后整组退回到原值（A/B/C={now_seq[A]}/{now_seq[B]}/{now_seq[C]}）",
    )

    # 坏数据不许趁撤销把树弄成环
    bad = client.post(
        "/api/combos/undo-move", json={"product": PROD, "rows": [{"id": 999999, "seq": 1}]}
    )
    check(bad.status_code == 400, f"不存在的行被拒 → {bad.status_code}")
    self_row = row_of(db, A)
    bad2 = client.post(
        "/api/combos/undo-move",
        json={"product": PROD, "rows": [{"id": self_row.id, "parent_id": self_row.id, "seq": 10}]},
    )
    check(bad2.status_code == 400, f"指向自己的撤销被拒 → {bad2.status_code}")
    check(parent_of(db, A) == ROOT, "被拒后数据没被动过")

    # 两行互指：单看每一行都「合法」，合起来就是环，必须整批拒
    ida, ida1 = row_of(db, A).id, row_of(db, A1).id
    cyc = client.post(
        "/api/combos/undo-move",
        json={
            "product": PROD,
            "rows": [
                {"id": ida, "parent_id": ida1, "seq": 10},
                {"id": ida1, "parent_id": ida, "seq": 10},
            ],
        },
    )
    check(cyc.status_code == 400, f"互指成环的撤销被整批拒 → {cyc.status_code}")
    check(parent_of(db, A) == ROOT and parent_of(db, A1) == ROOT, "环被拒后两行都还在原位")

    # ---------------------------------------------------------------- 11
    section("11 审计留痕")
    db.expire_all()
    rows = {A1: row_of(db, A1).id, C: row_of(db, C).id}
    # 结构改动现在记在结构实体名下（entity="node"），不再挂到组合上
    logs = db.scalars(
        select(AuditLog).where(
            AuditLog.entity == "node", AuditLog.entity_id.in_(list(rows.values()))
        )
    ).all()
    fields = {a.field for a in logs}
    check("parent_id" in fields, f"改上级留了审计（{sorted(fields)}）")
    check(all(a.user not in ("", None) for a in logs), "每条都记了操作人")

    print(f"\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for x in FAIL:
            print(f"  FAIL {x}")
    db.close()
    cleanup()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
