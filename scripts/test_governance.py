"""闭环整改回归：作废/恢复/删除、任务口径、缺工序阻断、跨期确认、登录留痕。

用法：python scripts/test_governance.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

六组：
  [1] 身份：口令校验、令牌失效、审计记的是登录人而不是请求体里的名字
  [2] 作废 / 恢复 / 彻底删除：三道闸与删除留痕
  [3] 跨期确认：默认拒绝，显式确认才入账
  [4] 缺工序：阻断级，显式强制才能过
  [5] 任务口径：实报只算已确认，待核对单列
  [6] 维度补录：已确认单据不必撤销确认也能补产品/件名/工序
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    AppUser,
    AuditLog,
    Batch,
    Image,
    Period,
    Sheet,
    WorkItem,
    WorkTask,
)
from app.security import hash_password, make_token, parse_token  # noqa: E402

YM = "2035-03"
YM_LOCK = "2035-04"
TAG = "ZZGOV"
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


# ------------------------------------------------------------------ 夹具
def ensure_period(db, ym: str, status: str = "open") -> Period:
    p = db.scalar(select(Period).where(Period.ym == ym))
    if p is None:
        p = Period(ym=ym, title=f"{ym} 自测", status=status)
        db.add(p)
        db.flush()
    p.status = status
    return p


def ensure_batch(db, period: Period) -> Batch:
    b = db.scalar(select(Batch).where(Batch.period_id == period.id, Batch.name == TAG))
    if b is None:
        b = Batch(period_id=period.id, name=TAG, source="selftest")
        db.add(b)
        db.flush()
    return b


def ensure_user(db, username: str, password: str, role: str = "editor", active: bool = True) -> AppUser:
    u = db.scalar(select(AppUser).where(AppUser.username == username))
    if u is None:
        u = AppUser(username=username, display_name=username, password_hash=hash_password(password), role=role)
        db.add(u)
    u.active = active
    u.role = role
    db.flush()
    return u


def make_sheet(
    db,
    period: Period,
    batch: Batch,
    *,
    product: str = "自测产品",
    part: str = "自测件",
    process: str = "车",
    worker_id: int | None = 1,
    unit: float = 2.0,
    qty: float = 4.0,
    finish: dt.date | None = None,
    status: str = "recognized",
    task_id: int | None = None,
) -> Sheet:
    img = Image(
        batch_id=batch.id,
        filename=f"{TAG}-{uuid.uuid4().hex[:6]}.jpg",
        path="",
        sha256=f"{TAG}-{uuid.uuid4().hex}",
        status="done",
    )
    db.add(img)
    db.flush()
    sheet = Sheet(
        image_id=img.id,
        period_id=period.id,
        status=status,
        header_json=json.dumps(
            {"product_name": product, "part_name": part, "batch_no": TAG}, ensure_ascii=False
        ),
        total_hours=round(unit * qty, 4),
        verify_user="复核人" if status == "verified" else "",
        verify_at=dt.datetime.now() if status == "verified" else None,
        task_id=task_id,
    )
    db.add(sheet)
    db.flush()
    db.add(
        WorkItem(
            sheet_id=sheet.id,
            seq=1,
            product_name=product,
            part_name=part,
            batch_no=TAG,
            process=process,
            process_std=process,
            worker_id=worker_id,
            worker_raw="自测员",
            unit_hours=unit,
            qualified_qty=qty,
            total_hours=round(unit * qty, 4),
            finish_date=finish,
        )
    )
    db.commit()
    db.refresh(sheet)
    return sheet


def make_task(db, period: Period, task_no: str) -> WorkTask:
    t = db.scalar(select(WorkTask).where(WorkTask.task_no == task_no))
    if t is None:
        t = WorkTask(task_no=task_no, title=TAG, product="自测产品", period_id=period.id, status="open")
        db.add(t)
        db.commit()
    return t


def cleanup(db) -> None:
    """只清自造数据：按前缀/期间定位，绝不碰真实单据。"""
    ids = [s.id for s in db.scalars(select(Sheet).join(Period, Sheet.period_id == Period.id).where(Period.ym.in_((YM, YM_LOCK)))).all()]
    if ids:
        db.query(WorkItem).filter(WorkItem.sheet_id.in_(ids)).delete(synchronize_session=False)
        db.query(AuditLog).filter(AuditLog.sheet_id.in_(ids)).delete(synchronize_session=False)
        db.query(Sheet).filter(Sheet.id.in_(ids)).delete(synchronize_session=False)
    periods = db.scalars(select(Period).where(Period.ym.in_((YM, YM_LOCK)))).all()
    pids = [p.id for p in periods]
    if pids:
        imgs = db.scalars(select(Image).join(Batch, Image.batch_id == Batch.id).where(Batch.period_id.in_(pids))).all()
        img_ids = [i.id for i in imgs]
        db.query(Image).filter(Image.id.in_(img_ids)).delete(synchronize_session=False) if img_ids else None
        db.query(Batch).filter(Batch.period_id.in_(pids)).delete(synchronize_session=False)
        db.query(WorkTask).filter(WorkTask.period_id.in_(pids)).delete(synchronize_session=False)
        db.query(Period).filter(Period.id.in_(pids)).delete(synchronize_session=False)
    db.query(AppUser).filter(AppUser.username.like(f"{TAG.lower()}%")).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.field == "dims:batch").delete(synchronize_session=False)
    db.commit()


# ------------------------------------------------------------------ 用例
def main() -> int:
    from app.main import app

    init_db()
    client = TestClient(app)
    db = SessionLocal()
    cleanup(db)

    p_open = ensure_period(db, YM, "open")
    p_lock = ensure_period(db, YM_LOCK, "locked")
    batch = ensure_batch(db, p_open)
    batch_lock = ensure_batch(db, p_lock)
    db.commit()

    # ---------------------------------------------------------------- 1. 身份
    section("1 身份：登录、令牌、留痕到底记谁")
    user = ensure_user(db, f"{TAG.lower()}_u", "Gov@2026", role="editor")
    admin = ensure_user(db, f"{TAG.lower()}_admin", "Gov@2026", role="admin")
    db.commit()

    r = client.post("/api/auth/login", json={"username": user.username, "password": "wrong"})
    check(r.status_code == 401, "错误口令被拒（401）")
    r = client.post("/api/auth/login", json={"username": user.username, "password": "Gov@2026"})
    check(r.status_code == 200 and r.json().get("token"), "正确口令拿到令牌")
    token = r.json()["token"]
    H = {"Authorization": f"Bearer {token}"}

    r = client.get("/api/auth/me", headers=H)
    check(r.status_code == 200 and (r.json().get("user") or {}).get("username") == user.username, "/auth/me 返回当前账号")

    payload = parse_token(token)
    check(bool(payload) and payload.get("u") == user.username, "令牌验签通过且带用户名")
    expired = make_token(user.username, 0, ttl_hours=-1)["token"]
    check(parse_token(expired) is None, "过期令牌验不过")

    # 冒名：请求体写别人的名字，落库必须是持令牌的人
    s = make_sheet(db, p_open, batch, status="recognized")
    r = client.post(f"/api/sheets/{s.id}/verify", json={"user": "冒名者", "confirm_out_of_period": True}, headers=H)
    check(r.status_code == 200, "持令牌确认成功")
    db.expire_all()
    s = db.get(Sheet, s.id)
    check(s.verify_user == user.username, f"确认人=登录人（实得 {s.verify_user}）")
    rows = db.query(AuditLog).filter(AuditLog.sheet_id == s.id, AuditLog.field == "status").all()
    check(any(a.user == user.username for a in rows), "审计记的是登录人")
    check(not any(a.user == "冒名者" for a in rows), "请求体里的名字没有进审计")

    # 停用即踢下线
    user.active = False
    user.token_epoch = int(user.token_epoch or 0) + 1
    db.commit()
    r = client.post(f"/api/sheets/{s.id}/reopen", headers=H)
    check(r.status_code == 401, "账号停用后旧令牌立即失效（401）")
    user.active = True
    db.commit()
    r = client.post("/api/auth/login", json={"username": user.username, "password": "Gov@2026"})
    token = r.json()["token"]
    H = {"Authorization": f"Bearer {token}"}

    # ---------------------------------------------------------------- 2. 作废/恢复/删除
    section("2 作废 / 恢复 / 彻底删除")
    s2 = make_sheet(db, p_open, batch, status="recognized")
    sid2 = s2.id
    r = client.post(f"/api/sheets/{s2.id}/reject", json={"reason": "拍糊了"}, headers=H)
    check(r.status_code == 200, "作废成功")
    db.expire_all()
    check(db.get(Sheet, s2.id).status == "rejected", "状态=已作废")
    r = client.post(f"/api/sheets/{s2.id}/restore", headers=H)
    check(r.status_code == 200, "作废可恢复")
    db.expire_all()
    check(db.get(Sheet, s2.id).status == "recognized", "恢复后回到待核对")

    r = client.post(f"/api/sheets/{s2.id}/reject", json={"reason": "重复上传"}, headers=H)
    r = client.delete(f"/api/sheets/{s2.id}", params={"reason": "重复上传，删除"}, headers=H)
    check(r.status_code == 200, "作废单可以彻底删除")
    check(client.get(f"/api/sheets/{sid2}", headers=H).status_code == 404, "删除后单据查不到")
    db.expire_all()
    gone = db.query(AuditLog).filter(AuditLog.field == "deleted", AuditLog.entity_id == sid2).all()
    check(bool(gone) and json.loads(gone[0].old_value).get("items"), "删除留痕里有整单快照")
    check(db.scalar(select(func.count()).select_from(Sheet).where(Sheet.id == sid2)) == 0, "库里确实删掉了")

    s3 = make_sheet(db, p_open, batch, status="recognized")
    client.post(f"/api/sheets/{s3.id}/verify", json={"confirm_out_of_period": True}, headers=H)
    r = client.delete(f"/api/sheets/{s3.id}", headers=H)
    check(r.status_code == 400, "已确认单据不许删（必须先撤销+作废）")

    s4 = make_sheet(db, p_lock, batch_lock, status="rejected")
    r = client.delete(f"/api/sheets/{s4.id}", headers=H)
    check(r.status_code == 400, "锁定期间的凭证不许删")

    # ---------------------------------------------------------------- 3. 跨期
    section("3 跨期确认：默认拦下，显式点头才入账")
    s5 = make_sheet(db, p_open, batch, finish=dt.date(2030, 1, 15), status="recognized")
    r = client.post(f"/api/sheets/{s5.id}/verify", json={}, headers=H)
    check(r.status_code == 409, "跨期单据默认不确认（409）")
    detail = r.json().get("detail") or {}
    check(isinstance(detail, dict) and detail.get("code") == "OUT_OF_PERIOD", "回执带 OUT_OF_PERIOD 与明细行")
    check(bool(detail.get("items")), "回执列出了跨期行")
    r = client.post(f"/api/sheets/{s5.id}/verify", json={"confirm_out_of_period": True}, headers=H)
    check(r.status_code == 200, "勾选允许跨期后可确认")
    check(len(r.json().get("out_of_period") or []) == 1, "确认回执再次提示跨期行")

    s6 = make_sheet(db, p_open, batch, finish=dt.date(2030, 2, 1), status="recognized")
    r = client.post("/api/sheets/batch-verify", json={"sheet_ids": [s6.id]}, headers=H)
    check(s6.id in [x["sheet_id"] for x in r.json().get("skipped") or []], "批量确认跳过跨期单据")

    # ---------------------------------------------------------------- 4. 缺工序
    section("4 缺工序：阻断级")
    s7 = make_sheet(db, p_open, batch, process="", status="recognized")
    # 真实流程里告警是识别/编辑时算出来的；这里用一次保存触发重算
    client.patch(f"/api/sheets/{s7.id}", json={}, headers=H)
    detail = client.get(f"/api/sheets/{s7.id}", headers=H).json()
    codes = [f["code"] for f in (detail["items"][0]["flags"] or [])]
    check("MISSING_PROCESS" in codes, "缺工序产生 MISSING_PROCESS 告警")
    lv = [f for f in detail["items"][0]["flags"] if f["code"] == "MISSING_PROCESS"][0]["level"]
    check(lv == "block", "该告警是阻断级")
    r = client.post(f"/api/sheets/{s7.id}/verify", json={"confirm_out_of_period": True}, headers=H)
    check(r.status_code == 400 and "阻断" in str(r.json().get("detail")), "缺工序不许直接确认")
    r = client.post(f"/api/sheets/{s7.id}/verify", json={"force": True, "confirm_out_of_period": True}, headers=H)
    check(r.status_code == 200 and r.json().get("forced"), "强制确认可过（留痕）")

    # ---------------------------------------------------------------- 5. 任务口径
    section("5 任务口径：实报只算已确认，待核对单列")
    task = make_task(db, p_open, f"{TAG}-TASK-1")
    sv = make_sheet(db, p_open, batch, unit=2.0, qty=4.0, status="verified", task_id=task.id)  # 8h
    sp = make_sheet(db, p_open, batch, unit=2.5, qty=2.0, status="recognized", task_id=task.id)  # 5h
    from app.services.tasks import refresh_task

    refresh_task(db, task)
    db.commit()
    db.refresh(task)
    check(abs(task.actual_hours - 8.0) < 0.01, f"实报只算已确认（实得 {task.actual_hours}）")
    check(task.sheet_count == 1, f"已确认单据数=1（实得 {task.sheet_count}）")
    check(abs(task.pending_hours - 5.0) < 0.01, f"待核对工时单列（实得 {task.pending_hours}）")
    check(task.pending_count == 1, f"待核对单据数=1（实得 {task.pending_count}）")

    r = client.post(f"/api/sheets/{sp.id}/verify", json={"confirm_out_of_period": True}, headers=H)
    check(r.status_code == 200, "待核对单确认入账")
    db.expire_all()
    task = db.get(WorkTask, task.id)
    check(abs(task.actual_hours - 13.0) < 0.01, f"确认后实报变 13h（实得 {task.actual_hours}）")
    check(task.sheet_count == 2 and task.pending_count == 0, "单据数与待核对同步更新")

    listing = client.get("/api/tasks", params={"q": f"{TAG}-TASK-1"}, headers=H).json()
    row = next((x for x in listing["items"] if x["id"] == task.id), None)
    check(row is not None and "pending_hours" in row and "pending_count" in row, "任务列表带待核对字段")
    check(abs((row or {}).get("actual_hours", 0) - 13.0) < 0.01, "任务列表实报=13h")

    # ---------------------------------------------------------------- 6. 维度补录
    section("6 维度补录：已确认单据不必撤销确认")
    s8 = make_sheet(db, p_open, batch, product="", part="", process="", status="recognized")
    client.post(f"/api/sheets/{s8.id}/verify", json={"force": True, "confirm_out_of_period": True}, headers=H)
    db.expire_all()
    s8 = db.get(Sheet, s8.id)
    item = s8.items[0]
    before_hours = s8.total_hours
    r = client.patch(
        f"/api/sheets/{s8.id}/dims",
        json={
            "items": [{"id": item.id, "product_name": "制丸机", "part_name": "切刀", "process": "焊"}],
            "reason": "按原图补录",
        },
        headers=H,
    )
    check(r.status_code == 200 and r.json().get("changed"), "已确认单据可直接补录维度")
    db.expire_all()
    s8 = db.get(Sheet, s8.id)
    item = db.get(WorkItem, item.id)
    check((item.product_name, item.part_name, item.process) == ("制丸机", "切刀", "焊"), "三个维度都写进去了")
    check(abs(s8.total_hours - before_hours) < 0.001, "补维度不影响工时金额")
    check(s8.status == "verified", "补录后仍是已确认，确认时间戳没被抹掉")
    audits = db.query(AuditLog).filter(AuditLog.entity_id == item.id, AuditLog.field.like("item:%")).all()
    check(len(audits) >= 3, "逐字段留痕")
    r = client.patch(f"/api/sheets/{s8.id}/dims", json={"items": [{"id": item.id, "product_name": "制丸机"}], "reason": "同值"}, headers=H)
    check(r.status_code == 200 and not r.json().get("changed"), "同值不重复写")
    r = client.patch(f"/api/sheets/{s8.id}/dims", json={"items": [{"id": item.id, "process": ""}], "reason": "清空"}, headers=H)
    check(r.status_code == 400, "补录接口不允许把维度清空")

    # ---------------------------------------------------------------- 清理
    section("7 清理自造数据")
    cleanup(db)
    left = db.scalar(select(func.count()).select_from(Sheet).join(Period, Sheet.period_id == Period.id).where(Period.ym.in_((YM, YM_LOCK)))) or 0
    check(left == 0, "自测单据已清空")
    users_left = db.scalar(select(func.count()).select_from(AppUser).where(AppUser.username.like(f"{TAG.lower()}%"))) or 0
    check(users_left == 0, "自测账号已清空")
    db.close()

    print("\n" + "=" * 60)
    print(f"通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
