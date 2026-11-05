"""「删了工艺，结构却删不掉」的回归。

现场链路（用户报的）：
    新增件 → 编工艺 → 发布 → 撤回发布 → 删除工艺 → 删结构
                                   ↑ 卡在这里：提示「有工时组合（工艺身份）」

根因：`work_combo` 是工艺的**身份行**，删路线时没人管它。最后一版路线
删掉之后这一行还在、`status` 还挂着 draft，于是

* 树上显示「草稿」，看着像还有一份工艺；
* 删结构被「有工艺身份」拦住 —— 工艺明明已经删了。

钉住的规则：
  [1] 删掉最后一版工艺，且无历史工时/无任务引用 → 身份行一起收掉
  [2] 收掉之后这一件回到「结构里有、工艺没编」，结构可以正常删除
  [3] 有历史工时（standard_hour）→ 身份行留着，删结构仍然拦住（这是真引用）
  [4] 还有别的版本 → 身份行不动（那不叫空壳）
  [5] 历史遗留的空壳身份（没路线、没引用的残渣）→ 删结构时自愈
  [6] 已发布路线仍然删不掉（回归，别为了修这个把闸门拆了）
  [7] 每一条清理都留审计，别让身份行无声消失

用法：python scripts/test_route_delete_shell.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）
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
    ProductNode,
    StandardHour,
    WorkCombo,
)

PROD = "ZZ删工艺产品"
ROOT = "整机装配"
PART = "U盖板"
PART_STD = "有历史工时件"
PART_TWO = "两版工艺件"
PART_LEGACY = "历史空壳件"

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


def combo_of(db, part: str) -> WorkCombo | None:
    db.expire_all()
    return db.scalar(
        select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == part)
    )


def node_of(db, part: str) -> ProductNode | None:
    db.expire_all()
    return db.scalar(
        select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == part)
    )


def routes_of(db, part: str) -> list[ProcessRoute]:
    db.expire_all()
    return list(
        db.scalars(
            select(ProcessRoute).where(
                ProcessRoute.kind == "route",
                ProcessRoute.product == PROD,
                ProcessRoute.part == part,
            )
        ).all()
    )


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
        for r in db.scalars(select(ProcessRoute).where(ProcessRoute.product == PROD)).all():
            db.delete(r)
        for h in db.scalars(select(StandardHour).where(StandardHour.product == PROD)).all():
            db.delete(h)
        db.commit()
    finally:
        db.close()


def publish(client, route_id: int, part: str) -> None:
    r = client.post(f"/api/routes/{route_id}/publish", json={"new_version": False})
    assert r.status_code == 200, f"发布 {part} 失败：{r.text}"


def main() -> int:
    init_db()
    cleanup()
    from app.main import app

    client = TestClient(app)
    db = SessionLocal()

    # 结构：整机装配 → 四个件
    for part, qty in ((ROOT, 1.0), (PART, 2.0), (PART_STD, 1.0), (PART_TWO, 1.0), (PART_LEGACY, 1.0)):
        r = client.post(
            "/api/combos/node",
            json={
                "product": PROD,
                "part": part,
                "parent_part": "" if part == ROOT else ROOT,
                "qty_per_unit": qty,
            },
        )
        assert r.status_code == 200, f"建 {part} 失败：{r.text}"

    # ---------------------------------------------------------------- 1
    section("1 现场链路：新增 → 编工艺 → 发布 → 撤回 → 删工艺")
    r = client.post(
        "/api/routes",
        json={
            "product": PROD,
            "part": PART,
            "steps": [{"seq": 10, "process_name": "铣", "unit_hours": 0.5}],
        },
    )
    check(r.status_code == 200, "编工艺：建草稿 200")
    route = r.json()
    # 草稿阶段还不登记身份：发布才是「这版工艺可以用了」的动作
    check(combo_of(db, PART) is None, "只建草稿时不登记工艺身份")

    publish(client, route["id"], PART)
    c = combo_of(db, PART)
    check(c is not None and c.status == "released", "发布后身份=已发布")
    check(c is not None and c.route_id == route["id"], "发布后身份指向这一版")

    r = client.post(
        "/api/combos/withdraw",
        json={"product": PROD, "part": PART, "reason": "工艺要重排"},
    )
    check(r.status_code == 200, "撤回发布 200")
    c = combo_of(db, PART)
    check(c is not None and c.status == "draft", "撤回后身份=草稿")
    check(c is not None and c.route_id is None, "撤回后不再指向任何版本")

    r = client.delete(f"/api/routes/{route['id']}")
    check(r.status_code == 200, "删除工艺 200")
    check(routes_of(db, PART) == [], "路线确实删干净了")
    check(combo_of(db, PART) is None, "空壳工艺身份跟着收掉了（这是修复的核心）")

    # ---------------------------------------------------------------- 2
    section("2 收掉身份后：结构正常可删")
    tree = client.get("/api/combos/tree", params={"product": PROD, "structure": 1}).json()
    prod = (tree.get("products") or [{}])[0]
    flat: dict[str, dict] = {}

    def walk(ns):
        for n in ns or []:
            flat[n["part"]] = n
            walk(n.get("children"))

    walk(prod.get("roots"))
    check(flat.get(PART, {}).get("status") == "none", "树上这件回到「无工艺」")
    check(flat.get(PART, {}).get("has_combo") is False, "树上不再声称有工艺身份")

    r = client.delete("/api/combos/node", params={"product": PROD, "part": PART})
    check(r.status_code == 200, f"删结构 200（{r.text[:80]}）")
    check(node_of(db, PART) is None, "结构节点已删除")

    # ---------------------------------------------------------------- 3
    section("3 有历史工时：身份保留，删结构仍拦住")
    r = client.post(
        "/api/routes",
        json={
            "product": PROD,
            "part": PART_STD,
            "steps": [{"seq": 10, "process_name": "车", "unit_hours": 1.0}],
        },
    )
    route_std = r.json()
    publish(client, route_std["id"], PART_STD)
    client.post(
        "/api/combos/withdraw",
        json={"product": PROD, "part": PART_STD, "reason": "调整"},
    )
    db.add(
        StandardHour(
            product=PROD,
            part=PART_STD,
            process="车",
            unit_hours=1.0,
            samples=3,
            origin="import",
        )
    )
    db.commit()
    r = client.delete(f"/api/routes/{route_std['id']}")
    check(r.status_code == 200, "删工艺 200")
    check(combo_of(db, PART_STD) is not None, "有历史工时 → 身份保留，不当空壳删")

    r = client.delete("/api/combos/node", params={"product": PROD, "part": PART_STD})
    check(r.status_code == 400, "删结构被拦（有真实引用，该拦）")
    check("工艺身份" in r.text, f"理由说清是工艺身份：{r.json().get('detail', '')[:60]}")

    # ---------------------------------------------------------------- 4
    section("4 还有别的版本：不叫空壳")
    r = client.post(
        "/api/routes",
        json={"product": PROD, "part": PART_TWO, "steps": [{"seq": 10, "process_name": "钻"}]},
    )
    v1 = r.json()
    publish(client, v1["id"], PART_TWO)  # 先有一版生效的，身份才存在
    check(combo_of(db, PART_TWO) is not None, "发布 v1 后身份存在")
    # 升版：复制成新草稿 v2，v1 仍是核对基准
    v2 = client.post(f"/api/routes/{v1['id']}/copy").json()
    check(len(routes_of(db, PART_TWO)) >= 2, f"有两版路线（{len(routes_of(db, PART_TWO))}）")
    r = client.delete(f"/api/routes/{v2['id']}")
    check(r.status_code == 200, "删掉 v2 草稿 200")
    c = combo_of(db, PART_TWO)
    check(c is not None, "身份不动（还有 v1 在）")
    check(c is not None and c.route_id == v1["id"], "生效版仍指向 v1，没被误清")

    # ---------------------------------------------------------------- 5
    section("5 历史遗留空壳：删结构时自愈")
    db.add(
        WorkCombo(
            product=PROD,
            part=PART_LEGACY,
            status="draft",
            note="旧版本删路线留下的残渣",
        )
    )
    db.commit()
    check(combo_of(db, PART_LEGACY) is not None, "先造一条空壳身份")
    r = client.delete("/api/combos/node", params={"product": PROD, "part": PART_LEGACY})
    check(r.status_code == 200, f"空壳不拦删结构（{r.text[:80]}）")
    check(combo_of(db, PART_LEGACY) is None, "空壳被顺带清掉")
    check(node_of(db, PART_LEGACY) is None, "节点已删除")

    # ---------------------------------------------------------------- 6
    section("6 已发布路线仍然删不掉")
    r = client.post(
        "/api/routes",
        json={"product": PROD, "part": ROOT, "steps": [{"seq": 10, "process_name": "装"}]},
    )
    rel = r.json()
    publish(client, rel["id"], ROOT)
    r = client.delete(f"/api/routes/{rel['id']}")
    check(r.status_code == 400, "已发布不能直接删")
    check("先撤回" in r.text or "撤回" in r.text, f"提示先撤回：{r.json().get('detail', '')[:40]}")
    r = client.delete("/api/combos/node", params={"product": PROD, "part": ROOT})
    check(r.status_code == 400, "有工艺的件结构仍不能删")

    # ---------------------------------------------------------------- 7
    section("7 审计留痕")
    n_shell = db.scalar(
        select(AuditLog.id)
        .where(AuditLog.entity == "combo", AuditLog.field == "combo:drop_shell")
        .limit(1)
    )
    check(n_shell is not None, "清空壳写了审计（combo:drop_shell）")

    cleanup()
    db.close()
    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
