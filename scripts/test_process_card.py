"""工艺卡（页面 + 打印）的回归。

用法：python scripts/test_process_card.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

六组：
  [1] 只印已发布：草稿不出卡，哪怕它挂着一条已发布路线
  [2] 编号跟着层级走：根 01 → 部件 01-01 → 子件 01-01-01
  [3] 三个工时口径：本层 / 含子件（按每台用量加权）/ 产品单台
  [4] 选一个部件：含子件出整棵子树，不含子件只出这一张
  [5] 撤回后卡片消失，编号重排不跳号
  [6] 脏数据兜底：没有生效路线 → 进 missing 不出空白卡；层级成环不死循环
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
    WorkCombo,
)
from app.services import combo_registry as CR  # noqa: E402
from app.services import process_card as PC  # noqa: E402

PROD = "ZZ工艺卡产品"
PROD2 = "ZZ工艺卡环产品"
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


def seed(db, product: str, part: str, processes: list[tuple[str, float]], **kw) -> ProcessRoute:
    """造一条草稿基线 + 一条草稿组合。工序直接写库，不经接口。"""
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
                setup_hours=0.5,
            )
        )
    db.flush()
    combo = db.scalar(
        select(WorkCombo).where(WorkCombo.product == product, WorkCombo.part == part)
    )
    # qty_per_unit 现在住在结构表里。seed 传进来的「每台几件」要落到 ProductNode，
    # 落到组合上是写了个没人读的字段，工艺卡还是按 1 算，测出来的结论就不对了
    struct = {k: kw.pop(k) for k in ("qty_per_unit", "parent_id", "seq") if k in kw}
    if combo is None:
        combo = WorkCombo(product=product, part=part, status="draft", note="test", **kw)
        db.add(combo)
        db.flush()
    else:
        for k, v in kw.items():
            setattr(combo, k, v)
        db.add(combo)
        db.flush()
    node = db.scalar(
        select(ProductNode).where(ProductNode.product == product, ProductNode.part == part)
    )
    if node is None:
        node = ProductNode(product=product, part=part, level=1, seq=100, source="auto")
        db.add(node)
        db.flush()
    for k, v in struct.items():
        setattr(node, k, v)
    db.add(node)
    db.flush()
    return route


def cleanup() -> None:
    db = SessionLocal()
    try:
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
        # 先删结构节点：它还指着父节点 id，留着会挡住下一轮的重新铺层
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


def codes_of(res: dict) -> dict[str, str]:
    return {c["part"]: c["code"] for c in res["cards"]}


def main() -> int:
    init_db()
    cleanup()
    from app.main import app

    client = TestClient(app)
    db = SessionLocal()

    # ---- 造一棵三层树：整机装配 → 机架组件 → 机架横梁；另一部件 电控柜 ----
    seed(db, PROD, "整机装配", [("总装", 4.0), ("调试", 1.0)])
    seed(db, PROD, "机架组件", [("焊接", 2.0)])
    seed(db, PROD, "机架横梁", [("下料", 0.5)])
    seed(db, PROD, "电控柜", [("接线", 1.0)])
    db.commit()
    # 走真实的自动铺层（整机装配=根、xx组件=部件、前缀命中的=子件），
    # 不手工骗 parent_id：工艺卡要证明的是「库里的层级能直接变成卡上的层级」
    laid = CR.layout_levels(db, PROD)
    db.commit()
    db.expire_all()

    # ---------------------------------------------------------------- 1
    section("1 只印已发布")
    res = PC.build_cards(db, product=PROD)
    check(res["product"] == PROD, f"按产品取卡（{res['product']}）")
    check(len(res["cards"]) == 0, f"全草稿时一张卡都不出（{len(res['cards'])}）")
    check(res["summary"]["draft"] == 4, f"未发布条数如实报出（{res['summary']['draft']}）")
    check(res["tree"] == [], "左栏树也是空的，不给人「有卡可打」的错觉")

    # 把草稿组合挂上一条「已发布」路线 —— 组合没发布就仍然不出卡
    root = db.scalar(select(WorkCombo).where(WorkCombo.part == "整机装配"))
    route = db.scalar(select(ProcessRoute).where(ProcessRoute.part == "整机装配"))
    route.status = "released"
    db.add(route)
    db.commit()
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    check(len(res["cards"]) == 0, "组合是草稿，路线已发布也不出卡（闸在组合上）")
    route.status = "draft"
    db.add(route)
    db.commit()

    # ---------------------------------------------------------------- 2
    section("2 发布后的编号与层级")
    check(laid["root"] == "整机装配", f"自动铺层先认出总装根（{laid['root']}）")
    for part in ("整机装配", "机架组件", "机架横梁", "电控柜"):
        c = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == part))
        CR.release_combo(db, c, by="test")
    db.commit()
    db.expire_all()

    res = PC.build_cards(db, product=PROD)
    codes = codes_of(res)
    check(len(res["cards"]) == 4, f"发布 4 条 → 4 张卡（{len(res['cards'])}）")
    check(codes.get("整机装配") == "01", f"总装是 01（{codes.get('整机装配')}）")
    check(
        codes.get("机架组件") == "01-01" and codes.get("机架横梁") == "01-01-01",
        f"部件与子件逐层展开（{codes.get('机架组件')} / {codes.get('机架横梁')}）",
    )
    check(codes.get("电控柜") == "01-02", f"同级部件顺序编号（{codes.get('电控柜')}）")
    check(len(set(codes.values())) == 4, "编号不重复")
    order = [c["part"] for c in res["cards"]]
    check(order == ["整机装配", "机架组件", "机架横梁", "电控柜"],
          f"卡片顺序 = 装订顺序（{order}）")

    root_card = next(c for c in res["cards"] if c["part"] == "整机装配")
    check(root_card["level_name"] == "总装", f"总装根层级名正确（{root_card['level_name']}）")
    check(root_card["child_count"] == 2, f"总装卡带 2 个下级（{root_card['child_count']}）")
    check(
        [x["part"] for x in root_card["children"]] == ["机架组件", "电控柜"],
        "下级部件按卡号顺序列出",
    )
    child_card = next(c for c in res["cards"] if c["part"] == "机架组件")
    check(child_card["level_name"] == "部件", f"中间层叫部件（{child_card['level_name']}）")
    check(child_card["parent_code"] == "01", f"卡头写上上级卡号（{child_card['parent_code']}）")
    grand = next(c for c in res["cards"] if c["part"] == "机架横梁")
    check(grand["parent_part"] == "机架组件", f"子件卡写上装在谁上面（{grand['parent_part']}）")

    # ---------------------------------------------------------------- 3
    section("3 三个工时口径")
    check(abs(root_card["own_hours"] - 5.0) < 1e-6, f"本层 = 自身工序合计（{root_card['own_hours']}）")
    check(
        abs(root_card["subtotal_hours"] - 8.5) < 1e-6,
        f"含子件 = 5 + 2 + 0.5 + 1（{root_card['subtotal_hours']}）",
    )
    check(
        abs(root_card["product_hours"] - 8.5) < 1e-6,
        f"产品单台 = 整机合计（{root_card['product_hours']}）",
    )
    check(abs(child_card["own_hours"] - 2.0) < 1e-6, "部件的本层只管自己那 2h")
    check(abs(child_card["subtotal_hours"] - 2.5) < 1e-6, f"部件的含子件 = 2 + 0.5（{child_card['subtotal_hours']}）")
    check(child_card["subtotal_hours"] < root_card["subtotal_hours"], "下级含子件不会大过上级")
    check(root_card["share_pct"] > 0, f"本层占整机比例（{root_card['share_pct']}%）")

    # 每台用量：横梁一台装 3 件，含子件要按 3 倍算（用量在结构表上）
    hb = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "机架横梁"))
    hb.qty_per_unit = 3
    db.add(hb)
    db.commit()
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    child_card = next(c for c in res["cards"] if c["part"] == "机架组件")
    root_card = next(c for c in res["cards"] if c["part"] == "整机装配")
    check(abs(child_card["subtotal_hours"] - 3.5) < 1e-6, f"用量 3 件 → 2 + 3×0.5（{child_card['subtotal_hours']}）")
    check(abs(root_card["subtotal_hours"] - 9.5) < 1e-6, f"整机跟着变（{root_card['subtotal_hours']}）")

    # ---------------------------------------------------------------- 4
    section("4 选一个部件")
    only = PC.build_cards(db, product=PROD, part="机架组件", include_children=False)
    check([c["part"] for c in only["cards"]] == ["机架组件"], "不含子件只出这一张")
    check(
        only["cards"][0]["subtotal_hours"] == child_card["subtotal_hours"],
        "单张卡的含子件仍是整棵子树，不会因为只打一张就变成本层",
    )
    with_kids = PC.build_cards(db, product=PROD, part="机架组件", include_children=True)
    check(
        [c["part"] for c in with_kids["cards"]] == ["机架组件", "机架横梁"],
        f"含子件出整棵子树（{[c['part'] for c in with_kids['cards']]}）",
    )
    check(
        with_kids["cards"][0]["code"] == "01-01",
        "只打一棵子树时编号仍取全产品口径，本子里的号与整本对得上",
    )
    check(with_kids["summary"]["printed"] == 2, "摘要按这批卡算，不是按整产品算")

    # ---------------------------------------------------------------- 5
    section("5 撤回后卡片消失、编号不跳号")
    cab = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == "电控柜"))
    CR.withdraw_combo(db, cab, by="test", reason="工艺要改")
    db.commit()
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    codes = codes_of(res)
    check("电控柜" not in codes, "撤回发布的组合不再出卡")
    check(len(res["cards"]) == 3, f"剩 3 张（{len(res['cards'])}）")
    check(sorted(codes.values()) == ["01", "01-01", "01-01-01"], f"编号连续不跳号（{sorted(codes.values())}）")
    idx = client.get("/api/cards", params={"product": PROD}).json()
    check(idx["summary"]["draft"] == 1, "接口把撤回的那条算进「未发布」")

    CR.release_combo(db, cab, by="test")
    db.commit()
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    check(codes_of(res).get("电控柜") == "01-02", "重新发布后编号回到 01-02")

    # ---------------------------------------------------------------- 6
    section("6 脏数据兜底")
    # 组合已发布、但它指向的路线被手工改回草稿 → 进 missing，不出空白卡
    weird = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == "电控柜"))
    r = db.scalar(select(ProcessRoute).where(ProcessRoute.part == "电控柜"))
    weird.status = "released"
    weird.route_id = r.id
    r.status = "draft"
    db.add_all([weird, r])
    db.commit()
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    check(len(res["cards"]) == 3, f"没有生效路线的组合不出卡（{len(res['cards'])}）")
    check(
        [x["part"] for x in res["missing"]] == ["电控柜"],
        f"但如实报在 missing 里（{res['missing']}）",
    )
    api = client.get("/api/cards", params={"product": PROD}).json()
    check(api["summary"]["missing"] == 1, "接口把缺工艺的条数暴露出来")
    r.status = "released"
    db.add(r)
    db.commit()

    # 层级成环：直接改库绕过校验，接口不能死循环，每张卡仍要有编号
    a = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "机架组件"))
    b = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "机架横梁"))
    b.parent_id = a.id
    a.parent_id = b.id
    db.add_all([a, b])
    db.commit()
    db.expire_all()
    res = PC.build_cards(db, product=PROD)
    check(len(res["cards"]) == 4, f"成环时仍然出齐 4 张卡（{len(res['cards'])}）")
    check(all(c["code"] for c in res["cards"]), "每张卡都有编号，没有 None")
    check(
        len({c["code"] for c in res["cards"]}) == len(res["cards"]),
        "成环时编号也不重复",
    )

    # 另一个产品：拆环，确认不影响别的产品
    seed(db, PROD2, "整机装配", [("总装", 1.0)])
    db.commit()
    res2 = PC.build_cards(db, product=PROD2)
    check(res2["summary"]["draft"] == 1 and not res2["cards"], "另一个产品的草稿同样不出卡")
    listed = client.get("/api/cards/products").json()
    check(
        any(x["product"] == PROD for x in listed["products"]),
        "产品清单接口能列出该产品",
    )

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
