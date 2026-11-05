"""路线复用测试：改一次基线，所有引用它的订单立刻同步。

这是「引用」与「复制」最本质的差别，也是复用唯一真正的价值：
标准工艺改了，车间所有在制订单按同一版执行，不需要逐个任务去改。
一旦某个订单 fork 出副本，它才从这条同步链上摘下来。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import ProcessRoute, ProcessRouteStep, TaskRoute, WorkTask  # noqa: E402
from app.main import app  # noqa: E402
from app.services import routes as R  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
PREFIX = "ZZREUSE"


def check(ok: bool, label: str) -> None:
    if ok:
        PASSED.append(label)
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def cleanup(db) -> None:
    for t in db.scalars(select(WorkTask).where(WorkTask.task_no.like(f"{PREFIX}%"))).all():
        db.query(TaskRoute).filter(TaskRoute.task_id == t.id).delete(synchronize_session=False)
        db.delete(t)
    db.flush()
    for r in db.scalars(select(ProcessRoute).where(ProcessRoute.kind == "task")).all():
        if db.query(TaskRoute).filter(TaskRoute.route_id == r.id).count() == 0:
            db.query(ProcessRouteStep).filter(ProcessRouteStep.route_id == r.id).delete(
                synchronize_session=False
            )
            db.delete(r)
    db.commit()


def main() -> int:
    client = TestClient(app)
    with SessionLocal() as db:
        cleanup(db)
        base = db.scalars(
            select(ProcessRoute)
            .where(ProcessRoute.kind == "route", ProcessRoute.part != "")
            .order_by(ProcessRoute.id)
        ).first()
        base_id, product, part = base.id, base.product, base.part
        steps = (
            db.query(ProcessRouteStep)
            .filter(ProcessRouteStep.route_id == base_id)
            .order_by(ProcessRouteStep.seq)
            .all()
        )
        # 「工时组合默认草稿」之后，任务挂线只认已发布工艺：
        # 测试要引用基线，就得自己先把这条基线发布出来。
        if base.status != "released":
            R.publish(db, base, published_by="test")
            db.commit()
        original = round(sum(float(s.unit_hours or 0) for s in steps), 4)
        first_old = float(steps[0].unit_hours or 0)
        check(len(steps) >= 1, f"基线 #{base_id} {product}/{part} 有 {len(steps)} 道工序")

    # ---- 两个订单同时引用这条基线
    print("\n[1] 两个订单引用同一条基线")
    a = client.post(
        "/api/tasks",
        json={"task_no": f"{PREFIX}-A", "product": product, "routes": [{"product": product, "part": part}]},
    ).json()["task"]
    b = client.post(
        "/api/tasks",
        json={"task_no": f"{PREFIX}-B", "product": product, "routes": [{"product": product, "part": part}]},
    ).json()["task"]
    check(a["routes"][0]["route_id"] == base_id == b["routes"][0]["route_id"], "两个订单指向同一基线")
    check(abs(a["routes"][0]["unit_hours"] - original) < 1e-6, f"单件工时 {original} 与基线一致")

    # ---- 改基线
    print("\n[2] 改基线，引用型任务立即同步")
    delta = 3.5
    with SessionLocal() as db:
        rows = (
            db.query(ProcessRouteStep)
            .filter(ProcessRouteStep.route_id == base_id)
            .order_by(ProcessRouteStep.seq)
            .all()
        )
        rows[0].unit_hours = first_old + delta
        db.commit()
    expect = round(original + delta, 4)

    da = client.get(f"/api/tasks/{a['id']}").json()
    db_ = client.get(f"/api/tasks/{b['id']}").json()
    check(abs(da["routes"][0]["unit_hours"] - expect) < 1e-6, f"A 同步为 {expect}")
    check(abs(db_["routes"][0]["unit_hours"] - expect) < 1e-6, f"B 同步为 {expect}")
    check(da["summary"]["unit_hours"] == expect, "A 的任务级单件工时同步")

    # ---- fork 之后脱钩
    print("\n[3] fork 之后不再跟随基线")
    fork = client.post(f"/api/tasks/{b['id']}/routes/{b['routes'][0]['id']}/fork").json()
    copy_id = fork["route_id"]
    with SessionLocal() as db:
        rows = (
            db.query(ProcessRouteStep)
            .filter(ProcessRouteStep.route_id == base_id)
            .order_by(ProcessRouteStep.seq)
            .all()
        )
        rows[0].unit_hours = first_old + 20.0
        db.commit()
    expect2 = round(original + 20.0, 4)
    da2 = client.get(f"/api/tasks/{a['id']}").json()
    db2 = client.get(f"/api/tasks/{b['id']}").json()
    check(abs(da2["routes"][0]["unit_hours"] - expect2) < 1e-6, "A 继续跟随基线")
    check(abs(db2["routes"][0]["unit_hours"] - expect) < 1e-6, "B 已脱钩，仍是 fork 时的口径")
    check(db2["routes"][0]["origin"] == "copy", "B 的路线标记为副本")

    # ---- 副本与基线的差异可读
    print("\n[4] 副本差异")
    diff = client.get(f"/api/tasks/{b['id']}/diff").json()
    check(diff["copy_count"] == 1, "差异接口只报副本")
    item = diff["items"][0]["diff"]
    check(item is not None and item["identical"] is False, "副本与基线不同")
    check(abs(item["delta_total"] - (expect - expect2)) < 1e-6, f"差额 {item['delta_total']}")

    # ---- 复原并清理
    with SessionLocal() as db:
        rows = (
            db.query(ProcessRouteStep)
            .filter(ProcessRouteStep.route_id == base_id)
            .order_by(ProcessRouteStep.seq)
            .all()
        )
        rows[0].unit_hours = first_old
        db.commit()
        after = round(
            sum(
                float(s.unit_hours or 0)
                for s in db.query(ProcessRouteStep)
                .filter(ProcessRouteStep.route_id == base_id)
                .all()
            ),
            4,
        )
        check(abs(after - original) < 1e-6, f"基线已复原为 {original}")

        cleanup(db)
        check(
            db.query(WorkTask).filter(WorkTask.task_no.like(f"{PREFIX}%")).count() == 0,
            "测试任务已清理",
        )
        check(db.get(ProcessRoute, copy_id) is None, "副本已清理")
        check(db.get(ProcessRoute, base_id) is not None, "基线仍在")

    print("\n" + "=" * 56)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print(f"全部通过（{len(PASSED)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
