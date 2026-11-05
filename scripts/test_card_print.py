"""工艺卡打印（= 加工路线单版式）的回归。

用法：python scripts/test_card_print.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

为什么单独一个脚本：`test_process_card.py` 管「出哪几张卡、编号怎么排」，
这里管「卡上的格子填得对不对」——同一张纸换了个版式，最容易错的不是业务判断，
而是「本批件数印成了 1」「小计漏了准备工时」「表头件号没带编号」这类映射。

七组：
  [1] 版面映射：工序 → 明细行，单件用 2ʰ/30' 写法，合计按件数算
  [2] 表头：件号带编号、类别写层级、加工件数按本批口径
  [3] 本批件数三种来源：task_route 数量 / 投产台数×用量 / 单台
  [4] 分页：超过 10 道工序自动续页，PDF 页数 = 各卡页数之和
  [5] 加工路线单同步：已有的单据统计进卡片，材料从最近一张单取
  [6] 只印已发布 + 任务清单只列挂着路线的任务
  [7] 工序备注要印出去：短的排备注格第二行，长的挪到工序名下面
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import select, text  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Batch,
    Image,
    Period,
    ProcessRoute,
    ProcessRouteStep,
    ProductNode,
    Sheet,
    TaskRoute,
    WorkCombo,
    WorkItem,
    WorkTask,
)
from app.render import route_form as form  # noqa: E402
from app.services import card_print as CP  # noqa: E402
from app.services import combo_registry as CR  # noqa: E402
from app.services import process_card as PC  # noqa: E402

PROD = "ZZ卡印产品"
YM = "2099-07"
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


# ------------------------------------------------------------------ 造数


def seed(db, part: str, processes: list[tuple[str, float]], *, setup: float = 0.5, **kw) -> ProcessRoute:
    route = ProcessRoute(
        kind="route", product=PROD, part=part, version=1, status="draft",
        created_by="test", source="manual",
    )
    db.add(route)
    db.flush()
    for i, (name, hours) in enumerate(processes, start=1):
        db.add(
            ProcessRouteStep(
                route_id=route.id, seq=i * 10, process_name=name,
                process_code=name, unit_hours=hours, setup_hours=setup,
            )
        )
    db.flush()
    combo = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == part))
    # 结构与工艺已分表：qty_per_unit 是结构字段，写组合上没人读
    struct = {k: kw.pop(k) for k in ("qty_per_unit", "parent_id", "seq") if k in kw}
    if combo is None:
        combo = WorkCombo(product=PROD, part=part, status="draft", note="test", **kw)
        db.add(combo)
    else:
        for k, v in kw.items():
            setattr(combo, k, v)
    node = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == part))
    if node is None:
        node = ProductNode(product=PROD, part=part, level=1, seq=100, source="auto")
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
        db.execute(text("DELETE FROM work_item WHERE sheet_id IN "
                        "(SELECT id FROM sheet WHERE header_json LIKE :p)"), {"p": f"%{PROD}%"})
        db.execute(text("DELETE FROM sheet WHERE header_json LIKE :p"), {"p": f"%{PROD}%"})
        db.execute(text("DELETE FROM image WHERE filename LIKE :p"), {"p": "ZZCARD%"})
        db.execute(text("DELETE FROM batch WHERE name LIKE :p"), {"p": "ZZCARD%"})
        tids = [
            t.id for t in db.scalars(select(WorkTask).where(WorkTask.task_no.like("ZZCARD%"))).all()
        ]
        for tid in tids:
            db.execute(text("DELETE FROM task_route WHERE task_id = :t"), {"t": tid})
            db.execute(text("DELETE FROM work_task WHERE id = :t"), {"t": tid})
        rids = [
            r.id for r in db.scalars(select(ProcessRoute).where(ProcessRoute.product == PROD)).all()
        ]
        for rid in rids:
            db.execute(text("DELETE FROM process_route_step WHERE route_id = :r"), {"r": rid})
            db.execute(text("DELETE FROM process_route WHERE id = :r"), {"r": rid})
        db.execute(text("DELETE FROM product_node WHERE product = :p"), {"p": PROD})
        db.execute(text("DELETE FROM work_combo WHERE product = :p"), {"p": PROD})
        db.execute(text("DELETE FROM period WHERE ym = :y"), {"y": YM})
        db.commit()
    finally:
        db.close()


def main() -> int:
    init_db()
    cleanup()
    db = SessionLocal()
    try:
        # 整机装配 2 道（6h + 0.5h），料仓组件 2 道（2h + 1h），机架横梁 11 道（0.1h×11）
        seed(db, "整机装配", [("装配", 6.0), ("调试", 0.5)])
        seed(db, "料仓组件", [("焊接", 2.0), ("检验", 1.0)], qty_per_unit=2.0)
        seed(db, "机架横梁", [(f"工序{i}", 0.1) for i in range(1, 12)], qty_per_unit=4.0)
        db.commit()
        db.expire_all()

        # 铺层级：料仓组件、机架横梁都装在整机装配下（层级现在只存在结构表里）
        CR.layout_levels(db, PROD)
        root = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "整机装配"))
        tank = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "料仓组件"))
        beam = db.scalar(select(ProductNode).where(ProductNode.product == PROD, ProductNode.part == "机架横梁"))
        for c in (tank, beam):
            c.parent_id = root.id
            c.source = "manual"
        db.commit()
        db.expire_all()

        for name in ("整机装配", "料仓组件", "机架横梁"):
            c = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == name))
            CR.release_combo(db, c, by="tester", note="卡印测试")
        db.commit()
        db.expire_all()

        # ---------------------------------------------------------- 1
        section("1 版面映射：工序 → 明细行")
        res = PC.build_cards(db, product=PROD)
        card = next(c for c in res["cards"] if c["part"] == "整机装配")
        v = CP.card_values(card)
        check(len(v["rows"]) == 2, f"两道工序就是两行（{len(v['rows'])}）")
        check(v["rows"][0]["process"] == "装配", "工序名进「工序等级」栏")
        check(v["rows"][0]["unit_hours"] == "6ʰ", f"单件工时用车间写法（{v['rows'][0]['unit_hours']}）")
        check(
            v["rows"][0]["declared_total"] == "6:00:00",
            f"合计按件数算（{v['rows'][0]['declared_total']}）",
        )
        check(v["rows"][0]["worker_raw"] == "", "操作者留白：这是车间要填的，预印就是编数据")
        check(v["rows"][0]["qualified_qty"] == "", "加工结果留白")
        check(v["rows"][0]["inspector"] == "", "检查员留白")
        # 6 + 0.5 = 6.5h，加上每道 0.5h 准备工时共 1.0h → 7.5h
        expect = (6.0 + 0.5) * 1 + 1.0
        h, m = divmod(int(round(expect * 60)), 60)
        check(v["subtotal"] == f"{h}:{m:02d}:00", f"小计含准备工时（{v['subtotal']}，应为 {h}:{m:02d}:00）")

        # ---------------------------------------------------------- 2
        section("2 表头：编号 / 层级 / 加工件数")
        head = v["header"]
        check(head["part_no"] == "01 整机装配", f"件号带编号（{head['part_no']}）")
        check(head["part_no@2"] == "整机装配", "第二个件号格写件名（原单现场就这么填）")
        check(head["category"] == "总装", f"类别写层级（{head['category']}）")
        check(head["product_name"] == PROD, "产品名称")
        check(head["machine_count"] == "", "没挂任务时不印投产台数（印 0 会被当成零台）")
        check(head["part_count"] == "1", f"没挂任务按每台用量（{head['part_count']}）")

        sub = PC.build_cards(db, product=PROD, part="料仓组件", include_children=False)
        c_tank = sub["cards"][0]
        vh = CP.card_values(c_tank)["header"]
        check(vh["category"] == "部件", f"部件卡的类别是「部件」（{vh['category']}）")
        check(vh["category@2"] == "装在 01", f"部件卡写明装在谁下面（{vh['category@2']}）")
        check(vh["part_no"].startswith("01-01"), f"部件卡编号（{vh['part_no']}）")

        # ---------------------------------------------------------- 3
        section("3 本批件数三种来源")
        # 造任务：10 台，料仓组件本单写 20 件（不按用量，按单子）
        period = db.scalar(select(Period).where(Period.ym == YM))
        if period is None:
            period = Period(ym=YM, title="卡印测试期", status="open")
            db.add(period)
            db.flush()
        task = WorkTask(
            task_no="ZZCARD-1", title="卡印测试单", product=PROD,
            qty=10, period_id=period.id, status="open", created_by="tester",
        )
        db.add(task)
        db.commit()
        for part, qty in (("整机装配", 0.0), ("料仓组件", 20.0), ("机架横梁", 0.0)):
            combo = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == part))
            route = db.scalar(
                select(ProcessRoute).where(ProcessRoute.product == PROD, ProcessRoute.part == part)
            )
            db.add(
                TaskRoute(
                    task_id=task.id, route_id=route.id, origin="baseline",
                    product=PROD, part=part, qty=qty, status="active",
                )
            )
            combo.route_id = route.id
        db.commit()
        db.expire_all()

        tres = PC.build_cards(db, product=PROD, task_id=task.id)
        by_part = {c["part"]: c for c in tres["cards"]}
        check(by_part["整机装配"]["print_qty"] == 10, f"投产台数 × 用量 = 10（{by_part['整机装配']['print_qty']}）")
        check(by_part["整机装配"]["qty_src"] == "task", "件数来源标成 task")
        check(by_part["料仓组件"]["print_qty"] == 20, f"本单件名数量优先（{by_part['料仓组件']['print_qty']}）")
        check(by_part["料仓组件"]["qty_src"] == "route", "件数来源标成 route")
        check(by_part["机架横梁"]["print_qty"] == 40, f"10 台 × 每台 4 件 = 40（{by_part['机架横梁']['print_qty']}）")

        tv = CP.card_values(by_part["整机装配"], task=by_part["整机装配"].get("task"))
        check(tv["header"]["machine_count"] == "10", f"投产台数印 10（{tv['header']['machine_count']}）")
        check(tv["header"]["part_count"] == "10", f"加工件数印 10（{tv['header']['part_count']}）")
        check(
            tv["rows"][0]["declared_total"] == "60:00:00",
            f"合计 = 6h × 10 台（{tv['rows'][0]['declared_total']}）",
        )
        check(by_part["整机装配"]["batch_hours"] == round(6.5 * 10 + 1.0, 4), f"本批定额（{by_part['整机装配']['batch_hours']}）")

        # ---------------------------------------------------------- 4
        section("4 分页：工序多就续页")
        beam_card = by_part["机架横梁"]
        check(beam_card["step_count"] == 11, f"11 道工序（{beam_card['step_count']}）")
        check(beam_card["pages"] == 2, f"超过 10 行就要 2 页（{beam_card['pages']}）")
        pages = form.render_pages(CP.card_values(beam_card, task=beam_card.get("task")), dpi=72)
        check(len(pages) == 2, f"渲染出来确实 2 页（{len(pages)}）")
        check(tres["summary"]["pages"] == sum(c["pages"] for c in tres["cards"]), "摘要页数 = 各卡页数之和")
        pdf = CP.render_pdf(tres["cards"], task=tres.get("task"), dpi=72)
        check(pdf[:4] == b"%PDF", "PDF 头正常")
        check(pdf.count(b"/Type /Page") >= 4, f"整本 PDF 至少 4 页（{pdf.count(b'/Type /Page')}）")

        # ---------------------------------------------------------- 5
        section("5 加工路线单同步：已有的单据统计进卡片")
        batch = Batch(period_id=period.id, name="ZZCARD-批次")
        db.add(batch)
        db.flush()
        img = Image(
            batch_id=batch.id, filename="ZZCARD-1.jpg", path="data/none.jpg",
            sha256="ZZCARD1", status="done",
        )
        db.add(img)
        db.flush()
        sheet = Sheet(
            image_id=img.id, period_id=period.id, task_id=task.id,
            form_type="加工路线单", status="verified", total_hours=3.0,
            header_json='{"product_name": "%s", "batch_no": "B1", "part_name": "整机装配"}' % PROD,
            verify_user="tester",
        )
        db.add(sheet)
        db.flush()
        db.add(
            WorkItem(
                sheet_id=sheet.id, seq=1, product_name=PROD, part_name="整机装配",
                process="装配", process_std="装配", unit_hours=3.0, total_hours=3.0,
                qualified_qty=1.0, material="Q235", inspector="杜文杰",
            )
        )
        db.commit()
        db.expire_all()

        res2 = PC.build_cards(db, product=PROD)
        c2 = next(c for c in res2["cards"] if c["part"] == "整机装配")
        check(c2["sheets"]["count"] == 1, f"这张卡已经有 1 张加工路线单（{c2['sheets']['count']}）")
        check(c2["sheets"]["hours"] == 3.0, f"实报工时 3h（{c2['sheets']['hours']}）")
        check(c2["sheets"]["ids"] == [sheet.id], "能点回那张单")
        check(c2["material"] == "Q235", f"材料从最近一张单取（{c2['material']}）")
        v2 = CP.card_values(c2)
        check(v2["header"]["material"] == "Q235", "材料进「材料」格")
        check(res2["summary"]["sheet_count"] == 1, "摘要统计已录单据数")
        # 左栏也要标出来：扫一眼就知道哪些卡还得下发
        node = next(n for n in res2["tree"] if n["part"] == "整机装配")
        check(node["sheet_count"] == 1, f"左栏标「已报」（{node['sheet_count']}）")

        # ---------------------------------------------------------- 6
        section("6 任务清单与「只印已发布」")
        items = PC.tasks(db)
        mine = next((t for t in items if t["id"] == task.id), None)
        check(mine is not None, "任务清单里有这张测试单")
        check(mine["released"] == 3, f"3 件都能出卡（{mine['released'] if mine else 0}）")
        check(all(t["routes"] for t in items), "没有件名的任务不列入（点进去只会看到空页面）")

        combo = db.scalar(select(WorkCombo).where(WorkCombo.product == PROD, WorkCombo.part == "料仓组件"))
        # 这单在制、正引用着它，正常会被拦（那是另一条测试守的闸）；
        # 这里测的是「撤回后卡片消失」，所以显式 force 过去
        CR.withdraw_combo(db, combo, by="tester", reason="测试撤回", force=True)
        db.commit()
        db.expire_all()
        res3 = PC.build_cards(db, product=PROD, task_id=task.id)
        check(
            "料仓组件" not in [c["part"] for c in res3["cards"]],
            "撤回的组合不出卡",
        )
        miss = {m["part"]: m["reason"] for m in res3["missing"]}
        check("料仓组件" in miss, f"但如实报在 missing 里（{miss.get('料仓组件')}）")
        check(
            "未发布" in (miss.get("料仓组件") or ""),
            "理由是「工艺未发布」，不是含糊的「缺工艺」",
        )

        # ---------------------------------------------------------- 7
        section("7 工序备注要印出去（以前只印外协/检验，备注整条丢掉）")
        res4 = PC.build_cards(db, product=PROD)
        card4 = next(c for c in res4["cards"] if c["part"] == "整机装配")
        card4["steps"][0]["remark"] = "测试备注"
        card4["steps"][0]["inspect_required"] = True
        card4["steps"][1]["remark"] = "注意装夹方向，勿压伤密封面，先试装一件再批量"
        card4["steps"][1]["op_type"] = "outsource"
        v4 = CP.card_values(card4)
        r0, r1 = v4["rows"][0], v4["rows"][1]
        check(r0["remark"] == "检验", f"标记占备注格第一行（{r0['remark']}）")
        check(r0["remark2"] == "测试备注", f"短备注排第二行，不丢（{r0['remark2']}）")
        check(r1["remark"] == "外协", "长备注时备注格只留标记，不硬塞")
        check(
            r1.get("process_note", "").startswith("注意装夹方向"),
            f"整条备注挪到工序名下面（{r1.get('process_note', '')}）",
        )
        check(r1["process"] == "调试", "工序名仍在第一行")

        # 系统核对标记不是人手写的备注，不能印到纸上
        card4["steps"][0]["remark"] = "疑似非工序：历史统计里这个名称不在工序字典内"
        v5 = CP.card_values(card4)
        check(
            v5["rows"][0]["remark"] == "检验" and not v5["rows"][0]["remark2"],
            "「疑似非工序」不当作备注印（那是核对标记）",
        )
        check(len(form.render_pages(v4, dpi=72)) == 1, "带备注的版面照常渲染，不报错")
    finally:
        db.close()
        cleanup()

    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  - " + f)
    print("=" * 60)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
