"""单据改挂任务（归属纠正）验证。

归集是**自动**的（同产品 + 同批次 + 同期间），自动就会归错：同规格两批活分给两个订单、
批次号抄错、补录时期间填错。这一组测的就是「归错之后能不能不改库把它改回来」：

1. 改挂要同时重算两个任务的单据数与实报工时（物化字段，不重算列表就骗人）；
2. 目标订单没有该件名的工艺引用时，必须当场说清「这张单没有定额可对照」；
3. 三种拒绝有业务理由：核算期锁定、目标订单已结案/取消、跨期未确认；
4. 解绑（摘下归属）要能撤销错误归集，并说明下次确认会重新自动归集；
5. 批量改挂逐张独立成败，一张被拒不能拖累其余；
6. 审计留痕必须记下「谁把哪张单从哪个订单挪到哪个订单」；
7. 单据详情/台账的任务归属与升版提醒随改挂同步。

用法：.venv\\Scripts\\python.exe scripts/test_sheet_task_move.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    Batch,
    Image,
    Period,
    ProcessRoute,
    ProcessRouteStep,
    RouteVersionNotice,
    Sheet,
    TaskRoute,
    WorkItem,
    WorkTask,
)
from app.services import routes as R  # noqa: E402
from app.services import tasks as T  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
PREFIX = "ZZMOV"
PRODUCT = "ZZMOV产品"
PART = "改挂件"
YM = "2031-03"
YM2 = "2031-04"


def check(ok: bool, label: str) -> None:
    if ok:
        PASSED.append(label)
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def step(seq: int, name: str, unit: float) -> dict:
    return {
        "seq": seq,
        "process_code": name,
        "process_name": name,
        "op_type": "self",
        "category": "机加工",
        "work_center": "",
        "setup_hours": 0.0,
        "unit_hours": unit,
        "is_key": False,
        "inspect_required": False,
        "sop": "",
        "remark": "",
    }


def cleanup(db) -> None:
    """只清本脚本造的数据。绝不动库里既有的单据 —— 这里的库是生产用的。

    用原生 SQL 而不是 ORM 删除：Period/Batch/Image 之间有 relationship，
    ORM 删 Period 时会去 UPDATE batch.period_id=NULL，撞 NOT NULL。
    """
    db.rollback()
    db.expire_all()
    sids = [
        s.id
        for s in db.scalars(select(Sheet)).all()
        if s.image is not None and (s.image.filename or "").startswith(PREFIX)
    ]
    for sid in sids:
        db.execute(text("DELETE FROM work_item WHERE sheet_id = :s"), {"s": sid})
        db.execute(text("DELETE FROM audit_log WHERE sheet_id = :s"), {"s": sid})
    for sid in sids:
        db.execute(text("DELETE FROM sheet WHERE id = :s"), {"s": sid})
    db.execute(text("DELETE FROM image WHERE filename LIKE :p"), {"p": f"{PREFIX}%"})
    db.execute(text("DELETE FROM batch WHERE name LIKE :p"), {"p": f"{PREFIX}%"})
    db.execute(text("DELETE FROM period WHERE ym IN (:a, :b)"), {"a": YM, "b": YM2})

    tsk = [t.id for t in db.scalars(select(WorkTask).where(WorkTask.task_no.like(f"{PREFIX}%"))).all()]
    for tid in tsk:
        db.execute(text("DELETE FROM task_route WHERE task_id = :t"), {"t": tid})
    for tid in tsk:
        db.execute(text("DELETE FROM work_task WHERE id = :t"), {"t": tid})
    rts = [r.id for r in db.scalars(select(ProcessRoute).where(ProcessRoute.product == PRODUCT)).all()]
    for rid in rts:
        db.execute(text("DELETE FROM process_route_step WHERE route_id = :r"), {"r": rid})
    for rid in rts:
        db.execute(text("DELETE FROM process_route WHERE id = :r"), {"r": rid})
    db.execute(text("DELETE FROM route_version_notice WHERE product = :p"), {"p": PRODUCT})
    db.commit()


def mk_period(db, ym: str, status: str = "open") -> Period:
    p = db.scalars(select(Period).where(Period.ym == ym)).first()
    if p is None:
        p = Period(ym=ym, title=f"{ym} 测试期", status=status)
        db.add(p)
        db.flush()
    else:
        p.status = status
    # 必须落盘：SQLite 下本会话持有写事务时，TestClient 的另一个连接会写不进来
    db.commit()
    return p


def mk_sheet(db, period: Period, tag: str, hours: float = 2.0) -> Sheet:
    batch = Batch(period_id=period.id, name=f"{PREFIX}-批次-{tag}")
    db.add(batch)
    db.flush()
    img = Image(
        batch_id=batch.id,
        filename=f"{PREFIX}-{tag}.jpg",
        path=f"data/none-{tag}.jpg",
        sha256=f"{PREFIX}{tag}",
        status="done",
    )
    db.add(img)
    db.flush()
    sh = Sheet(
        image_id=img.id,
        period_id=period.id,
        form_type="加工路线单",
        status="verified",
        header_json='{"product_name": "%s", "batch_no": "B-%s"}' % (PRODUCT, tag),
        total_hours=hours,
        verify_user="tester",
    )
    db.add(sh)
    db.flush()
    db.add(
        WorkItem(
            sheet_id=sh.id,
            seq=1,
            product_name=PRODUCT,
            batch_no=f"B-{tag}",
            part_name=PART,
            process="车",
            process_std="车",
            process_code="车",
            qualified_qty=1,
            unit_hours=hours,
            total_hours=hours,
        )
    )
    db.commit()
    return sh


def main() -> int:
    client = TestClient(app)
    db = SessionLocal()
    cleanup(db)

    # ---------------------------------------------------------------- 准备
    print("\n[0] 准备：已发布基线 + 任务 A（引用基线）+ 任务 B + 期间与单据")
    base = R.create_draft(db, PRODUCT, PART, steps=[step(10, "车", 1.0)], created_by="test")
    db.commit()
    R.publish(db, base, published_by="tester")
    db.commit()

    p1 = mk_period(db, YM)
    p2 = mk_period(db, YM2)

    A = client.post("/api/tasks", json={"task_no": f"{PREFIX}-A", "product": PRODUCT}).json()["task"]
    client.post(f"/api/tasks/{A['id']}/routes", json={"items": [{"product": PRODUCT, "part": PART}]})
    B = client.post("/api/tasks", json={"task_no": f"{PREFIX}-B", "product": PRODUCT}).json()["task"]
    C = client.post("/api/tasks", json={"task_no": f"{PREFIX}-C", "product": PRODUCT}).json()["task"]
    D = client.post("/api/tasks", json={"task_no": f"{PREFIX}-D", "product": PRODUCT}).json()["task"]
    check(all(t.get("id") for t in (A, B, C, D)), "四个任务已创建")

    sh = mk_sheet(db, p1, "S1", hours=2.0)
    sh.task_id = A["id"]
    db.commit()
    T.refresh_task(db, db.get(WorkTask, A["id"]))
    db.commit()
    db.expire_all()
    check(db.get(WorkTask, A["id"]).sheet_count == 1, "初始：单据挂在 A 上（A 单据数 1）")

    # ---------------------------------------------------------------- 1 正常改挂
    print("\n[1] A → B：两个任务的物化字段都要重算")
    r = client.post(f"/api/sheets/{sh.id}/task", json={"task_id": B["id"], "user": "tester"})
    body = r.json()
    check(r.status_code == 200, f"改挂接口成功（{r.status_code}）")
    check(body.get("action") == "move", f"动作是改挂（{body.get('action')}）")
    check((body.get("from_task") or {}).get("task_no") == f"{PREFIX}-A", "返回值带原订单")
    check((body.get("to_task") or {}).get("task_no") == f"{PREFIX}-B", "返回值带新订单")

    db.expire_all()
    a, b = db.get(WorkTask, A["id"]), db.get(WorkTask, B["id"])
    check(a.sheet_count == 0, f"原订单 A 单据数归零（{a.sheet_count}）")
    check(abs(a.actual_hours) < 1e-6, f"原订单 A 实报归零（{a.actual_hours}）")
    check(b.sheet_count == 1, f"新订单 B 单据数 1（{b.sheet_count}）")
    check(abs(b.actual_hours - 2.0) < 1e-6, f"新订单 B 实报 2.0（{b.actual_hours}）")

    codes = [c["code"] for c in body.get("checks") or []]
    check("no_route_ref" in codes, f"提示新订单没有该件名的工艺引用（{codes}）")
    check("from_task_empty" in codes, "提示原订单已无单据")
    check(
        PART in " ".join(c["message"] for c in body.get("checks") or []),
        "提示文案写清了是哪个件名缺引用",
    )

    # 审计留痕
    log = db.scalars(
        select(AuditLog).where(AuditLog.entity_id == sh.id, AuditLog.field == "task_id")
    ).all()
    check(len(log) == 1, f"写了一条归属变更留痕（{len(log)}）")
    if log:
        check(log[0].old_value == f"{PREFIX}-A", f"留痕旧值为 A（{log[0].old_value}）")
        check(log[0].new_value == f"{PREFIX}-B", f"留痕新值为 B（{log[0].new_value}）")
        # 身份由服务端收口：请求体里的 user 不再采信，留痕记真实操作人
        check(log[0].user == "本机操作员", f"留痕记真实操作人（{log[0].user}）")
        check(log[0].user != "tester", "请求体冒名（tester）未被采信")

    # ---------------------------------------------------------------- 2 幂等
    print("\n[2] 再改挂到同一个订单：不改任何东西")
    r = client.post(f"/api/sheets/{sh.id}/task", json={"task_id": B["id"]})
    body = r.json()
    check(r.status_code == 200 and body.get("unchanged") is True, "同订单改挂返回 unchanged")
    db.expire_all()
    logs = db.scalars(
        select(AuditLog).where(AuditLog.entity_id == sh.id, AuditLog.field == "task_id")
    ).all()
    check(len(logs) == 1, f"没有重复写留痕（{len(logs)}）")

    # ---------------------------------------------------------------- 3 补挂工艺
    print("\n[3] 改挂到 D 并勾选「同时补挂工艺引用」")
    r = client.post(
        f"/api/sheets/{sh.id}/task",
        json={"task_id": D["id"], "attach_routes": True, "user": "tester"},
    )
    body = r.json()
    check(r.status_code == 200, "改挂并补挂成功")
    check(body.get("attached_routes") == 1, f"补挂 1 条引用（{body.get('attached_routes')}）")
    db.expire_all()
    dtr = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == D["id"], TaskRoute.status == "active")
    ).all()
    check(len(dtr) == 1, f"任务 D 上确实有 1 条引用（{len(dtr)}）")
    check(
        not [c for c in body.get("checks") or [] if c["code"] == "no_route_ref"],
        "补挂之后不再提示缺引用",
    )

    # ---------------------------------------------------------------- 4 结案任务拒绝
    print("\n[4] 目标订单已结案：拒绝")
    client.patch(f"/api/tasks/{C['id']}", json={"status": "closed"})
    r = client.post(f"/api/sheets/{sh.id}/task", json={"task_id": C["id"]})
    check(r.status_code == 400, f"结案订单被拒（{r.status_code}）")
    check("结案" in r.json().get("detail", ""), f"拒绝原因写清是结案（{r.json().get('detail')}）")
    db.expire_all()
    check(db.get(Sheet, sh.id).task_id == D["id"], "被拒后归属保持原样")

    # ---------------------------------------------------------------- 5 跨期
    print("\n[5] 跨期改挂：默认拒，显式确认后放行并带警告")
    E = client.post(
        "/api/tasks",
        json={"task_no": f"{PREFIX}-E", "product": PRODUCT, "period_id": p2.id},
    ).json()["task"]
    r = client.post(f"/api/sheets/{sh.id}/task", json={"task_id": E["id"]})
    check(r.status_code == 400 and "跨期" in r.json().get("detail", ""), "跨期未确认被拒")
    r = client.post(f"/api/sheets/{sh.id}/task", json={"task_id": E["id"], "force": True})
    body = r.json()
    check(r.status_code == 200, "force=True 时放行")
    check(
        any(c["code"] == "cross_period" for c in body.get("checks") or []),
        "放行后仍带跨期警告",
    )

    # ---------------------------------------------------------------- 6 期间锁定
    print("\n[6] 核算期锁定：拒绝")
    p1.status = "locked"
    db.commit()
    r = client.post(f"/api/sheets/{sh.id}/task", json={"task_id": B["id"], "force": True})
    check(r.status_code == 400, f"锁定期间被拒（{r.status_code}）")
    check("锁定" in r.json().get("detail", ""), f"拒绝原因写清是锁定（{r.json().get('detail')}）")
    p1.status = "open"
    db.commit()

    # ---------------------------------------------------------------- 7 解绑
    print("\n[7] 摘下归属：单据回到未归集")
    r = client.post(f"/api/sheets/{sh.id}/task", json={"task_id": None, "user": "tester"})
    body = r.json()
    check(r.status_code == 200 and body.get("action") == "detach", "解绑成功")
    check(
        any(c["code"] == "detached" for c in body.get("checks") or []),
        "提示下次确认会重新自动归集",
    )
    db.expire_all()
    check(db.get(Sheet, sh.id).task_id is None, "单据 task_id 已清空")
    e = db.get(WorkTask, E["id"])
    check(e.sheet_count == 0, f"原订单 E 单据数归零（{e.sheet_count}）")

    # ---------------------------------------------------------------- 8 批量
    print("\n[8] 批量改挂：逐张独立成败")
    s2 = mk_sheet(db, p1, "S2", hours=1.0)
    s3 = mk_sheet(db, p1, "S3", hours=1.5)
    s2.task_id = A["id"]
    s3.task_id = None
    db.commit()
    r = client.post(
        "/api/sheets/batch-task",
        json={"sheet_ids": [s2.id, s3.id, 999999], "task_id": B["id"], "user": "tester"},
    )
    body = r.json()
    check(r.status_code == 200, "批量接口成功")
    check(body.get("moved") == 2, f"成功 2 张（{body.get('moved')}）")
    check(len(body.get("failed") or []) == 1, f"失败 1 张（{body.get('failed')}）")
    db.expire_all()
    b = db.get(WorkTask, B["id"])
    check(b.sheet_count == 2, f"B 现在有 2 张单（{b.sheet_count}）")
    check(abs(b.actual_hours - 2.5) < 1e-6, f"B 实报 2.5（{b.actual_hours}）")

    # ---------------------------------------------------------------- 9 列表与详情
    print("\n[9] 台账与单据详情：归属与提醒跟着变")
    rows = {
        x["id"]: x
        for x in client.get("/api/sheets", params={"size": 200}).json()["items"]
    }
    check(rows.get(s2.id, {}).get("task_no") == f"{PREFIX}-B", "台账任务列显示新订单")
    detail = client.get(f"/api/sheets/{s2.id}").json()
    check((detail.get("task") or {}).get("task_no") == f"{PREFIX}-B", "单据详情归属同步")

    # B 现在引用了基线（任务创建时没引用，改挂后仍没有 → 先补一条再制造升版）
    client.post(f"/api/tasks/{B['id']}/routes", json={"items": [{"product": PRODUCT, "part": PART}]})
    # F 也要**先**引用现行版，之后 B 升版才会让 F 落后（这才是「待确认」的成因）
    F = client.post("/api/tasks", json={"task_no": f"{PREFIX}-F", "product": PRODUCT}).json()["task"]
    client.post(f"/api/tasks/{F['id']}/routes", json={"items": [{"product": PRODUCT, "part": PART}]})

    db.expire_all()
    tr_b = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == B["id"], TaskRoute.status == "active")
    ).first()
    client.put(
        f"/api/tasks/{B['id']}/routes/{tr_b.id}/steps",
        json={"steps": [step(10, "车", 1.6), step(20, "铣", 0.4)], "publish": True},
    )

    # 把 s3 从 B 改挂到 F：F 有升版待确认 → notices 必须回来
    r = client.post(f"/api/sheets/{s3.id}/task", json={"task_id": F["id"]})
    body = r.json()
    check(
        "version_notice" in [c["code"] for c in body.get("checks") or []],
        f"改挂到待确认订单时有提醒（{[c['code'] for c in body.get('checks') or []]}）",
    )
    check(body.get("notice_count", 0) >= 1, f"返回值带 notices（{body.get('notice_count')}）")
    rows = {
        x["id"]: x
        for x in client.get("/api/sheets", params={"size": 200}).json()["items"]
    }
    check(rows.get(s3.id, {}).get("task_notice_count", 0) >= 1, "台账上该单显示升版待确认徽标")

    print(f"\n===== {len(PASSED)} PASSED / {len(FAILED)} FAILED")
    for f in FAILED:
        print("  FAILED:", f)
    cleanup(db)
    db.close()
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
