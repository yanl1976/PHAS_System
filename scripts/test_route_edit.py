"""任务内编辑工艺：副本就地改、基线升版同步。

这两条路径的差别是本模块最容易做错的地方：
- 副本只属一个订单，改它不该惊动任何别的任务；
- 基线是全厂标准，改它必须留痕（升版），而且所有引用者要一起换到新版，
  否则旧版被归档后，别的任务还指着一个「已归档」的路线，等于悄悄脱钩。

用法：.venv\\Scripts\\python.exe scripts/test_route_edit.py
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
from app.models import ProcessRoute, ProcessRouteStep, TaskRoute, WorkTask  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
PREFIX = "ZZEDIT"
PRODUCT = "ZZEDIT产品"
PART = "件A"


def check(ok: bool, label: str) -> None:
    if ok:
        PASSED.append(label)
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def step(seq: int, name: str, unit: float, setup: float = 0.0) -> dict:
    return {
        "seq": seq,
        "process_code": name,
        "process_name": name,
        "op_type": "self",
        "category": "机加工",
        "work_center": "",
        "setup_hours": setup,
        "unit_hours": unit,
        "is_key": False,
        "inspect_required": False,
        "sop": "",
        "remark": "",
    }


def cleanup(db) -> None:
    """只清本测试自己造的数据，绝不碰真实任务与真实基线。"""
    for t in db.scalars(select(WorkTask).where(WorkTask.task_no.like(f"{PREFIX}%"))).all():
        for tr in db.scalars(select(TaskRoute).where(TaskRoute.task_id == t.id)).all():
            route = db.get(ProcessRoute, tr.route_id)
            db.delete(tr)
            db.flush()
            if route is not None and route.kind == "task":
                db.query(ProcessRouteStep).filter(
                    ProcessRouteStep.route_id == route.id
                ).delete(synchronize_session=False)
                db.delete(route)
        db.delete(t)
        db.flush()
    for r in db.scalars(
        select(ProcessRoute).where(ProcessRoute.product == PRODUCT)
    ).all():
        db.query(ProcessRouteStep).filter(ProcessRouteStep.route_id == r.id).delete(
            synchronize_session=False
        )
        db.delete(r)
    db.commit()


def main() -> int:
    client = TestClient(app)
    with SessionLocal() as db:
        cleanup(db)
        db.commit()

    print("\n[1] 准备：建一条基线 + 两个引用它的订单")
    r = client.post(
        "/api/routes",
        json={
            "product": PRODUCT,
            "part": PART,
            "kind": "route",
            "title": "测试基线",
            "steps": [step(10, "车", 1.0), step(20, "铣", 2.0), step(30, "磨", 0.5)],
        },
    )
    check(r.status_code == 200, f"基线草稿已建（{r.status_code}）")
    base_id = r.json()["id"]
    r = client.post(f"/api/routes/{base_id}/publish")
    check(r.status_code == 200 and r.json()["status"] == "released", "基线已发布 v1")

    task_ids = []
    for name in ("甲", "乙"):
        r = client.post(
            "/api/tasks",
            json={
                "task_no": f"{PREFIX}-{name}",
                "title": f"测试订单{name}",
                "product": PRODUCT,
                "qty": 10,
                "routes": [{"product": PRODUCT, "part": PART, "route_id": base_id, "qty": 10}],
            },
        )
        check(r.status_code == 200, f"任务{name}已建（{r.status_code}）")
        task_ids.append(r.json()["task"]["id"])

    d1 = client.get(f"/api/tasks/{task_ids[0]}").json()
    tr1 = d1["routes"][0]
    check(tr1["origin"] == "baseline", "任务甲引用基线")
    check(abs(d1["summary"]["unit_hours"] - 3.5) < 1e-6, f"单件合计 3.5h（实际 {d1['summary']['unit_hours']}）")

    print("\n[2] 改基线：升版 + 同步所有引用")
    new_steps = [step(10, "车", 1.2), step(20, "铣", 2.0), step(30, "磨", 0.5), step(40, "钻", 0.3)]
    r = client.put(
        f"/api/tasks/{task_ids[0]}/routes/{tr1['id']}/steps",
        json={"steps": new_steps, "user": "tester"},
    )
    check(r.status_code == 200, f"编辑接口可用（{r.status_code}）")
    body = r.json()
    check(body.get("mode") == "baseline", "走的是基线升版路径")
    check(body.get("version") == 2, f"版本升到 v2（实际 v{body.get('version')}）")
    check(body.get("synced_tasks") == 2, f"两个订单一起换到新版（实际 {body.get('synced_tasks')}）")
    check(abs(body["route"]["unit_sum"] - 4.0) < 1e-6, f"新单件合计 4.0h（实际 {body['route']['unit_sum']}）")

    with SessionLocal() as db:
        old = db.get(ProcessRoute, base_id)
        check(old.status == "archived", "旧版已归档留痕")
        new_id = body["route"]["id"]
        for tid in task_ids:
            tr = db.scalars(select(TaskRoute).where(TaskRoute.task_id == tid)).first()
            check(tr.route_id == new_id, f"任务{tid} 的引用已指向新版")

    d2 = client.get(f"/api/tasks/{task_ids[1]}").json()
    check(abs(d2["summary"]["unit_hours"] - 4.0) < 1e-6, f"订单乙同步到 4.0h（实际 {d2['summary']['unit_hours']}）")
    check(d2["routes"][0]["step_count"] == 4, "订单乙看到 4 道工序")

    print("\n[3] 原样再存一次：不该白造一个版本")
    r = client.put(
        f"/api/tasks/{task_ids[0]}/routes/{tr1['id']}/steps",
        json={"steps": new_steps, "user": "tester"},
    )
    body = r.json()
    check(r.status_code == 200, "重复保存可以正常返回")
    check(body.get("unchanged") is True, "识别为内容未变")
    check(body.get("version") == 2, f"版本没有继续升（实际 v{body.get('version')}）")

    print("\n[4] 副本：只改自己，不动标准")
    r = client.post(f"/api/tasks/{task_ids[0]}/routes/{body['task']['routes'][0]['id']}/fork")
    check(r.status_code == 200, f"拆副本成功（{r.status_code}）")
    copy_id = r.json()["route_id"]

    d1 = client.get(f"/api/tasks/{task_ids[0]}").json()
    tr_copy = d1["routes"][0]
    check(tr_copy["origin"] == "copy", "任务甲该条路线已是副本")
    copy_steps = [step(10, "车", 1.2), step(20, "铣", 2.0)]
    r = client.put(
        f"/api/tasks/{task_ids[0]}/routes/{tr_copy['id']}/steps",
        json={"steps": copy_steps, "user": "tester"},
    )
    check(r.status_code == 200, f"副本可编辑（原本这条路是断的，{r.status_code}）")
    body = r.json()
    check(body.get("mode") == "copy", "走的是副本就地保存路径")
    check(body.get("synced_tasks") == 0, "没有同步任何别的订单")
    check(body["route"]["status"] == "released", "副本保存后仍是生效状态（不走发布）")
    check(body["route"]["step_count"] == 2, f"副本减到 2 道（实际 {body['route']['step_count']}）")

    base_id_now = body["task"]["routes"][0]["baseline_id"]
    baseline = client.get(f"/api/routes/{base_id_now}").json()
    check(baseline["version"] == 2, "基线版本没变")
    check(abs(baseline["unit_sum"] - 4.0) < 1e-6, f"基线工时没被副本影响（{baseline['unit_sum']}）")

    d2 = client.get(f"/api/tasks/{task_ids[1]}").json()
    check(abs(d2["summary"]["unit_hours"] - 4.0) < 1e-6, f"订单乙不受影响（{d2['summary']['unit_hours']}）")

    d1 = client.get(f"/api/tasks/{task_ids[0]}").json()
    row = d1["routes"][0]
    check(row["diff"] is not None and not row["diff"]["identical"], "抽屉里能看到副本与基线的差异")
    check(row["diff"]["delta_total"] < 0, f"差异是负数（少了工序）{row['diff']['delta_total']}")

    print("\n[5] 边界：副本可以清空，基线不许清空")
    r = client.put(
        f"/api/tasks/{task_ids[0]}/routes/{tr_copy['id']}/steps",
        json={"steps": [], "user": "tester"},
    )
    check(r.status_code == 200 and r.json()["route"]["step_count"] == 0, "副本可清空重编")

    r = client.put(
        f"/api/tasks/{task_ids[1]}/routes/{d2['routes'][0]['id']}/steps",
        json={"steps": [], "user": "tester"},
    )
    check(r.status_code == 400, f"基线清空被拒（{r.status_code}）")

    print("\n[6] 不存在的路线 / 别的任务的路线")
    r = client.put(
        f"/api/tasks/{task_ids[0]}/routes/999999/steps", json={"steps": new_steps}
    )
    check(r.status_code == 404, f"路线不存在返回 404（{r.status_code}）")
    r = client.put(
        f"/api/tasks/{task_ids[0]}/routes/{d2['routes'][0]['id']}/steps",
        json={"steps": new_steps},
    )
    check(r.status_code == 404, f"跨任务的路线不能改（{r.status_code}）")

    print("\n[7] 清理")
    with SessionLocal() as db:
        cleanup(db)
        left = db.scalars(
            select(WorkTask).where(WorkTask.task_no.like(f"{PREFIX}%"))
        ).all()
        check(len(left) == 0, "测试任务已清理")
        left = db.scalars(
            select(ProcessRoute).where(ProcessRoute.product == PRODUCT)
        ).all()
        check(len(left) == 0, "测试路线已清理")

    print("\n" + "=" * 56)
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 通过 {len(PASSED)} 项")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"全部通过（{len(PASSED)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
