"""任务层测试：订单级任务 / 工艺路线复用 / fork 副本 / 单据归集。

核心要验证的一句话：**路线是引用，不是复制**。
两个任务挂同一个件名，应当指向同一条基线；改基线两边一起变；
只有当某个任务 fork 出副本后，改副本才只影响那一单。

原则：真实数据只读，自己造的数据一律带 `ZZTEST` 前缀，结束时清干净。
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
from app.main import app  # noqa: E402
from app.models import ProcessRoute, ProcessRouteStep, Sheet, TaskRoute, WorkTask  # noqa: E402
from app.services import routes as R  # noqa: E402
from app.services import tasks as T  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
PREFIX = "ZZTEST"


def check(ok: bool, label: str) -> None:
    if ok:
        PASSED.append(label)
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def cleanup(db) -> None:
    """只删本测试造的数据：任务号带 ZZTEST 前缀，副本靠来源标记识别。"""
    for t in db.scalars(select(WorkTask).where(WorkTask.task_no.like(f"{PREFIX}%"))).all():
        db.query(TaskRoute).filter(TaskRoute.task_id == t.id).delete(synchronize_session=False)
        db.delete(t)
    db.flush()
    # 清掉没人引用的 fork 副本
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

    # ---------------------------------------------------- 1. 任务列表
    print("\n[1] 任务列表与摘要")
    d = client.get("/api/tasks", params={"size": 500}).json()
    real = [x for x in d["items"] if not x["task_no"].startswith(PREFIX)]
    check(len(real) >= 5, f"库里有 {len(real)} 个真实任务")
    s = d["summary"]
    check(s["task_count"] == d["total"], f"摘要任务数 {s['task_count']} 与总数一致")
    check(
        s["with_route"] + s["no_route"] == s["task_count"],
        f"已挂路线 {s['with_route']} + 待补 {s['no_route']} = 任务数",
    )
    check(
        abs(s["actual_hours"] - sum(x["actual_hours"] for x in d["items"])) < 1e-4,
        f"摘要实报 {s['actual_hours']} 等于各任务求和",
    )
    for x in d["items"]:
        for key in ("task_no", "route_count", "sheet_count", "actual_hours", "status"):
            check(key in x, f"{x['task_no']} 列表含 {key}")
        if x["route_count"]:
            check(x["step_count"] > 0, f"{x['task_no']} 有路线就有工序")

    # ---------------------------------------------------- 2. 订单级归集：多张单挂一个任务
    print("\n[2] 一个订单 = 一个任务（多张单归集）")
    with SessionLocal() as db:
        multi = (
            db.query(WorkTask)
            .filter(WorkTask.sheet_count > 1)
            .order_by(WorkTask.sheet_count.desc())
            .first()
        )
        check(multi is not None, "存在挂多张单据的任务")
        if multi is not None:
            sheets = db.scalars(select(Sheet).where(Sheet.task_id == multi.id)).all()
            check(
                len(sheets) == multi.sheet_count,
                f"{multi.task_no} 挂了 {len(sheets)} 张单据",
            )
            pair_tasks = {
                sh.task_id for sh in sheets
            }
            check(len(pair_tasks) == 1, "这些单据都指向同一个任务（没有一单一个任务）")

    # ---------------------------------------------------- 3. 复用：两个任务共用一条基线
    print("\n[3] 同一基线被多个任务引用")
    with SessionLocal() as db:
        sample_route = db.scalars(
            select(ProcessRoute)
            .where(ProcessRoute.kind == "route", ProcessRoute.part != "")
            .order_by(ProcessRoute.id)
        ).first()
        check(sample_route is not None, "取到一条基线用于复用测试")
        # 任务挂线只认已发布工艺，测试自己把基线发出来
        if sample_route.status != "released":
            R.publish(db, sample_route, published_by="test")
            db.commit()
        product, part, base_id = sample_route.product, sample_route.part, sample_route.id

    a = client.post(
        "/api/tasks",
        json={
            "task_no": f"{PREFIX}-A",
            "title": "复用测试A",
            "product": product,
            "batch_no": "T1",
            "routes": [{"product": product, "part": part, "batch_no": "T1", "qty": 1}],
        },
    )
    b = client.post(
        "/api/tasks",
        json={
            "task_no": f"{PREFIX}-B",
            "title": "复用测试B",
            "product": product,
            "batch_no": "T2",
            "routes": [{"product": product, "part": part, "batch_no": "T2", "qty": 2}],
        },
    )
    check(a.status_code == 200 and b.status_code == 200, "两个任务都建成功")
    ta, tb = a.json()["task"], b.json()["task"]
    ra, rb = ta["routes"][0], tb["routes"][0]
    check(ra["route_id"] == base_id and rb["route_id"] == base_id, "两条路线都指向同一条基线")
    check(ra["origin"] == "baseline" and rb["origin"] == "baseline", "两条都是引用（没有复制）")
    check(ra["unit_hours"] == rb["unit_hours"], "两条路线的单件工时一致（同一份标准）")

    with SessionLocal() as db:
        check(
            db.query(TaskRoute).filter(TaskRoute.route_id == base_id).count() >= 2,
            "基线被至少 2 个任务引用",
        )
        before_routes = db.query(ProcessRoute).count()

    cands = client.get(f"/api/tasks/{tb['id']}/candidates", params={"limit": 500}).json()
    target = [c for c in cands["items"] if c["route_id"] == base_id]
    check(bool(target), "候选清单里能找到这条基线")
    if target:
        check(target[0]["ref_count"] >= 2, f"候选显示已被引用 {target[0]['ref_count']} 次")
        check(target[0]["in_task"] is True, "候选正确标记「已在任务中」")

    # 重复添加不会产生第二条关联
    dup = client.post(
        f"/api/tasks/{tb['id']}/routes",
        json={"items": [{"product": product, "part": part, "route_id": base_id}]},
    ).json()
    check(dup["added"] == 0 and dup["reactivated"] == 0, "重复添加被拒绝")
    check(bool(dup["failed"]), "重复添加给出原因")

    # ---------------------------------------------------- 4. fork 副本
    print("\n[4] 拆副本：只影响本单")
    f = client.post(f"/api/tasks/{tb['id']}/routes/{rb['id']}/fork", params={"user": "zztest"})
    check(f.status_code == 200, "fork 成功")
    fj = f.json()
    copy_id = fj["route_id"]
    check(copy_id != base_id, f"副本是新路线 #{copy_id}")
    check(fj["from_route"] == base_id and fj["from_version"] == 1, "副本记录了来源基线与版本")

    with SessionLocal() as db:
        copy = db.get(ProcessRoute, copy_id)
        base = db.get(ProcessRoute, base_id)
        check(copy is not None and copy.kind == "task", "副本 kind=task")
        check(copy.task_id == tb["id"], "副本归属该任务")
        steps = (
            db.query(ProcessRouteStep)
            .filter(ProcessRouteStep.route_id == copy_id)
            .order_by(ProcessRouteStep.seq)
            .all()
        )
        base_steps = (
            db.query(ProcessRouteStep)
            .filter(ProcessRouteStep.route_id == base_id)
            .order_by(ProcessRouteStep.seq)
            .all()
        )
        check(len(steps) == len(base_steps), f"副本工序数 {len(steps)} 与基线一致")
        check(
            [s.process_name for s in steps] == [s.process_name for s in base_steps],
            "副本工序与基线一致",
        )

        # 改副本定额，基线不动
        old = float(base_steps[0].unit_hours or 0)
        steps[0].unit_hours = old + 7.25
        db.commit()
        db.expire_all()
        base_after = (
            db.query(ProcessRouteStep)
            .filter(ProcessRouteStep.route_id == base_id)
            .order_by(ProcessRouteStep.seq)
            .first()
        )
        check(
            abs(float(base_after.unit_hours or 0) - old) < 1e-9,
            f"改副本后基线仍是 {old}（未被污染）",
        )
        copy_after = (
            db.query(ProcessRouteStep)
            .filter(ProcessRouteStep.route_id == copy_id)
            .order_by(ProcessRouteStep.seq)
            .first()
        )
        check(
            abs(float(copy_after.unit_hours or 0) - (old + 7.25)) < 1e-9,
            "副本已改成本单口径",
        )
        after_routes = db.query(ProcessRoute).count()
    check(after_routes == before_routes + 1, "全库只多了 1 条副本，没有整份复制")

    da = client.get(f"/api/tasks/{ta['id']}").json()
    check(da["routes"][0]["origin"] == "baseline", "另一个任务仍然引用基线")
    check(da["routes"][0]["route_id"] == base_id, "另一个任务没被 fork 影响")
    db_ = client.get(f"/api/tasks/{tb['id']}").json()
    check(db_["routes"][0]["origin"] == "copy", "本任务已切换为副本")
    diff = client.get(f"/api/tasks/{tb['id']}/diff").json()
    check(diff["copy_count"] == 1, "差异接口只统计副本")

    # ---------------------------------------------------- 5. 移除
    print("\n[5] 移除路线")
    r = client.delete(f"/api/tasks/{tb['id']}/routes/{rb['id']}").json()
    check(r["removed"] is True, "移除成功")
    check(r["deleted_copy"] == copy_id, "副本被一并删除（引用计数归零才删）")
    with SessionLocal() as db:
        check(db.get(ProcessRoute, copy_id) is None, "副本确实不在了")
        check(db.get(ProcessRoute, base_id) is not None, "基线完好")
        check(
            db.query(TaskRoute).filter(TaskRoute.task_id == tb["id"], TaskRoute.status == "active").count()
            == 0,
            "任务下已无活跃路线",
        )
    check(client.get(f"/api/tasks/{tb['id']}").json()["summary"]["route_count"] == 0, "详情路线数归零")

    # 引用型移除不动基线
    client.post(
        f"/api/tasks/{tb['id']}/routes",
        json={"items": [{"product": product, "part": part, "route_id": base_id}]},
    )
    with SessionLocal() as db:
        trs = db.scalars(select(TaskRoute).where(TaskRoute.task_id == tb["id"], TaskRoute.status == "active")).all()
        tr_id = trs[0].id
    r2 = client.delete(f"/api/tasks/{tb['id']}/routes/{tr_id}").json()
    check(r2["removed"] is True and r2["deleted_copy"] is None, "引用型路线移除不删基线")
    with SessionLocal() as db:
        check(db.get(ProcessRoute, base_id) is not None, "引用移除后基线仍在")

    # ---------------------------------------------------- 6. 手工建任务 / 改状态
    print("\n[6] 手工建任务与状态")
    empty = client.post(
        "/api/tasks",
        json={"task_no": f"{PREFIX}-EMPTY", "title": "空任务", "product": product, "qty": 3},
    ).json()["task"]
    check(empty["summary"]["route_count"] == 0, "可以先建空任务、后补路线")
    check(empty["qty"] == 3, "订单数量按手填保留")
    patched = client.patch(f"/api/tasks/{empty['id']}", json={"status": "closed"}).json()
    check(patched["task"]["status"] == "closed", "状态可以改")
    check(client.get("/api/tasks", params={"status": "closed"}).json()["total"] >= 1, "按状态能筛出来")

    # 有单据的任务不能删
    with SessionLocal() as db:
        with_sheet = db.scalars(select(WorkTask).where(WorkTask.sheet_count > 0)).first()
        check(with_sheet is not None, "取到有单据的任务")
        ws_id = with_sheet.id if with_sheet else None
    if ws_id:
        rr = client.delete(f"/api/tasks/{ws_id}")
        check(rr.status_code == 400, "有单据的任务拒绝删除（400）")

    check(client.get("/api/tasks/999999").status_code == 404, "不存在的任务 404")
    check(
        client.get("/api/tasks", params={"q": "不存在的任务ZZZ"}).json()["total"] == 0,
        "搜不到返回空",
    )

    # ---------------------------------------------------- 7. 筛选
    print("\n[7] 缺失筛选")
    no_route = client.get("/api/tasks", params={"only_no_route": True, "size": 500}).json()
    check(no_route["total"] == no_route["summary"]["no_route"], "待补路线筛选与摘要一致")
    check(all(x["route_count"] == 0 for x in no_route["items"]), "筛选结果确实都没路线")
    no_sheet = client.get("/api/tasks", params={"only_no_sheet": True, "size": 500}).json()
    check(all(x["sheet_count"] == 0 for x in no_sheet["items"]), "无单据筛选结果确实都没单据")

    # ---------------------------------------------------- 清理
    with SessionLocal() as db:
        # 只检查「本测试造的任务」有没有留下副本。库里别人拆出来的副本是
        # 正经业务数据，不该因为跑一次测试就要求工作库干干净净。
        mine = [
            t.id
            for t in db.scalars(
                select(WorkTask).where(WorkTask.task_no.like(f"{PREFIX}%"))
            ).all()
        ]
        cleanup(db)
        left = db.query(WorkTask).filter(WorkTask.task_no.like(f"{PREFIX}%")).count()
        check(left == 0, "测试任务已清理")
        leaked = (
            db.query(ProcessRoute)
            .filter(ProcessRoute.kind == "task", ProcessRoute.task_id.in_(mine))
            .count()
            if mine
            else 0
        )
        check(leaked == 0, "测试没有留下副本")

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
