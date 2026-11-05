"""工时组合「停用」的回归。

用法：python scripts/test_combo_archive.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

这个脚本存在的理由：停用一开始只翻了 `StandardHour.active`，台账「状态」列
读的却是 `WorkCombo.status`，于是点了停用状态纹丝不动；而且组合照样留在
`released_index()` 里 —— 识别能匹配、任务能挂线、工艺卡照样印得出来。
下面每一条都是当时漏掉的入口，谁改回去谁红。

七组：
  [1] 停用：状态变已归档，台账 active 跟着变
  [2] 停用不撤线：已发布的那一版仍是 released（在制任务不该失去基准）
  [3] 停用即退出闸门：识别索引、生效工艺、工艺卡全都不再收它
  [4] 归档组合发不出去：台账发布、编排页发布、恢复旧版三条路都拒绝
  [5] 归档组合挂不上线：任务报错，且不会偷偷给它造一条基线
  [6] 启用：路线还在发布态就回已发布，路线不在了就回草稿
  [7] 留痕：停用/启用各记一条审计，重复停用不刷记录
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    ProcessRoute,
    ProcessRouteStep,
    ProductNode,
    TaskRoute,
    WorkCombo,
    WorkTask,
)
from app.services import combo_registry as CR  # noqa: E402
from app.services import process_card as PC  # noqa: E402
from app.services import routes as R  # noqa: E402
from app.services import tasks as T  # noqa: E402

PROD = "ZZ停用产品"
PART = "停用整机"
PART2 = "停用部件"
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


def seed(db, part: str, processes: list[tuple[str, float]], *, parent: str = "") -> ProcessRoute:
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
    for i, (name, hours) in enumerate(processes, start=1):
        db.add(
            ProcessRouteStep(
                route_id=route.id,
                seq=i * 10,
                process_name=name,
                process_code=name,
                unit_hours=hours,
                setup_hours=0.5,
            )
        )
    db.flush()
    combo = db.scalar(
        select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == part)
    )
    if combo is None:
        combo = WorkCombo(product=PROD, part=part, status="draft", note="test")
        db.add(combo)
        db.flush()
    # 结构节点也补上：工艺卡要沿结构展开，只有组合没有节点就出不了卡
    node = db.scalar(
        select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == part)
    )
    if node is None:
        node = ProductNode(product=PROD, part=part, level=0, seq=100, source="auto")
        db.add(node)
        db.flush()
    if parent:
        # 层级已搬到结构表：组合只留工艺身份
        p = db.scalar(
            select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == parent)
        )
        node = db.scalar(
            select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == part)
        )
        if p is not None and node is not None:
            node.parent_id = p.id
            node.level = int(p.level or 1) + 1
            node.source = "manual"
            db.add(node)
    return route


def cleanup() -> None:
    db = SessionLocal()
    try:
        ids = [
            c.id
            for c in db.scalars(select(WorkCombo).where(WorkCombo.product == PROD)).all()
        ]
        node_ids = [
            n.id
            for n in db.scalars(select(ProductNode).where(ProductNode.product == PROD)).all()
        ]
        for tr in db.scalars(select(TaskRoute).where(TaskRoute.product == PROD)).all():
            db.delete(tr)
        for t in db.scalars(select(WorkTask).where(WorkTask.task_no.like("ZZARCH%"))).all():
            db.delete(t)
        for r in db.scalars(select(ProcessRoute).where(ProcessRoute.product == PROD)).all():
            for st in db.scalars(
                select(ProcessRouteStep).where(ProcessRouteStep.route_id == r.id)
            ).all():
                db.delete(st)
            db.delete(r)
        for n in db.scalars(select(ProductNode).where(ProductNode.product == PROD)).all():
            db.delete(n)
        for c in db.scalars(select(WorkCombo).where(WorkCombo.product == PROD)).all():
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


def status_of(db, part: str) -> str:
    db.expire_all()
    c = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == part))
    return c.status if c else ""


def row_of(client, part: str) -> dict:
    d = client.get("/api/combos", params={"q": part, "limit": 20}).json()
    for x in d["items"]:
        if x["part"] == part:
            return x
    return {}


def main() -> int:
    init_db()
    cleanup()
    from app.main import app

    client = TestClient(app)
    db = SessionLocal()

    seed(db, PART, [("总装", 4.0), ("调试", 1.0)])
    seed(db, PART2, [("焊接", 2.0)], parent=PART)
    db.commit()
    for part in (PART, PART2):
        c = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == part))
        CR.release_combo(db, c, by="test", note="初始发布")
    db.commit()

    # ---------------------------------------------------------------- 1
    section("1 停用：状态变已归档")
    r = client.post("/api/combos/active", json={"product": PROD, "part": PART, "active": False})
    check(r.status_code == 200, f"停用接口 → {r.status_code}")
    body = r.json()
    check(body.get("status") == "archived", f"接口直接返回新状态（{body.get('status')}）")
    check(body.get("active") is False, "同时告诉前端「已不在用」")
    check(status_of(db, PART) == "archived", "库里组合状态 = archived")
    row = row_of(client, PART)
    check(row.get("status") == "archived", f"台账状态列变为已归档（{row.get('status')}）")
    check(row.get("active") is False, "台账 active=false，按钮变成「启用」")
    check(row.get("state") == "archived", "旧字段 state 也跟着走（前端还在读它）")
    lst = client.get("/api/combos", params={"q": PART, "include_disabled": False}).json()
    check(
        all(x["part"] != PART for x in lst["items"]),
        "勾「只看在用」时被筛掉",
    )
    lst = client.get("/api/combos", params={"q": PART}).json()
    check(
        any(x["part"] == PART for x in lst["items"]),
        "默认视图仍能看到它（不能凭空少一条，得让人找得到并启用）",
    )

    # ---------------------------------------------------------------- 2
    section("2 停用不撤线")
    db.expire_all()
    route = db.scalar(
        select(ProcessRoute).where(ProcessRoute.product == PROD, ProcessRoute.part == PART)
    )
    check(route.status == "released", f"已发布的那一版仍是 released（{route.status}）")
    check(
        db.scalar(
            select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == PART)
        ).route_id
        == route.id,
        "route_id 仍指着它：启用时靠这个判断回到哪一档",
    )

    # ---------------------------------------------------------------- 3
    section("3 停用即退出闸门")
    idx = R.released_index(db)
    check((PROD, PART) not in idx, "识别索引里没有它了")
    check((PROD, PART2) in idx, "同产品另一个组合不受牵连")
    check(
        CR.active_route(db, PROD, PART) is None,
        "active_route 不再返回生效工艺",
    )
    check(R.pick_baseline(db, PROD, PART) is None, "pick_baseline 认为它没有生效基线")
    cards = PC.build_cards(db, product=PROD)
    parts = [c["part"] for c in cards["cards"]]
    check(PART not in parts, f"工艺卡不印它（当前 {parts}）")
    check(
        cards["summary"].get("archived", 0) >= 1,
        f"它被计进「已停用」，不是被悄悄吞掉（archived={cards['summary'].get('archived')}）",
    )
    check(
        all(c["part"] != PART for c in cards["cards"]),
        "归档的不混进「未发布」的行列，它压根不出卡",
    )

    # ---------------------------------------------------------------- 4
    section("4 归档组合发不出去")
    combo = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == PART))
    try:
        CR.release_combo(db, combo, by="test")
        check(False, "release_combo 应拒绝归档组合")
    except ValueError as e:
        check("停用" in str(e), f"release_combo 拒绝：{e}")
    r = client.post("/api/combos/release", json={"product": PROD, "part": PART, "force": True})
    check(r.status_code == 400, f"台账发布接口 400（{r.status_code}）")
    check("停用" in r.json().get("detail", ""), f"报错说清了原因：{r.json().get('detail')}")

    draft = ProcessRoute(
        kind="route", product=PROD, part=PART, version=2, status="draft", created_by="test"
    )
    db.add(draft)
    db.flush()
    db.add(
        ProcessRouteStep(
            route_id=draft.id, seq=10, process_name="返工", process_code="返工", unit_hours=1.0
        )
    )
    db.commit()
    try:
        R.publish(db, draft, published_by="test")
        check(False, "编排页发布新草稿应被拒绝")
    except ValueError as e:
        check("停用" in str(e), f"编排页发布被拒：{e}")
    db.rollback()
    db.expire_all()
    check(status_of(db, PART) == "archived", "组合没被这次发布尝试改动")
    check(
        db.scalar(select(ProcessRoute).where(ProcessRoute.id == draft.id)).status == "draft",
        "那条草稿也没被偷偷改成已发布",
    )

    # ---------------------------------------------------------------- 5
    section("5 归档组合挂不上线")
    task = WorkTask(task_no="ZZARCH-1", title="停用验证单", status="active")
    db.add(task)
    db.flush()
    db.commit()
    out = T.add_routes(
        db, task, [{"product": PROD, "part": PART}], created_by="test", create_baseline=True
    )
    db.commit()
    check(out["added"] == 0, f"一条都没挂上（added={out['added']}）")
    fails = out.get("failed") or []
    check(bool(fails) and "停用" in fails[0]["reason"], f"失败理由点名停用：{fails[:1]}")
    check(
        not db.scalars(
            select(TaskRoute).where(TaskRoute.task_id == task.id, TaskRoute.status == "active")
        ).all(),
        "任务上没有留下引用",
    )
    n_routes = len(
        db.scalars(select(ProcessRoute).where(ProcessRoute.product == PROD, ProcessRoute.part == PART)).all()
    )
    check(n_routes == 2, f"没有偷偷造基线（该组合路线仍是 2 条：已发布 + 草稿，实际 {n_routes}）")

    # ---------------------------------------------------------------- 6
    section("6 启用")
    r = client.post("/api/combos/active", json={"product": PROD, "part": PART, "active": True})
    check(r.json().get("status") == "released", f"路线还在发布态 → 回已发布（{r.json().get('status')}）")
    check((PROD, PART) in R.released_index(db), "重新进入识别索引")
    db.expire_all()
    check(
        PART in [c["part"] for c in PC.build_cards(db, product=PROD)["cards"]],
        "工艺卡又能印了",
    )

    # 停用 → 把路线改成草稿 → 启用：不该回到「已发布」
    client.post("/api/combos/active", json={"product": PROD, "part": PART, "active": False})
    db.expire_all()
    route.status = "draft"
    db.add(route)
    db.commit()
    r = client.post("/api/combos/active", json={"product": PROD, "part": PART, "active": True})
    check(r.json().get("status") == "draft", f"路线已不是发布态 → 回草稿（{r.json().get('status')}）")
    db.expire_all()
    c = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == PART))
    check(c.route_id is None, "route_id 已清空，不留「草稿却挂着生效路线」的鬼数据")
    check((PROD, PART) not in R.released_index(db), "回草稿后不在索引里")

    # ---------------------------------------------------------------- 7
    section("7 留痕")
    logs = db.scalars(
        select(AuditLog).where(AuditLog.entity == "combo", AuditLog.field == "status")
    ).all()
    mine = [a for a in logs if a.entity_id == c.id]
    check(len(mine) >= 4, f"停用/启用/发版都留了痕（{len(mine)} 条）")
    check(
        any(a.new_value == "archived" for a in mine) and any(a.old_value == "archived" for a in mine),
        "能追到「谁把哪条下了架、谁又启用的」",
    )
    before = len(mine)
    client.post("/api/combos/active", json={"product": PROD, "part": PART, "active": False})
    client.post("/api/combos/active", json={"product": PROD, "part": PART, "active": False})
    db.expire_all()
    mine2 = [
        a
        for a in db.scalars(
            select(AuditLog).where(AuditLog.entity == "combo", AuditLog.field == "status")
        ).all()
        if a.entity_id == c.id
    ]
    check(len(mine2) == before + 1, f"重复停用不刷记录（{len(mine2)} 条，只多 1 条）")

    print(f"\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for x in FAIL:
            print(f"  FAIL {x}")
    cleanup()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
