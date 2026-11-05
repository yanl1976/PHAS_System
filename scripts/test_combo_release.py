"""工时组合的发布控制、部件层级与树结构的回归。

用法：python scripts/test_combo_release.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

七组：
  [1] 默认草稿：新建组合 / 建线都不会顺手把它发布
  [2] 发布闸：无路线、空工序、定额为 0、工序重复一律拒绝
  [3] 一步发布：组合 released + 路线 released + 识别索引同时生效
  [4] 撤回：原因必填；被在制任务引用时先拒绝，显式 force 才撤
  [5] 批量发布：预演不落库，真发只发「有路线有工序」的
  [6] 部件层级：跨产品/自环/成环都拒绝；人工定过的层级重铺时不丢
  [7] 树：产品 → 部件 → 零件；前缀分组是展示层，不落库也不改组合数
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    ProcessRoute,
    ProcessRouteStep,
    ProductNode,
    StandardHour,
    WorkCombo,
)
from app.services import combo_registry as CR  # noqa: E402
from app.services import routes as R  # noqa: E402

PROD = "ZZ组合产品"
PROD2 = "ZZ另一个产品"
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


def seed(db, product: str, part: str, processes: list[tuple[str, float]]) -> ProcessRoute:
    """造一条草稿基线。工序直接写库，不走接口——这里测的是发布，不是建线。"""
    route = ProcessRoute(
        kind="route",
        product=product,
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
            )
        )
    db.flush()
    if db.scalar(
        select(WorkCombo).where(WorkCombo.product == product, WorkCombo.part == part)
    ) is None:
        db.add(WorkCombo(product=product, part=part, status="draft", note="test"))
        db.flush()
    # 结构节点一起建：自动铺层、工艺卡展开都读 product_node，
    # 只造组合不造节点，这些件在结构里就是「不存在」
    if db.scalar(
        select(ProductNode).where(ProductNode.product == product, ProductNode.part == part)
    ) is None:
        db.add(ProductNode(product=product, part=part, level=0, seq=100, source="auto"))
        db.flush()
    return route


def cleanup() -> None:
    from app.models import TaskRoute, WorkTask

    db = SessionLocal()
    try:
        # 先拆引用：task_route 挂了 route 外键，直接删路线会撞 FK
        for tr in db.scalars(
            select(TaskRoute).where(TaskRoute.product.in_([PROD, PROD2]))
        ).all():
            db.delete(tr)
        for t in db.scalars(select(WorkTask).where(WorkTask.task_no.like("ZZTASK%"))).all():
            db.delete(t)
        db.flush()

        combos = db.scalars(
            select(WorkCombo).where(WorkCombo.product.in_([PROD, PROD2]))
        ).all()
        ids = [c.id for c in combos]
        nodes = db.scalars(
            select(ProductNode).where(ProductNode.product.in_([PROD, PROD2]))
        ).all()
        node_ids = [n.id for n in nodes]
        for r in db.scalars(
            select(ProcessRoute).where(ProcessRoute.product.in_([PROD, PROD2]))
        ).all():
            for st in db.scalars(
                select(ProcessRouteStep).where(ProcessRouteStep.route_id == r.id)
            ).all():
                db.delete(st)
            db.delete(r)
        for h in db.scalars(
            select(StandardHour).where(StandardHour.product.in_([PROD, PROD2]))
        ).all():
            db.delete(h)
        for n in nodes:
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
    from app.main import app

    client = TestClient(app)
    db = SessionLocal()

    # ---------------------------------------------------------------- 1
    section("1 默认草稿")
    r = client.post(
        "/api/combos",
        json={
            "product": PROD,
            "part": "件A",
            "steps": [{"process": "下料", "unit_hours": 1.0}],
        },
    )
    check(r.status_code == 200, f"新建组合成功（{r.status_code}）")
    c1 = db.scalar(select(WorkCombo).where(WorkCombo.part == "件A"))
    check(c1 is not None and c1.status == "draft", f"新建即草稿（{getattr(c1, 'status', None)}）")

    route_a = seed(db, PROD, "件A", [("下料", 1.0), ("车", 2.0)])
    db.commit()
    db.refresh(c1)
    check(c1.status == "draft", "建了路线也还是草稿，不会自动发布")
    idx = R.released_index(db)
    check((PROD, "件A") not in idx, "草稿组合不出现在识别索引里")

    # ---------------------------------------------------------------- 2
    section("2 发布闸")
    # 先登记身份（不建路线），才测得到「有组合但没工艺」这条闸
    client.post("/api/combos", json={"product": PROD, "part": "件B", "steps": []})
    r = client.post("/api/combos/release", json={"product": PROD, "part": "件B"})
    check(r.status_code == 400 and "没有建工艺路线" in r.text, f"无路线拒绝发布（{r.text[:40]}）")

    seed(db, PROD, "件C", [])
    db.commit()
    r = client.post("/api/combos/release", json={"product": PROD, "part": "件C"})
    check(r.status_code == 400 and "没有工序" in r.text, f"空路线拒绝发布（{r.text[:40]}）")

    seed(db, PROD, "件D", [("下料", 0.0), ("车", 0.0)])
    db.commit()
    r = client.post("/api/combos/release", json={"product": PROD, "part": "件D"})
    check(r.status_code == 400 and "定额没填" in r.text, f"定额为 0 拒绝发布（{r.text[:40]}）")

    seed(db, PROD, "件E", [("下料", 1.0), ("下料", 2.0)])
    db.commit()
    r = client.post("/api/combos/release", json={"product": PROD, "part": "件E"})
    check(r.status_code == 400 and "工序重复" in r.text, f"工序重复拒绝发布（{r.text[:40]}）")

    # 产品名为空：历史数据里就有 89 条，不能靠它发布出去
    empty = WorkCombo(product="", part="件F", status="draft")
    db.add(empty)
    db.flush()
    seed(db, "", "件F", [("下料", 1.0)])
    db.commit()
    r = client.post("/api/combos/release", json={"product": "", "part": "件F"})
    check(r.status_code == 400 and "产品名为空" in r.text, f"产品名为空拒绝发布（{r.text[:40]}）")

    # ---------------------------------------------------------------- 3
    section("3 一步发布")
    r = client.post(
        "/api/combos/release",
        json={"product": PROD, "part": "件A", "note": "工艺科确认"},
    )
    check(r.status_code == 200, f"发布成功（{r.status_code} {r.text[:60]}）")
    body = r.json()
    check(body["combo"]["status"] == "released", "组合状态变为已发布")

    db.expire_all()
    c1 = db.scalar(select(WorkCombo).where(WorkCombo.part == "件A"))
    route_a = db.get(ProcessRoute, route_a.id)
    check(c1.route_id == route_a.id, "组合记住了当前生效的那一版")
    check(route_a.status == "released", "路线同时置为已发布")
    check(c1.published_by != "", f"发布留痕有操作人（{c1.published_by}）")

    idx = R.released_index(db)
    check((PROD, "件A") in idx, "已发布组合进了识别索引")
    check(len(idx.get((PROD, "件A")) or []) == 2, "索引里的工序数正确（2 道）")

    r = client.post("/api/combos/release", json={"product": PROD, "part": "件A"})
    check(r.status_code == 400 and "已经是已发布" in r.text, "重复发布被拒")

    logs = db.scalars(
        select(AuditLog).where(
            AuditLog.entity == "combo", AuditLog.entity_id == c1.id, AuditLog.field == "status"
        )
    ).all()
    check(len(logs) == 1 and logs[0].old_value == "draft", "发布留痕记了 draft → released")

    # ---------------------------------------------------------------- 4
    section("4 撤回")
    r = client.post("/api/combos/withdraw", json={"product": PROD, "part": "件A", "reason": ""})
    check(r.status_code == 400 and "原因" in r.text, "撤回不填原因被拒")

    r = client.post(
        "/api/combos/withdraw",
        json={"product": PROD, "part": "件A", "reason": "定额重核"},
    )
    check(r.status_code == 200, f"无引用时撤回成功（{r.status_code}）")
    db.expire_all()
    c1 = db.scalar(select(WorkCombo).where(WorkCombo.part == "件A"))
    check(c1.status == "draft" and c1.demoted_from == "released", "撤回后回到草稿并留下降级痕迹")
    check((PROD, "件A") not in R.released_index(db), "撤回后立刻从识别索引消失")

    # 在制任务引用时：先拒，force 才放
    from app.models import TaskRoute, WorkTask

    client.post("/api/combos/release", json={"product": PROD, "part": "件A"})
    db.expire_all()
    c1 = db.scalar(select(WorkCombo).where(WorkCombo.part == "件A"))
    task = WorkTask(task_no="ZZTASK-1", title="测试单", status="active")
    db.add(task)
    db.flush()
    db.add(
        TaskRoute(
            task_id=task.id,
            route_id=c1.route_id,
            origin="baseline",
            product=PROD,
            part="件A",
            status="active",
        )
    )
    db.commit()

    r = client.post(
        "/api/combos/withdraw",
        json={"product": PROD, "part": "件A", "reason": "硬撤"},
    )
    check(r.status_code == 400 and "在制任务" in r.text, f"有在制引用拒绝撤回（{r.text[:50]}）")

    r = client.post(
        "/api/combos/withdraw",
        json={"product": PROD, "part": "件A", "reason": "硬撤", "force": True},
    )
    check(r.status_code == 200, f"显式 force 才撤回（{r.status_code}）")

    # ---------------------------------------------------------------- 5
    section("5 批量发布")
    seed(db, PROD, "件G", [("下料", 0.5)])
    seed(db, PROD, "件H", [])  # 空路线，批量时应被跳过
    db.commit()
    db.expire_all()
    before = {
        c.part: c.status
        for c in db.scalars(select(WorkCombo).where(WorkCombo.product == PROD)).all()
    }
    pre = client.post(
        "/api/combos/batch-release", json={"dry_run": True, "product": PROD, "limit": 50}
    ).json()
    db.expire_all()
    after = {
        c.part: c.status
        for c in db.scalars(select(WorkCombo).where(WorkCombo.product == PROD)).all()
    }
    check(before == after, "预演不改库")
    parts_ok = {x["part"] for x in pre["released"]}
    parts_skip = {x["part"] for x in pre["skipped"]}
    check("件G"in parts_ok, "预演把有工序的列进将发布")
    check("件H"in parts_skip, "预演把空路线列进跳过")

    res = client.post(
        "/api/combos/batch-release", json={"dry_run": False, "product": PROD, "limit": 50}
    ).json()
    db.expire_all()
    okc = db.scalar(select(WorkCombo).where(WorkCombo.part == "件G"))
    hc = db.scalar(select(WorkCombo).where(WorkCombo.part == "件H"))
    check(okc.status == "released", f"批量真发把件G 发出去了（{res['released']}）")
    check(hc.status == "draft", "空路线仍留草稿，没有被顺手发成假基准")

    # ---------------------------------------------------------------- 6
    section("6 部件层级")
    seed(db, PROD, "整机装配", [("总装", 1.0)])
    seed(db, PROD, "料仓组件", [("组焊", 1.0)])
    seed(db, PROD, "料仓水套", [("车", 1.0)])
    seed(db, PROD2, "别的件", [("车", 1.0)])
    db.commit()

    rows = CR.layout_levels(db, PROD)
    db.commit()
    db.expire_all()
    root = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "整机装配"))
    comp = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "料仓组件"))
    leaf = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "料仓水套"))
    check(root.level == 0 and root.parent_id is None, f"整机装配是根（L{root.level}）")
    check(comp.level == 1 and comp.parent_id == root.id, "组件挂到根下（部件层）")
    check(leaf.level == 2 and leaf.parent_id == comp.id, "料仓水套挂到料仓组件下（子件层）")
    check(rows["root"] == "整机装配", f"铺层级报告指出了根（{rows['root']}）")

    r = client.patch(
        "/api/combos/level",
        json={"product": PROD, "part": "料仓水套", "parent_part": "别的件"},
    )
    check(r.status_code == 404 or "同一个产品" in r.text, f"跨产品挂载被拒（{r.status_code}）")

    r = client.patch(
        "/api/combos/level",
        json={"product": PROD, "part": "料仓组件", "parent_part": "料仓水套"},
    )
    check(r.status_code == 400, f"成环被拒（{r.text[:40]}）")

    r = client.patch(
        "/api/combos/level",
        json={"product": PROD, "part": "料仓水套", "parent_part": "整机装配"},
    )
    check(r.status_code == 200, f"人工改层级成功（{r.status_code}）")
    db.expire_all()
    leaf = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "料仓水套"))
    check(leaf.source == "manual", "人工定过的层级打了 manual 标记")

    CR.layout_all(db)
    db.commit()
    db.expire_all()
    leaf = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "料仓水套"))
    check(leaf.parent_id == root.id, "重铺层级不会冲掉人工判断")

    # ---------------------------------------------------------------- 7
    section("7 树结构")
    tree = client.get("/api/combos/tree", params={"product": PROD}).json()
    prod = tree["products"][0]
    check(prod["virtual_root"] is False, "有整机装配的产品用真实根，不造假根")
    names = {n["part"] for n in prod["roots"]}
    check("整机装配"in names, "树上根节点是整机装配")

    seen: list[str] = []

    def walk(node):
        if node.get("id") is not None:
            seen.append(node["part"])
        for ch in node.get("children") or []:
            walk(ch)

    for n in prod["roots"]:
        walk(n)
    check(len(seen) == prod["total"], f"树上组合数与台账一致（{len(seen)}/{prod['total']}）")

    # 无总装件名的产品：虚拟根 + 前缀分组
    seed(db, PROD2, "下托辊轴", [("车", 1.0)])
    seed(db, PROD2, "下托辊轴承", [("车", 1.0)])
    seed(db, PROD2, "下托辊轴管", [("车", 1.0)])
    seed(db, PROD2, "散件", [("车", 1.0)])
    db.commit()
    CR.layout_levels(db, PROD2)
    db.commit()
    t2 = client.get("/api/combos/tree", params={"product": PROD2}).json()["products"][0]
    check(t2["virtual_root"] is True, "没有总装件名的产品用虚拟根（产品本身当根）")
    groups = [n for n in t2["roots"][0]["children"] if n.get("virtual")]
    check(len(groups) >= 1, f"散件按前缀聚成了分组（{len(groups)} 组）")
    g = groups[0] if groups else {}
    check(g.get("part", "").startswith("下托辊"), f"分组名取的是最长公共前缀（{g.get('part')}）")
    check(g.get("id") is None, "分组是展示层结构，没有组合 id")

    db.expire_all()
    n_combo = db.scalar(select(func.count()).select_from(WorkCombo).where(WorkCombo.product == PROD2))
    check(n_combo == 5, f"分组不落库、不增加组合数（{n_combo} 条）")

    # ---------------------------------------------------------------- 清理
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
