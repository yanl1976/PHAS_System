"""闭环整改的回归自测：账内/账外分开判、锁定不可绕过、状态回滚要重算、动作要留痕。

这些点原先都「看起来正常」——接口照常 200，数字也照常显示，
错的是口径：一张作废单能永久挡住导出、锁定期能被批量入口绕过、
撤单后任务的实报工时还挂着、批量确认查不到是谁批的。

用法：`.venv/Scripts/python.exe scripts/test_closure_audit.py`
自带数据与清理，只碰自造的 2032 期间，不动 2026-09 的真实账。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.domain.aggregation import reconcile  # noqa: E402
from app.domain.rules import validate_item  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AppUser,
    AuditLog,
    Batch,
    Image,
    Period,
    Sheet,
    TaskRoute,
    WorkItem,
    WorkTask,
)
from app.security import hash_password  # noqa: E402

YM = "2032-01"
FAILED: list[str] = []
CLIENT = TestClient(app)


def check(cond: bool, label: str) -> None:
    print(f"  {'[OK]' if cond else '[FAIL]'} {label}")
    if not cond:
        FAILED.append(label)


SELF_TEST_USERS = ("self_batchman", "self_closer")


def login_as(username: str, role: str = "editor") -> dict:
    """建号并登录，返回请求头。

    审计里的操作人现在由令牌决定，测试再用 body.user 冒名是过不了的 ——
    这正是要验证的行为，所以测试也得走登录。
    """
    db = SessionLocal()
    try:
        u = db.query(AppUser).filter(AppUser.username == username).first()
        if u is None:
            u = AppUser(
                username=username,
                display_name=username,
                password_hash=hash_password("Self@2026"),
                role=role,
                active=True,
            )
            db.add(u)
        else:
            u.active = True
            u.role = role
        db.commit()
    finally:
        db.close()
    r = CLIENT.post("/api/auth/login", json={"username": username, "password": "Self@2026"})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def fresh(db) -> None:
    """清掉自造的整段数据（只按本测试的期间与任务号前缀，绝不按表清空）。"""
    period = db.query(Period).filter(Period.ym == YM).first()
    if period:
        sheets = db.query(Sheet).filter(Sheet.period_id == period.id).all()
        for s in sheets:
            db.query(WorkItem).filter(WorkItem.sheet_id == s.id).delete()
            db.query(AuditLog).filter(AuditLog.sheet_id == s.id).delete()
        db.query(Sheet).filter(Sheet.period_id == period.id).delete()
        batches = db.query(Batch).filter(Batch.period_id == period.id).all()
        for b in batches:
            db.query(Image).filter(Image.batch_id == b.id).delete()
            db.query(Batch).filter(Batch.id == b.id).delete()
        for t in db.query(WorkTask).filter(
            WorkTask.task_no.like("ZZCLOSE-%") | (WorkTask.product == "自测产品")
        ).all():
            db.query(TaskRoute).filter(TaskRoute.task_id == t.id).delete()
            db.delete(t)
        db.query(Period).filter(Period.id == period.id).delete()
    db.commit()


def build_sheet(db, *, status: str = "recognized", with_worker: bool = True) -> tuple[Sheet, WorkItem, int]:
    period = db.query(Period).filter(Period.ym == YM).first()
    if period is None:
        period = Period(ym=YM, title="闭环自测", status="open")
        db.add(period)
        db.flush()
    batch = Batch(period_id=period.id, name="闭环自测批次")
    db.add(batch)
    db.flush()
    img = Image(batch_id=batch.id, filename="closure.jpg", path="", sha256=f"closure-selftest-{uuid.uuid4().hex[:8]}", status="done")
    db.add(img)
    db.flush()
    sheet = Sheet(
        image_id=img.id,
        period_id=period.id,
        status=status,
        header_json='{"product_name": "自测产品", "batch_no": "ZZ"}',
        total_hours=8.0,
    )
    db.add(sheet)
    db.flush()
    item = WorkItem(
        sheet_id=sheet.id,
        seq=1,
        product_name="自测产品",
        part_name="自测件",
        process="车",
        process_code="CHE",
        unit_hours=2.0,
        qualified_qty=4.0,
        total_hours=8.0,
        finish_date=None,
        worker_id=1 if with_worker else None,
        worker_raw="某工人" if with_worker else "",
        flags_json="[]",
    )
    db.add(item)
    db.commit()
    return sheet, item, period.id


def main() -> None:
    db = SessionLocal()
    try:
        fresh(db)

        # ---------------------------------------------------------- 1. 规则层
        print("[1] 归集维度缺失要报出来（否则汇总悄悄多一列「未填」）")
        flags = validate_item({"unit_hours": 1.0, "qualified_qty": 1.0, "product_name": "", "part_name": ""}, YM)
        codes = {f["code"] for f in flags}
        check("MISSING_PRODUCT" in codes, "产品为空 → MISSING_PRODUCT")
        check("MISSING_PART" in codes, "件名为空 → MISSING_PART")
        flags2 = validate_item({"unit_hours": 1.0, "qualified_qty": 1.0, "product_name": "P", "part_name": "A"}, YM)
        check(not ({f["code"] for f in flags2} & {"MISSING_PRODUCT", "MISSING_PART"}), "维度齐全时不报")

        # ------------------------------------------------- 2. 账内 / 账外分开判
        print("[2] 未入账单据的问题不得阻断整月导出")
        sheet_r, item_r, _ = build_sheet(db, status="recognized", with_worker=False)
        rep = reconcile(db, YM)
        codes = {i["code"] for i in rep["issues"]}
        check(rep["balanced"] is True, "只有账外坏行时 balanced=True（可导出）")
        check("UNASSIGNED" not in codes, "账外坏行不再被判成 UNASSIGNED 阻断")
        check("UNASSIGNED_OFFLINE" in codes, "账外坏行以 UNASSIGNED_OFFLINE 提示")
        check(any(x["item_id"] == item_r.id for x in rep["checks"]["unassigned_offline"]), "提示里带得上具体行")
        # 作废单不是「待入账」：它永远不入账，算进未确认工时会让人天天看见一条消不掉的提示
        sheet_x, item_x, _ = build_sheet(db, status="rejected", with_worker=False)
        rep = reconcile(db, YM)
        check(
            not any(x["item_id"] == item_x.id for x in rep["checks"]["unassigned_offline"]),
            "作废单不进账外待办清单",
        )
        check(
            abs(rep["checks"]["pending_hours"] - (item_r.total_hours or 0)) < 0.01,
            f"未确认工时只算待核对（实得 {rep['checks']['pending_hours']}）",
        )

        print("[3] 已入账的单据出问题必须阻断")
        sheet_v, item_v, _ = build_sheet(db, status="verified", with_worker=False)
        rep = reconcile(db, YM)
        codes = {i["code"] for i in rep["issues"] if i["level"] == "error"}
        check("UNASSIGNED" in codes, "账内缺人 → error UNASSIGNED，导出被拦")
        check(rep["balanced"] is False, "balanced=False")
        item_v.worker_id = 1
        db.commit()

        # ------------------------------------------- 4. 状态回滚要重算任务实报
        print("[4] 重开单据后任务实报必须跟着减")
        task = WorkTask(task_no="ZZCLOSE-T1", title="自测任务", product="自测产品", period_id=sheet_v.period_id, status="open")
        db.add(task)
        db.flush()
        sheet_v.task_id = task.id
        db.commit()
        from app.services.tasks import refresh_task  # 局部导入避免测试启动慢

        refresh_task(db, task)
        db.commit()
        check(abs((task.actual_hours or 0) - 8.0) < 0.001, f"确认状态下实报 8h（实得 {task.actual_hours}）")
        check((task.pending_count or 0) == 0, "确认状态下没有待核对")
        # 口径说明：任务实报 = 「有效单据」合计（含待核对，不含驳回）。
        # 重开只是退回待核对：这单不再算「已入账」，实报归零、进待核对。
        # 汇总页只认已确认，任务页若还把待核对算进实报，同一批单据会有两个数。
        r = CLIENT.post(f"/api/sheets/{sheet_v.id}/reopen", params={"user": "tester"})
        db.expire_all()
        task = db.get(WorkTask, task.id)
        check(r.status_code == 200, "重开接口 200")
        check(abs(task.actual_hours or 0) < 0.001, f"重开后实报归零（实得 {task.actual_hours}）")
        check(abs((task.pending_hours or 0) - 8.0) < 0.001, f"该单转入待核对 8h（实得 {task.pending_hours}）")
        r = CLIENT.post(f"/api/sheets/{sheet_v.id}/reject", json={"reason": "自测驳回", "user": "tester"})
        db.expire_all()
        task = db.get(WorkTask, task.id)
        check(r.status_code == 200, "驳回接口 200")
        check(abs((task.actual_hours or 0)) < 0.001, f"驳回（作废）后实报必须归零（实得 {task.actual_hours}）")
        # 复位：重新确认，供后面锁定/留痕用例使用
        CLIENT.post(f"/api/sheets/{sheet_v.id}/reopen", params={"user": "tester"})

        # -------------------------------------------------- 5. 锁定不可绕过
        print("[5] 核算期锁定后，任何入口都不能改账")
        period = db.query(Period).filter(Period.ym == YM).first()
        period.status = "locked"
        db.commit()
        r1 = CLIENT.post(f"/api/sheets/{sheet_v.id}/verify", json={"user": "tester", "force": True})
        r2 = CLIENT.post(f"/api/sheets/{sheet_v.id}/reopen", params={"user": "tester"})
        r3 = CLIENT.post("/api/sheets/batch-verify", json={"sheet_ids": [sheet_v.id], "user": "tester"})
        rejected = all(r.status_code == 400 for r in (r1, r2))
        skipped = r3.status_code == 200 and sheet_v.id not in (r3.json().get("verified") or [])
        check(rejected, f"单张确认/重开被拒（{r1.status_code}/{r2.status_code}）")
        check(skipped, "批量确认把锁定单据跳过而不是照样确认")
        period.status = "open"
        db.commit()

        # ------------------------------------------------------ 6. 留痕可追
        print("[6] 动作必须留痕")
        sheet_f, _item_f, _ = build_sheet(db, status="recognized", with_worker=True)  # 未归属任何任务
        batch_headers = login_as("self_batchman")
        r = CLIENT.post("/api/sheets/batch-verify", json={"sheet_ids": [sheet_f.id]}, headers=batch_headers)
        db.expire_all()
        check(r.status_code == 200 and sheet_f.id in (r.json().get("verified") or []), "批量确认成功")
        rows = db.query(AuditLog).filter(AuditLog.sheet_id == sheet_f.id, AuditLog.field == "status").all()
        check(any(a.user == "self_batchman" for a in rows), "批量确认写了状态留痕（登录人）")
        moved = db.query(AuditLog).filter(AuditLog.sheet_id == sheet_f.id, AuditLog.field == "task_id").all()
        check(bool(moved), "自动归集写了归属留痕")
        check(bool(moved) and moved[0].new_value, "留痕里带得上归到了哪个任务")

        # ------------------------------------------------------ 7. 月结不被作废单卡死
        print("[7] 作废单不能永久阻止月结，且锁定/解锁要留痕")
        # 先把第 4 步被退回待核对的那张重新确认掉，让本期间只剩「已确认」和「已作废」
        CLIENT.post(f"/api/sheets/{sheet_v.id}/verify", json={"user": "tester", "force": True})
        build_sheet(db, status="rejected", with_worker=False)  # 该期间留一张作废单
        # 前面几段留下的待核对单据也要清掉，否则「存在未核对单据」会先拦住锁定，
        # 测不到「作废单不该卡住月结」这件事本身
        for s_ in db.query(Sheet).filter(Sheet.period_id == sheet_v.period_id).all():
            if s_.status not in ("verified", "rejected"):
                s_.status = "rejected"
        db.commit()
        db.expire_all()
        period = db.query(Period).filter(Period.ym == YM).first()
        check(period.status == "open", "期间当前是未锁定")
        closer_headers = login_as("self_closer", role="admin")
        r = CLIENT.post(f"/api/periods/{period.id}/lock", headers=closer_headers)
        check(r.status_code == 200, f"有作废单也能锁定（HTTP {r.status_code} {r.text[:60]}）")
        db.expire_all()
        rows = db.query(AuditLog).filter(AuditLog.entity == "period", AuditLog.entity_id == period.id).all()
        check(any(a.field == "status" and a.new_value == "locked" and a.user == "self_closer" for a in rows), "锁定写了留痕（登录人）")
        r = CLIENT.post(f"/api/periods/{period.id}/unlock", params={"reason": "自测解锁"}, headers=closer_headers)
        db.expire_all()
        rows = db.query(AuditLog).filter(AuditLog.entity == "period", AuditLog.entity_id == period.id).all()
        check(r.status_code == 200, "解锁 200")
        check(any(a.field == "unlock_reason" and a.new_value == "自测解锁" for a in rows), "解锁原因写了留痕")
    finally:
        try:
            fresh(db)
            db.query(AppUser).filter(AppUser.username.in_(SELF_TEST_USERS)).delete(synchronize_session=False)
            db.query(AuditLog).filter(AuditLog.user.in_(SELF_TEST_USERS)).delete(synchronize_session=False)
            db.commit()
        except Exception as exc:  # 清理失败不许掩盖测试结果
            print("  清理失败：", exc)
        db.close()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
