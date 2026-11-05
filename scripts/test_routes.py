"""工艺路线生成与发布逻辑的测试。

覆盖：
1. 生成的工序集合与工时，必须与 standard_hour 逐条对得上（不许多、不许少、不许错值）
2. 顺序：无真单据时按工艺默认序；有真单据时按真单据 seq
3. 发布/升版/归档的状态机
4. 准备工时分摊：批量为 0 时必须等价于纯单件工时（不能除零）

跑法：.venv/Scripts/python.exe scripts/test_routes.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import ProcessRoute, StandardHour, WorkItem  # noqa: E402
from app.services import routes as R  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def main() -> int:
    init_db()
    db = SessionLocal()
    pidx = R.process_index(db)

    # ---------------------------------------------------- 1. 与标准工时对齐
    # 建议工序有两个来源：standard_hour（历史实测）与真单据明细（WorkItem）。
    # 所以口径是「标准工时里的每道工序都在、工时一致」，多出来的必须能由真单据解释。
    print("\n[1] 生成结果与 standard_hour / 真单据明细对齐")
    combos: dict[tuple[str, str], list] = {}
    for h in db.scalars(select(StandardHour).where(StandardHour.active)).all():
        combos.setdefault((h.product or "", h.part or ""), []).append(h)

    def sheet_codes(product: str, part: str) -> set[str]:
        """真单据里出现过的工序 code（建议工序的第二个来源）。"""
        out: set[str] = set()
        for it in db.scalars(
            select(WorkItem).where(
                WorkItem.product_name == product, WorkItem.part_name == part
            )
        ).all():
            for cand in (it.process_code, it.process_std, it.process, it.process_raw):
                info = pidx.get(cand) or pidx.get((cand or "").strip())
                if info:
                    out.add(info["code"])
                    break
        return out

    checked = 0
    missing: list = []
    changed: list = []
    extra: list = []
    for key, rows in combos.items():
        if not key[1]:
            continue
        # 标准工时侧：同一道工序取样本最多的那条
        std_map: dict[str, tuple[float, int]] = {}
        for h in rows:
            info = pidx.get(h.process) or pidx.get((h.process or "").strip())
            code = info["code"] if info else (h.process or "").strip()
            if not code:
                continue
            prev = std_map.get(code)
            if prev is None or (h.samples or 0) > prev[1]:
                std_map[code] = (float(h.unit_hours or 0.0), int(h.samples or 0))
        steps = R.suggest_steps(db, key[0], key[1])
        got_map = {s["process_code"]: (round(s["unit_hours"], 4), s["samples"]) for s in steps}

        for code, (hours, _samples) in std_map.items():
            if code not in got_map:
                missing.append((key, code, hours))
            elif abs(got_map[code][0] - hours) > 1e-6:
                changed.append((key, code, hours, got_map[code][0]))

        allowed_extra = sheet_codes(key[0], key[1])
        for code in got_map:
            if code not in std_map and code not in allowed_extra:
                extra.append((key, code, got_map[code][0]))
        checked += 1

    check(missing == [], f"{checked} 个组合：标准工时的工序全部出现在建议里")
    check(changed == [], f"{checked} 个组合：单件工时与 standard_hour 逐条一致")
    check(extra == [], "建议里多出的工序都能由真单据明细解释")
    for label, rows_bad in (("缺失", missing), ("工时不一致", changed), ("来源不明", extra)):
        for item in rows_bad[:3]:
            print(f"       {label}: {item}")

    # ---------------------------------------------------- 2. 顺序
    print("\n[2] 顺序推断")
    # 2.1 无真单据的组合：应按 default_seq 递增
    ok_seq = True
    for key, rows in combos.items():
        if not key[1]:
            continue
        has_sheet = db.execute(
            select(StandardHour.id).where(
                StandardHour.product == key[0], StandardHour.part == key[1]
            )
        ).first()
        steps = R.suggest_steps(db, key[0], key[1])
        seqs = [pidx.get(s["process_code"], {}).get("default_seq", 999) for s in steps]
        if any(s["order_source"] == "sheet" for s in steps):
            continue  # 有真单据顺序的走 2.2
        if seqs != sorted(seqs):
            ok_seq = False
            print(f"       顺序不对 {key}: {seqs}")
            break
    check(ok_seq, "无真单据的组合按工艺默认序递增")

    # 2.2 有真单据的组合：按真单据 seq 排列
    from app.models import Sheet as _Sheet, WorkItem as _WI  # noqa: E402

    sheet_pairs = db.execute(select(_WI.product_name, _WI.part_name).distinct()).all()
    ok_sheet = True
    checked_sheet = 0
    for product, part in sheet_pairs:
        if not part:
            continue
        steps = R.suggest_steps(db, product or "", part or "")
        real = []
        for i, _s in db.execute(
            select(_WI, _Sheet)
            .join(_Sheet, _WI.sheet_id == _Sheet.id)
            .where(_WI.product_name == product, _WI.part_name == part)
            .order_by(_Sheet.id, _WI.seq)
        ).all():
            code = i.process_code or i.process_std or i.process
            if code and code not in real:
                real.append(code)
        got = [s["process_code"] for s in steps]
        head = got[: len(real)]
        if head != real:
            ok_sheet = False
            print(f"       真单据顺序未生效 {product}/{part}: 真单 {real} 生成 {got}")
            break
        checked_sheet += 1
    check(ok_sheet, f"{checked_sheet} 个有真单据的组合按真单据 seq 排列")

    # ---------------------------------------------------- 3. 状态机
    print("\n[3] 发布 / 升版 / 归档")
    route = R.create_draft(
        db, "测试产品", "测试件名", steps=R.suggest_steps(db, "SJ2栓剂", "主动轴")
    )
    db.commit()
    check(route.version == 1, "新建路线版本号为 1")
    check(route.status == "draft", "新建路线状态为 draft")

    R.publish(db, route)
    db.commit()
    check(route.status == "released", "发布后状态为 released")

    idx = R.released_index(db)
    check(("测试产品", "测试件名") in idx, "已发布路线进入索引")

    # 重复发布同一版应被拒
    dup_err = False
    try:
        R.publish(db, route)
    except ValueError:
        dup_err = True
    db.rollback()
    check(dup_err, "重复发布同一版本被拒绝")

    # 升版：生成**可编辑的新草稿**，旧版继续作为基准（发布新版时才归档旧版）
    route = db.scalars(
        select(ProcessRoute).where(
            ProcessRoute.product == "测试产品", ProcessRoute.part == "测试件名"
        ).order_by(ProcessRoute.version)
    ).all()
    check(len(route) == 1, "升版前只有 1 条路线")
    v2 = R.publish(db, route[0], new_version=True)
    db.commit()
    allr = db.scalars(
        select(ProcessRoute).where(
            ProcessRoute.product == "测试产品", ProcessRoute.part == "测试件名"
        ).order_by(ProcessRoute.version)
    ).all()
    check(len(allr) == 2, "升版后有 2 条路线")
    check(allr[0].status == "released", "升版后旧版仍为已发布（基准不断档）")
    check(allr[1].status == "draft", "升版产生的是可编辑草稿")
    check(allr[1].version == 2, "新版版本号为 2")
    check(v2.id == allr[1].id, "升版返回的是新草稿")

    # 新版发布后，旧版才归档
    R.publish(db, allr[1])
    db.commit()
    db.refresh(allr[0])
    check(allr[1].status == "released", "新版发布后为已发布")
    check(allr[0].status == "archived", "新版发布后旧版自动归档")

    # 撤销归档：归档不是终点，必须能退回来
    R.restore(db, allr[0])
    db.commit()
    check(allr[0].status == "draft", "归档版本可恢复为草稿")

    # 恢复成「已发布」时不能出现第二条基准
    R.publish(db, allr[0])
    db.commit()
    db.refresh(allr[1])
    check(allr[0].status == "released", "旧版恢复后重新发布")
    check(allr[1].status == "archived", "发布时其它已发布版自动归档")
    conflict = False
    try:
        R.restore(db, allr[1], to="released")
    except ValueError:
        conflict = True
    db.rollback()
    check(conflict, "已有基准时恢复为已发布被拒绝")

    # 已发布路线不能直接改：撤回发布后才行
    R.reopen(db, allr[0])
    db.commit()
    check(allr[0].status == "draft", "撤回发布回到草稿，可就地改")
    R.publish(db, allr[0])
    db.commit()

    # 已发布路线不能直接改
    edit_err = False
    try:
        R.save_steps(db, allr[0], [])
    except ValueError:
        edit_err = True
    db.rollback()
    check(edit_err, "已发布路线不能直接修改")

    # 删除非草稿应被拒（已发布是基准；归档版允许删，草稿允许删）
    del_err = False
    try:
        R.delete_route(db, allr[0])
    except ValueError:
        del_err = True
    db.rollback()
    check(del_err, "不能删除已发布路线")

    # ---------------------------------------------------- 4. 工时计算
    print("\n[4] 工时计算")
    r2 = R.create_draft(
        db,
        "算例产品",
        "算例件名",
        steps=[
            {
                "process_code": "CHE",
                "process_name": "车",
                "unit_hours": 1.75,
                "setup_hours": 0.5,
            },
            {
                "process_code": "XI",
                "process_name": "铣",
                "unit_hours": 0.5833,
                "setup_hours": 0.0,
            },
        ],
    )
    db.commit()
    out = R.route_out(r2)
    check(abs(out["unit_sum"] - 2.3333) < 0.001, f"单件合计 = {out['unit_sum']}")
    check(abs(out["per_unit_hours"] - 2.3333) < 0.001, "批量为 0 时准备工时不摊（等价纯单件）")

    r2.batch_qty = 10
    db.add(r2)
    db.commit()
    out = R.route_out(r2)
    check(abs(out["per_unit_hours"] - (2.3333 + 0.05)) < 0.001, f"批量 10 时单件 = {out['per_unit_hours']}")

    # ---------------------------------------------------- 5. 覆盖率
    print("\n[5] 覆盖率统计")
    cov = R.coverage(db)
    check(cov["total"] > 0, f"待建清单有 {cov['total']} 条")
    check(cov["items"][0]["value"] >= cov["items"][-1]["value"], "清单按价值降序")

    # ---------------------------------------------------- 清理
    # 用服务层删：只有草稿能删，已发布/已归档先改草稿再删。
    # 不走 db.query(...).delete()——批量删除绕过 ORM 的 delete-orphan 级联。
    for r in db.query(ProcessRoute).filter(
        ProcessRoute.product.in_(["测试产品", "算例产品"])
    ).all():
        r.status = "draft"
        db.add(r)
        db.flush()
        R.delete_route(db, r)
    db.commit()
    left = db.query(ProcessRoute).filter(
        ProcessRoute.product.in_(["测试产品", "算例产品"])
    ).count()
    check(left == 0, f"清理后无残留路线（剩 {left} 条）")
    from app.models import ProcessRouteStep  # noqa: E402

    steps_left = db.query(ProcessRouteStep).filter(
        ProcessRouteStep.route_id.in_(
            db.query(ProcessRoute.id).filter(
                ProcessRoute.product.in_(["测试产品", "算例产品"])
            )
        )
    ).count()
    check(steps_left == 0, f"清理后无残留工序行（剩 {steps_left} 条）")

    print("\n" + "=" * 56)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
