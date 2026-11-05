"""升版提醒（引用漂移）验证。

这一组测的是「标准改了，引用它的人一定会知道」：

1. 首版发布不产生提醒（没人用过的东西不需要确认）；
2. 基线在某任务里被改并发布 → **别的任务**立刻出现「升版待确认」，
   改的人自己不留提醒（是他改的，已经知道）；
3. 任务列表 / 任务详情 / 待办 / 路线详情都要能给出这条提醒与原因；
4. 确认有两条路：采用新版（adopt）、本单保留现版（keep → 转副本，从此脱钩）；
5. 在编排页发布草稿（不重指引用）→ 引用方提示「仍按旧版，已有新版」；
6. 保存/发布动作的返回值里要带受影响任务清单，界面才能当场提醒。

用法：.venv\\Scripts\\python.exe scripts/test_version_notice.py
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
from app.models import ProcessRoute, ProcessRouteStep, RouteVersionNotice, TaskRoute, WorkTask  # noqa: E402
from app.services import routes as R  # noqa: E402
from app.services import tasks as T  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
PREFIX = "ZZVER"
PRODUCT = "ZZVER产品"
PART = "提醒件"


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
        db.query(TaskRoute).filter(TaskRoute.task_id == t.id).delete(synchronize_session=False)
        db.delete(t)
        db.flush()
    for r in db.scalars(select(ProcessRoute).where(ProcessRoute.product == PRODUCT)).all():
        db.query(ProcessRouteStep).filter(ProcessRouteStep.route_id == r.id).delete(
            synchronize_session=False
        )
        db.delete(r)
    db.query(RouteVersionNotice).filter(RouteVersionNotice.product == PRODUCT).delete(
        synchronize_session=False
    )
    db.commit()


def notices_of(client: TestClient, task_id: int) -> list[dict]:
    return client.get(f"/api/tasks/{task_id}/notices").json()["items"]


def main() -> int:
    client = TestClient(app)
    db = SessionLocal()
    cleanup(db)

    # ---------------------------------------------------------------- 准备
    print("\n[0] 准备：一条已发布基线 + 两个引用它的任务")
    base = R.create_draft(db, PRODUCT, PART, steps=[step(10, "车", 1.0)], created_by="test")
    db.commit()
    R.publish(db, base, published_by="tester")
    db.commit()
    check(base.status == "released", "基线 v1 已发布")

    tasks = {}
    for tag in ("A", "B"):
        r = client.post(
            "/api/tasks", json={"task_no": f"{PREFIX}-{tag}", "title": "提醒测试", "product": PRODUCT}
        )
        check(r.status_code == 200, f"任务 {tag} 已创建")
        tasks[tag] = r.json()["task"]
        resp = client.post(
            f"/api/tasks/{tasks[tag]['id']}/routes",
            json={"items": [{"product": PRODUCT, "part": PART}]},
        )
        check(resp.json()["added"] == 1, f"任务 {tag} 引用基线成功")

    check(not notices_of(client, tasks["A"]["id"]), "首版引用没有「待确认」提醒")
    check(not notices_of(client, tasks["B"]["id"]), "另一个任务同样没有提醒")

    tr_a = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == tasks["A"]["id"], TaskRoute.status == "active")
    ).first()
    check(tr_a.seen_route_id == tr_a.route_id, "引用时自动记录「已确认版本」")

    # ---------------------------------------------------------------- 1 升版
    print("\n[1] 在任务 A 里改基线并发布：A 不留提醒，B 必须亮起")
    resp = client.put(
        f"/api/tasks/{tasks['A']['id']}/routes/{tr_a.id}/steps",
        json={"steps": [step(10, "车", 1.5), step(20, "铣", 0.5)], "publish": True},
    )
    body = resp.json()
    check(resp.status_code == 200, "任务内保存并发布成功")
    check(body.get("version") == 2, f"基线升到 v2（实际 {body.get('version')}）")
    affected = body.get("affected_tasks") or []
    check(
        any(t["task_no"] == f"{PREFIX}-B" for t in affected),
        "响应里带出受影响任务清单（B）",
    )

    check(not notices_of(client, tasks["A"]["id"]), "改的人自己不留「待确认」")
    b_notices = notices_of(client, tasks["B"]["id"])
    check(len(b_notices) == 1, f"B 出现 1 条「升版待确认」（实际 {len(b_notices)}）")
    if b_notices:
        n = b_notices[0]
        check(n["reason"] == "unconfirmed", f"原因是「已切版未确认」（{n['reason']}）")
        notice = n.get("notice") or {}
        check(notice.get("from_version") == 1 and notice.get("to_version") == 2, "提醒带出 v1 → v2")
        check(abs((notice.get("unit_delta") or 0) - 1.0) < 1e-6, "提醒带出单件工时变化 +1.000h")
        check("升版" in n["message"] or "尚未确认" in n["message"], "提醒文案说清了发生了什么")
        check(n.get("ref_version") == 2, "提醒带出本单当前引用版本")

    detail = client.get(f"/api/tasks/{tasks['B']['id']}").json()
    check(detail["summary"]["notice_count"] == 1, "任务详情带 notice_count")
    check(len(detail["notices"]) == 1, "任务详情带 notices 清单")
    stale_rows = [r for r in detail["routes"] if r.get("stale")]
    check(len(stale_rows) == 1, "路线行带 stale 标记（界面逐行标红）")

    rows = client.get("/api/tasks", params={"q": f"{PREFIX}-B"}).json()["items"]
    check(rows and rows[0]["notice_count"] == 1, "任务列表行带 notice_count")
    check(rows and rows[0]["notice_msg"], "任务列表行带提醒文案")

    todos = client.get("/api/tasks/todos").json()
    keys = {i["key"]: i for i in todos["items"]}
    check("version_notice" in keys, "业务待办新增「基线升版待确认」")
    check(keys.get("version_notice", {}).get("count", 0) >= 1, "待办计数不为零")

    pub2 = client.get(f"/api/routes/{body['route']['id']}").json()
    check(pub2["ref_count"] == 2, "新版被 2 个任务引用")
    check(pub2["pending_count"] == 1, "路线详情能说出还有 1 个引用没确认")
    check(pub2["notice"]["to_version"] == 2, "路线详情带升版通知")

    # ---------------------------------------------------------------- 2 采用新版
    print("\n[2] B 确认采用新版：提醒清零，不再挂待办")
    tr_b = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == tasks["B"]["id"], TaskRoute.status == "active")
    ).first()
    resp = client.post(
        f"/api/tasks/{tasks['B']['id']}/ack", json={"tr_id": tr_b.id, "mode": "adopt"}
    )
    check(resp.status_code == 200, "确认接口可用")
    check(not notices_of(client, tasks["B"]["id"]), "确认后不再提醒")
    db.expire_all()
    tr_b = db.get(TaskRoute, tr_b.id)
    check(tr_b.seen_route_id == tr_b.route_id, "确认到具体版本（可追溯）")

    # ---------------------------------------------------------------- 3 保留现版
    print("\n[3] 再升一版后 B 选择「本单保留现版」：转成本单副本")
    tr_a = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == tasks["A"]["id"], TaskRoute.status == "active")
    ).first()
    old_unit = 2.0
    resp = client.put(
        f"/api/tasks/{tasks['A']['id']}/routes/{tr_a.id}/steps",
        json={"steps": [step(10, "车", 1.5), step(20, "铣", 1.0)], "publish": True},
    )
    check(resp.json().get("version") == 3, "基线升到 v3")
    check(len(notices_of(client, tasks["B"]["id"])) == 1, "B 再次出现待确认")

    tr_b = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == tasks["B"]["id"], TaskRoute.status == "active")
    ).first()
    resp = client.post(
        f"/api/tasks/{tasks['B']['id']}/ack", json={"tr_id": tr_b.id, "mode": "keep"}
    )
    body = resp.json()
    check(resp.status_code == 200 and body["acked"] == 1, "「保留现版」执行成功")
    db.expire_all()
    tr_b = db.get(TaskRoute, tr_b.id)
    check(tr_b.origin == "copy", "B 的这条路线变成副本（脱离基线）")
    copy_route = db.get(ProcessRoute, tr_b.route_id)
    check(copy_route.kind == "task", "副本类型 kind=task")
    check(
        abs(sum(s.unit_hours for s in copy_route.steps) - old_unit) < 1e-6,
        "副本固化的是旧版口径（1.5+0.5=2.0h）",
    )
    check(not notices_of(client, tasks["B"]["id"]), "转副本后不再被提醒")

    # 基线再升一版，B 的副本不应跟着变
    tr_a = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == tasks["A"]["id"], TaskRoute.status == "active")
    ).first()
    client.put(
        f"/api/tasks/{tasks['A']['id']}/routes/{tr_a.id}/steps",
        json={"steps": [step(10, "车", 2.0)], "publish": True},
    )
    db.expire_all()
    copy_route = db.get(ProcessRoute, db.get(TaskRoute, tr_b.id).route_id)
    check(
        abs(sum(s.unit_hours for s in copy_route.steps) - old_unit) < 1e-6,
        "基线再升版不影响已副本化的本单",
    )
    check(not notices_of(client, tasks["B"]["id"]), "副本不产生升版提醒")

    # ---------------------------------------------------------------- 4 编排页发布
    print("\n[4] 编排页发布草稿（不重指引用）→ 引用方提示「仍按旧版」")
    latest = R.pick_baseline(db, PRODUCT, PART)
    draft = R.create_draft(db, PRODUCT, PART, steps=[step(10, "车", 3.0)], created_by="test")
    db.commit()
    resp = client.post(f"/api/routes/{draft.id}/publish", json={"user": "tester"})
    body = resp.json()
    check(resp.status_code == 200, "编排页发布成功")
    check(body.get("affected_count", 0) >= 1, "发布响应带受影响任务数")
    check(
        any(t["task_no"] == f"{PREFIX}-A" for t in body.get("affected_tasks") or []),
        "发布响应列出仍在旧版上的任务",
    )
    check("升版待确认" in (body.get("hint") or ""), "发布响应给出提醒文案")

    a_notices = notices_of(client, tasks["A"]["id"])
    check(len(a_notices) == 1, "A 出现「仍按旧版」提醒")
    if a_notices:
        check(a_notices[0]["reason"] == "upgraded", f"原因是 upgraded（{a_notices[0]['reason']}）")
        check(
            a_notices[0].get("current_version") == body["version"],
            "提醒带出目前生效版本",
        )

    # 采用新版后引用指向最新版
    tr_a = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == tasks["A"]["id"], TaskRoute.status == "active")
    ).first()
    resp = client.post(
        f"/api/tasks/{tasks['A']['id']}/ack", json={"tr_id": tr_a.id, "mode": "adopt"}
    )
    check(resp.status_code == 200, "「采用新版」把引用切到现行基准")
    db.expire_all()
    tr_a = db.get(TaskRoute, tr_a.id)
    check(tr_a.route_id == body["id"], "引用已切到刚发布的版本")
    check(not notices_of(client, tasks["A"]["id"]), "确认后提醒消失")

    # ---------------------------------------------------------------- 5 一次性确认
    print("\n[5] 一键确认本任务全部待确认")
    resp = client.post(f"/api/tasks/{tasks['A']['id']}/ack", json={"mode": "adopt"})
    check(resp.status_code == 200, "不带 tr_id 时可全量确认")

    # ---------------------------------------------------------------- 6 单据侧
    print("\n[6] 单据详情带归属任务的提醒位")
    any_sheet = db.scalars(select(__import__("app.models", fromlist=["Sheet"]).Sheet).limit(1)).first()
    if any_sheet is not None:
        data = client.get(f"/api/sheets/{any_sheet.id}").json()
        check("task" in data, "单据详情预留 task 字段（含提醒条数）")
    else:
        check(True, "库里没有单据，跳过单据侧检查")

    # ---------------------------------------------------------------- 清理
    print("\n[7] 清理")
    cleanup(db)
    left = db.scalars(select(ProcessRoute).where(ProcessRoute.product == PRODUCT)).all()
    check(not left, "测试路线已清理")
    check(
        not db.scalars(
            select(RouteVersionNotice).where(RouteVersionNotice.product == PRODUCT)
        ).all(),
        "测试通知已清理",
    )

    print("\n" + "=" * 56)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for x in FAILED:
            print("  -", x)
    print(f"通过 {len(PASSED)} 项 / 共 {len(PASSED) + len(FAILED)} 项")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
