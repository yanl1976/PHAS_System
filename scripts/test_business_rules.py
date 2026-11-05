"""业务规则收口验证（P3）。

这一组测的不是「功能能不能用」，而是「不合规的动作会不会被挡住」：

1. 只有**已发布**的工艺路线能被任务引用（草稿/归档一律拒绝）；
2. 已发布路线不能被撤回/归档/删除，只要还有在制任务引用它；
3. 发布必须有工序、同一组合只能有一条已发布；
4. 工序行必须能认出工序、工时不能为负、工序号撞号要顺延而不是整批失败；
5. 任务结案/取消后，工艺路线锁死；
6. 副本只能从已发布基线拆，且副本可以进阶为基线（一致则合并回引用）。

用法：.venv\\Scripts\\python.exe scripts/test_business_rules.py
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
from app.services import routes as R  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
PREFIX = "ZZRULE"
PRODUCT = "ZZRULE产品"
PART = "规则件"


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
    """只清本测试造的数据。"""
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
    db = SessionLocal()
    cleanup(db)

    # ---------------------------------------------------------------- 1 发布门槛
    print("\n[1] 发布门槛：空路线、重复发布、唯一基准")
    draft = R.create_draft(db, PRODUCT, PART, steps=[], created_by="test")
    db.commit()
    try:
        R.publish(db, draft)
        check(False, "空路线发布被拒")
    except ValueError as exc:
        check("空路线" in str(exc) or "1 道工序" in str(exc), f"空路线发布被拒（{exc}）")
        db.rollback()

    draft = R.create_draft(db, PRODUCT, PART, steps=[step(10, "车", 1.5)], created_by="test")
    db.commit()
    R.publish(db, draft, published_by="tester")
    db.commit()
    db.refresh(draft)
    check(draft.status == "released", "有工序的草稿可以发布")
    check(bool(draft.published_at) and draft.published_by == "tester", "发布留痕（时间+人）")

    try:
        R.publish(db, draft)
        check(False, "重复发布被拒")
    except ValueError as exc:
        check("已发布" in str(exc), f"重复发布被拒（{exc}）")
        db.rollback()

    # ---------------------------------------------------------------- 2 引用门槛
    print("\n[2] 引用门槛：草稿不能引用，自动补建会立即发布")
    task = client.post(
        "/api/tasks", json={"task_no": f"{PREFIX}-1", "title": "规则测试", "product": PRODUCT}
    ).json()["task"]
    tid = task["id"]

    # 造一条草稿（v2），看引用时是否被挡
    draft2 = R.create_draft(db, PRODUCT, PART, steps=[step(10, "车", 2.0)], created_by="test")
    db.commit()
    resp = client.post(
        f"/api/tasks/{tid}/routes",
        json={"items": [{"product": PRODUCT, "part": PART, "route_id": draft2.id}]},
    )
    body = resp.json()
    check(
        body["failed"] and "未发布" in body["failed"][0]["reason"],
        f"指定草稿版本被拒（{body['failed'][0]['reason'] if body['failed'] else '没有失败'}）",
    )
    check(body["failed"][0].get("need_publish") is True, "失败原因带「需要发布」标记")

    # 引用已发布的 v1：应当成功
    resp = client.post(
        f"/api/tasks/{tid}/routes",
        json={"items": [{"product": PRODUCT, "part": PART}]},
    )
    body = resp.json()
    check(body["added"] == 1, "引用已发布基线成功")
    tr = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == tid, TaskRoute.status == "active")
    ).first()
    check(tr is not None and tr.origin == "baseline", "引用型 origin=baseline")
    tr_id = tr.id

    # 全新产品×件名且**没有历史工时**：不该硬造一条空基线，
    # 应当明确要求人工建线（空路线发布出去会让核对把所有工序判成「多做的」）
    new_part = "自动件"
    resp = client.post(
        f"/api/tasks/{tid}/routes",
        json={"items": [{"product": PRODUCT, "part": new_part}], "create_baseline": True},
    )
    body = resp.json()
    reasons = " ".join(f.get("reason", "") for f in body.get("failed", []))
    check("没有历史工时" in reasons, f"无历史工时的新组合被明确拒绝（{reasons}）")
    check(R.pick_baseline(db, PRODUCT, new_part) is None, "没有留下空壳基线")
    check(body.get("auto_published") == 0, "没有偷偷建线")

    # create_baseline=False 且只有草稿 → 明确报「未发布」
    resp = client.post(
        f"/api/tasks/{tid}/routes",
        json={"items": [{"product": PRODUCT, "part": PART}], "create_baseline": False},
    )
    body = resp.json()
    check(
        body["failed"] and "已经在任务里了" in body["failed"][0]["reason"],
        "重复引用同一条被拒",
    )

    # ---------------------------------------------------------------- 3 撤回/归档/删除保护
    print("\n[3] 被引用的已发布路线不能撤回/归档/删除")
    base = db.get(ProcessRoute, tr.route_id)
    # 因为上面自动建线也引用了，这里只看 bas 这一条
    r1 = client.post(f"/api/routes/{base.id}/reopen")
    check(r1.status_code == 400, f"撤回发布被拒（{r1.status_code}）")
    r2 = client.post(f"/api/routes/{base.id}/archive")
    check(r2.status_code == 400, f"归档被拒（{r2.status_code}）")
    r3 = client.delete(f"/api/routes/{base.id}")
    check(r3.status_code == 400, f"删除被拒（{r3.status_code}）")
    db.expire_all()
    check(db.get(ProcessRoute, base.id).status == "released", "拒绝后状态没变")

    # 先把引用摘掉，再撤回就应当允许
    client.delete(f"/api/tasks/{tid}/routes/{tr_id}")
    r4 = client.post(f"/api/routes/{base.id}/reopen")
    check(r4.status_code == 200, f"无引用时可以撤回（{r4.status_code}）")
    # 撤回后它变成草稿：既不能被引用，也不该出现在候选清单
    cands = client.get(f"/api/tasks/{tid}/candidates").json()
    check(
        all(c["route_id"] != base.id for c in cands["items"]),
        "草稿不出现在可引用候选清单",
    )
    resp = client.post(
        f"/api/tasks/{tid}/routes",
        json={"items": [{"product": PRODUCT, "part": PART}], "create_baseline": False},
    )
    fail = resp.json()["failed"]
    check(
        bool(fail) and ("未发布" in fail[0]["reason"] or "只有草稿" in fail[0]["reason"]),
        f"撤回后引用被拒（{fail[0]['reason'] if fail else '没有失败'}）",
    )

    # ---------------------------------------------------------------- 4 工序校验
    print("\n[4] 工序行校验")
    d = R.create_draft(db, PRODUCT, PART, steps=[step(10, "车", 1.0)], created_by="test")
    db.commit()
    try:
        R.save_steps(db, d, [{"seq": 10, "process_code": "", "process_name": "", "unit_hours": 1}])
        check(False, "空工序行被拒")
    except ValueError as exc:
        check("没有选工序" in str(exc), f"空工序行被拒（{exc}）")
        db.rollback()
        d = db.get(ProcessRoute, d.id)

    try:
        R.save_steps(db, d, [step(10, "车", -1)])
        check(False, "负工时被拒")
    except ValueError as exc:
        check("负数" in str(exc), f"负工时被拒（{exc}）")
        db.rollback()
        d = db.get(ProcessRoute, d.id)

    R.save_steps(db, d, [step(10, "车", 1.0), step(10, "铣", 2.0), step(10, "刨", 3.0)])
    db.commit()
    seqs = [s.seq for s in sorted(d.steps, key=lambda x: x.seq)]
    check(seqs == [10, 11, 12], f"工序号撞号顺延（{seqs}）")
    check(len(d.steps) == 3, "三行都保存成功")

    # 只填编码没填工序名 → 用编码兜底，不留空名
    R.save_steps(db, d, [{"seq": 10, "process_code": "C001", "process_name": "", "unit_hours": 1}])
    db.commit()
    check(d.steps[0].process_name == "C001", "只填编码时用编码兜底工序名")

    # ---------------------------------------------------------------- 5 任务锁定
    print("\n[5] 已结案任务锁工艺")
    client.patch(f"/api/tasks/{tid}", json={"status": "closed"})
    resp = client.post(
        f"/api/tasks/{tid}/routes",
        json={"items": [{"product": PRODUCT, "part": PART}]},
    )
    check(resp.status_code == 400, f"结案后加路线被拒（{resp.status_code}）")
    trs = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == tid, TaskRoute.status == "active")
    ).all()
    if trs:
        resp = client.delete(f"/api/tasks/{tid}/routes/{trs[0].id}")
        check(resp.status_code == 400, f"结案后移除路线被拒（{resp.status_code}）")
        resp = client.post(f"/api/tasks/{tid}/routes/{trs[0].id}/fork")
        check(resp.status_code == 400, f"结案后拆副本被拒（{resp.status_code}）")
        resp = client.put(
            f"/api/tasks/{tid}/routes/{trs[0].id}/steps",
            json={"steps": [step(10, "车", 9.0)]},
        )
        check(resp.status_code == 400, f"结案后改工序被拒（{resp.status_code}）")
    client.patch(f"/api/tasks/{tid}", json={"status": "open"})

    # ---------------------------------------------------------------- 6 副本进阶
    print("\n[6] 副本进阶为基线")
    # 重新发布基线，再挂到任务上
    base2 = R.create_draft(db, PRODUCT, PART, steps=[step(10, "车", 1.0)], created_by="test")
    db.commit()
    R.publish(db, base2, published_by="tester")
    db.commit()
    resp = client.post(
        f"/api/tasks/{tid}/routes",
        json={"items": [{"product": PRODUCT, "part": PART}]},
    )
    body = resp.json()
    check(body["added"] == 1, "重新挂上已发布基线")
    tr2 = db.scalars(
        select(TaskRoute).where(TaskRoute.task_id == tid, TaskRoute.status == "active")
    ).first()
    tr2_id, base2_id = tr2.id, tr2.route_id

    # ---- 6.1 只存草稿：现行基准一个字节都不动
    print("\n[6.1] 只存草稿（不发布）")
    before_version = db.get(ProcessRoute, tr2.route_id).version
    before_hours = round(sum(s.unit_hours for s in db.get(ProcessRoute, tr2.route_id).steps), 4)
    resp = client.put(
        f"/api/tasks/{tid}/routes/{tr2_id}/steps",
        json={"steps": [step(10, "车", 7.7)], "publish": False},
    )
    body = resp.json()
    check(body.get("mode") == "draft", f"返回 mode=draft（{body.get('mode')}）")
    check(body.get("effective") is False, "标记为未生效")
    check(bool(body.get("draft_id")), "返回新草稿 id")
    check("尚未发布" in (body.get("message") or ""), f"提示草稿待发布（{body.get('message')}）")
    db.expire_all()
    cur = db.get(ProcessRoute, tr2.route_id)
    check(cur.status == "released", "现行基线仍是已发布状态")
    check(cur.version == before_version, f"现行版本没变（v{cur.version}）")
    check(
        abs(sum(s.unit_hours for s in cur.steps) - before_hours) < 1e-6,
        f"现行工序没被草稿覆盖（{before_hours}h）",
    )
    check(
        R.pick_baseline(db, PRODUCT, PART).id == cur.id,
        "任务仍指向原基线，草稿不参与核对",
    )

    fork = client.post(f"/api/tasks/{tid}/routes/{tr2_id}/fork").json()
    copy_id = fork["route_id"]
    check(copy_id != base2_id, "拆出副本成功")
    db.expire_all()
    check(db.get(ProcessRoute, copy_id).kind == "task", "副本 kind=task")

    # 内容与基线一致 → 合并回引用
    out = client.post(f"/api/tasks/{tid}/routes/{tr2_id}/promote").json()
    check(out["action"] == "merged", f"内容一致时合并回引用（{out['action']}）")
    db.expire_all()
    check(db.get(ProcessRoute, copy_id) is None, "合并后副本被删除")
    tr2 = db.get(TaskRoute, tr2_id)
    check(tr2.origin == "baseline" and tr2.route_id == base2_id, "任务回到引用基线")

    # 副本改成不同内容 → 进阶为新版基线
    fork = client.post(f"/api/tasks/{tid}/routes/{tr2_id}/fork").json()
    copy_id = fork["route_id"]
    resp = client.put(
        f"/api/tasks/{tid}/routes/{tr2_id}/steps",
        json={"steps": [step(10, "车", 2.5), step(20, "铣", 1.0)]},
    )
    check(resp.status_code == 200, "副本就地改成功")
    out = client.post(f"/api/tasks/{tid}/routes/{tr2_id}/promote").json()
    check(out["action"] == "promoted", f"进阶为新版（{out['action']}）")
    check(out["baseline_version"] >= 2, f"基线版本升到 v{out.get('baseline_version')}")
    db.expire_all()
    new_base = db.get(ProcessRoute, out["baseline_id"])
    check(new_base.status == "released", "进阶后新基线已发布")
    check(new_base.source == "promote", "基线标记来源 promote")
    check(f"{PREFIX}-1" in (new_base.remark or ""), "备注里记了进阶来源任务")
    check(abs(sum(s.unit_hours for s in new_base.steps) - 3.5) < 1e-6, "新基线工序来自副本")
    check(db.get(ProcessRoute, copy_id) is None, "进阶后副本被删除")

    # 旧基线应当已归档
    db.expire_all()
    old = db.get(ProcessRoute, base2_id)
    check(old is not None and old.status == "archived", f"旧基线归档（{old.status if old else '无'}）")

    # 副本清单接口
    resp = client.get(f"/api/routes/copies?q={PRODUCT}")
    check(resp.status_code == 200, "副本清单接口可用")
    check("identical_count" in resp.json(), "副本清单带「与基线一致」计数")

    # ---------------------------------------------------------------- 7 批量发布
    print("\n[7] 批量发布草稿")
    dry = client.post("/api/routes/publish-drafts", json={"dry_run": True, "limit": 5}).json()
    check(dry["dry_run"] is True, "批量发布支持预演")
    check("published_count" in dry and "skipped" in dry, "预演返回发布/跳过清单")

    # ---------------------------------------------------------------- 8 删任务
    print("\n[8] 删任务连带清副本")
    fork3 = client.post(f"/api/tasks/{tid}/routes/{tr2_id}/fork").json()
    orphan_candidate = fork3["route_id"]
    resp = client.delete(f"/api/tasks/{tid}")
    check(resp.status_code == 200, f"删任务成功（{resp.status_code}）")
    check(resp.json().get("deleted_copies") == 1, f"连带删了 {resp.json().get('deleted_copies')} 份副本")
    db.expire_all()
    check(db.get(ProcessRoute, orphan_candidate) is None, "副本没有留下孤儿")
    check(
        db.scalars(select(TaskRoute).where(TaskRoute.task_id == tid)).first() is None,
        "任务路线关联已清空",
    )

    # ---------------------------------------------------------------- 9 清理
    print("\n[9] 清理")
    cleanup(db)
    left = db.scalar(
        select(ProcessRoute.id).where(ProcessRoute.product == PRODUCT).limit(1)
    )
    check(left is None, "测试数据已清理")
    db.close()

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
