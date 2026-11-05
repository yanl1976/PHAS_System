"""S4 真实数据验证：建一条真路线，看告警是否按预期触发，且不影响其它单据。

流程：
1. 记录所有明细的告警基线（改代码后已有值）
2. 建一条真路线（YUJ-18BZ / 料包），发布
3. 触发重校验，看哪些单据多了/少了告警
4. 逐条核对：只有命中路线的单据才该有变化
5. 清理路线，验证回到基线
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models import ProcessRoute, WorkItem  # noqa: E402
from app.services import routes as R  # noqa: E402

FAILED: list[str] = []
PRODUCT = "YUJ-18BZ"
PART = "料包"


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def snapshot(db) -> dict:
    out = {}
    for it in db.query(WorkItem).order_by(WorkItem.sheet_id, WorkItem.seq).all():
        flags = json.loads(it.flags_json or "[]")
        out[f"{it.sheet_id}:{it.seq}"] = {
            "codes": [f["code"] for f in flags],
            "product": it.product_name or "",
            "part": it.part_name or "",
            "process": it.process or "",
            "process_code": it.process_code or "",
            "unit_hours": it.unit_hours,
        }
    return out


def main() -> int:
    db = SessionLocal()

    # P3 之后首期基线是「已发布」状态（只有已发布的才能被任务引用）。
    # 本测试发布新版会把同组合的原基线归档，所以先记下它，清理时原样恢复，
    # 否则跑一次测试就丢掉一个核对基准。
    prev = R.pick_baseline(db, PRODUCT, PART)
    prev_id = prev.id if prev else None
    prev_status = prev.status if prev else None
    released_before = (
        db.query(ProcessRoute)
        .filter(ProcessRoute.status == "released", ProcessRoute.kind == "route")
        .count()
    )

    # ---- 1. 建一条真路线：镗 3.0 / 铣 1.5 / 刨 4.0 / 车 0.5
    print("\n[1] 建真路线并发布")
    steps = R.suggest_steps(db, PRODUCT, PART)
    print(f"  候选 {len(steps)} 道：{[s['process_name'] for s in steps]}")
    # 故意把「镗」的定额设成 2.0（真实单据是 3.0/2.5），
    # 这样单据 1 的两行镗应该触发「工时偏离路线定额」
    for s in steps:
        if s["process_name"] == "镗":
            s["unit_hours"] = 2.0
    route = R.create_draft(db, PRODUCT, PART, steps=steps, source="history", created_by="s4")
    R.publish(db, route)
    db.commit()
    print(f"  已发布路线 id={route.id} v{route.version}")

    idx = R.released_index(db)
    key = (PRODUCT, PART)
    check(key in idx, "路线进入已发布索引")
    print(f"  路线工序：{[(s['process_name'], s['unit_hours']) for s in idx.get(key, [])]}")

    # ---- 2. 重校验并对比
    print("\n[2] 重校验对比")
    from app.api.sheets import _revalidate
    from app.models import Sheet

    before = snapshot(db)
    for sh in db.query(Sheet).all():
        _revalidate(db, sh)
    db.commit()
    after = snapshot(db)

    changed = []
    for k in before:
        if before[k]["codes"] != after[k]["codes"]:
            changed.append((k, before[k]["codes"], after[k]["codes"]))

    print(f"  告警有变化的明细：{len(changed)} 条")
    for k, b, a in changed:
        print(f"    {k} {b} → {a}")

    # 单据 1（镗 3.0 / 2.5，定额 2.0）：
# 3.0 → +50% 恰好到阈值，报；2.5 → +25% 在容差内，不报
    codes = after["1:1"]["codes"]
    check(
        "ROUTE_HOURS_DEVIATE" in codes,
        f"单据1 第1行（镗，实际 3.0，偏差 +50%）报工时偏离：{codes}",
    )
    check(
        "ROUTE_HOURS_DEVIATE" not in after["1:2"]["codes"],
        f"单据1 第2行（镗，实际 2.5，偏差 +25% 在容差内）不报：{after['1:2']['codes']}",
    )

    # 单据 3 seq1（车 0.5 == 定额 0.5）不该报
    check(
        "ROUTE_HOURS_DEVIATE" not in after["3:1"]["codes"],
        f"单据3 第1行（车 0.5，与定额相同）不报：{after['3:1']['codes']}",
    )
    # 单据 3 seq2（车 8.0 vs 0.5，超差 1500%）该报
    check(
        "ROUTE_HOURS_DEVIATE" in after["3:2"]["codes"],
        f"单据3 第2行（车 8.0 vs 定额 0.5）报工时偏离：{after['3:2']['codes']}",
    )

    # 不在路线组合里的单据（5/6/7）必须一条都不变
    for k in ("5:1", "6:1", "7:1"):
        check(
            before[k]["codes"] == after[k]["codes"],
            f"{k} 告警不变（该组合没路线）：{before[k]['codes']} → {after[k]['codes']}",
        )

    # 单据 4（产品已补录为 YUJ-18BZ / 料包）：现在能命中真路线。
    # 基线路线上是 镗/铣/刨/车，没有「电工」→ 起步时它会带一条「工序不在路线内」；
    # 本测试发布的路线里含电工，重校验后这条应被清掉，且不该冒出新告警。
    check(
        "ROUTE_STEP_EXTRA" not in after["4:1"]["codes"],
        f"4:1 命中含「电工」的路线后不报工序不在路线内："
        f"{before['4:1']['codes']} → {after['4:1']['codes']}",
    )
    extra4 = [c for c in after["4:1"]["codes"] if c not in before["4:1"]["codes"]]
    check(not extra4, f"4:1 没有新增意外告警（新增 {extra4}）")

    # ---- 3. 工序不在路线内：升版到只含车/镗的路线
    print("\n[3] 工序不在路线内")
    steps2 = [
        {"seq": 10, "process_code": "CHE", "process_name": "车", "unit_hours": 0.5},
        {"seq": 20, "process_code": "TANG", "process_name": "镗", "unit_hours": 2.0},
    ]
    # 已发布的路线不能直接改：先建草稿副本 → 改 → 再发布
    route2 = R.create_draft(
        db,
        PRODUCT,
        PART,
        steps=[R.step_out(s) for s in route.steps],
        source="history",
        created_by="s4",
    )
    R.save_steps(db, route2, steps2)
    R.publish(db, route2)
    db.commit()
    print(f"  已升版到 v{route2.version}，只含 车/镗")
    for sh in db.query(Sheet).all():
        _revalidate(db, sh)
    db.commit()
    after2 = snapshot(db)
    # 单据 2 的铣/刨不在新路线里
    check(
        "ROUTE_STEP_EXTRA" in after2["2:1"]["codes"],
        f"单据2 第1行（铣）报工序不在路线内：{after2['2:1']['codes']}",
    )
    check(
        "ROUTE_HOURS_DEVIATE" not in after2["2:1"]["codes"],
        "工序不在路线内时不再叠加工时偏离告警",
    )

    # ---- 4. 清理后回到基线
    print("\n[4] 清理后回到基线")
    # 只能删本测试自己建的（created_by='s4'）。
    # 这里必须点名 created_by，不能按产品名 to_clean：PRODUCT 用的是真实产品
    # YUJ-18BZ，而 P1 把历史写法（YUJ18BZ制丸机）归一成了 YUJ-18BZ，
    # 按产品名删会把首期 11 条草稿一起带走 —— 已经踩过一次。
    for r in db.query(ProcessRoute).filter(ProcessRoute.created_by == "s4").all():
        r.status = "draft"
        db.add(r)
        db.flush()
        db.delete(r)
    # 发布新版时同组合的其它版本可能被归档，清理后要复位成测试前的状态
    for r in (
        db.query(ProcessRoute)
        .filter(
            ProcessRoute.product == PRODUCT,
            ProcessRoute.part == PART,
            ProcessRoute.created_by != "s4",
        )
        .all()
    ):
        if r.id == prev_id and prev_status:
            r.status = prev_status
        elif r.status == "archived":
            r.status = "draft"
        db.add(r)
    db.flush()
    # 路线状态变了，组合的对外状态要跟着回去：本测试改的是别人家的基线，
    # 不能留下「组合已发布、路线已草稿」这种半拉子状态。
    for r in db.query(ProcessRoute).filter(
        ProcessRoute.product == PRODUCT, ProcessRoute.part == PART
    ).all():
        R._sync_combo(db, r)
    db.commit()
    for sh in db.query(Sheet).all():
        _revalidate(db, sh)
    db.commit()
    final = snapshot(db)
    diff = [k for k in before if before[k]["codes"] != final[k]["codes"]]
    check(not diff, f"清理后所有明细告警回到基线（差异 {diff}）")

    # 本测试只应影响自己建的路线，不该额外发布任何一条基线
    # 只数基线：任务工艺（kind='task'）建好即 released 是正常状态
    left_released = (
        db.query(ProcessRoute)
        .filter(ProcessRoute.status == "released", ProcessRoute.kind == "route")
        .count()
    )
    check(
        left_released == released_before,
        f"本测试没有额外发布路线（进入 {released_before} → 现在 {left_released}）",
    )

    db.close()
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
