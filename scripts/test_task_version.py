"""任务基本信息编辑 + 版本号验证。

任务的基本信息是成本归集与月度汇总的口径（数量、交期、期间、客户、任务号）。
「这单当初按多少台下的」如果答不上来，事后对账就没有锚点，所以要版本号。
本组测的是：

1. 建单即 v1，没有修订记录；
2. 改口径（数量/交期/期间/客户/任务号/订单名/下单日）升一版，并记清改了什么；
3. 只改状态、备注**不**升版 —— 它们是流程动作与说明，不是口径；
4. 原样再保存一次不升版（不能因为点了保存就涨版本）；
5. 一次改多项只升一版（一次操作 = 一个版本），changes 逐条列出；
6. 任务号手工改撞号必须报错，不能被悄悄顺延成 A-2；
7. 列表带 version/revised_at，详情带 revisions 且新的在前。

用法：.venv\\Scripts\\python.exe scripts/test_task_version.py
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
    Batch,
    Image,
    Period,
    Sheet,
    TaskRevision,
    TaskRoute,
    WorkItem,
    WorkTask,
)

PASSED: list[str] = []
FAILED: list[str] = []
PREFIX = "ZZVER"
YM = "2031-05"


def check(ok: bool, label: str) -> None:
    if ok:
        PASSED.append(label)
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [!!] {label}")


def cleanup(db, client) -> None:
    """只删本脚本造的数据（按前缀），绝不动库里既有单据与任务。"""
    # 先清单据：任务上还挂着单据时删不掉（服务层会拒），且单据是 Sheet 的外键宿主
    mine = [
        s
        for s in db.scalars(select(Sheet)).all()
        if s.image is not None and (s.image.filename or "").startswith(PREFIX)
    ]
    for s in mine:
        img_id = s.image_id
        db.query(WorkItem).filter(WorkItem.sheet_id == s.id).delete(synchronize_session=False)
        db.delete(s)
        db.flush()
        img = db.get(Image, img_id) if img_id else None
        if img is not None:
            bid = img.batch_id
            db.delete(img)
            db.flush()
            b = db.get(Batch, bid)
            if b is not None:
                db.delete(b)
    db.flush()
    for t in db.scalars(select(WorkTask).where(WorkTask.task_no.like(f"{PREFIX}%"))).all():
        db.query(TaskRoute).filter(TaskRoute.task_id == t.id).delete(synchronize_session=False)
        db.query(TaskRevision).filter(TaskRevision.task_id == t.id).delete(
            synchronize_session=False
        )
        db.flush()
        db.delete(t)
        db.flush()
    db.commit()
    # 测试期间也要带走：留着会让体检的「核算期」数量越跑越多。
    # 只删没有任何引用的 2031-* 测试期，真实期间一概不碰。
    db.execute(
        text(
            "DELETE FROM period WHERE ym LIKE '2031-%' "
            "AND id NOT IN (SELECT period_id FROM batch WHERE period_id IS NOT NULL) "
            "AND id NOT IN (SELECT period_id FROM work_task WHERE period_id IS NOT NULL) "
            "AND id NOT IN (SELECT period_id FROM sheet WHERE period_id IS NOT NULL)"
        )
    )
    db.commit()


def mk_period(db, ym: str) -> Period:
    p = db.scalars(select(Period).where(Period.ym == ym)).first()
    if p is None:
        p = Period(ym=ym, title=f"{ym} 测试期", status="open")
        db.add(p)
        db.flush()
    return p


def mk_sheet(db, period: Period, task_id: int, tag: str) -> Sheet:
    """造一张已确认、已归到任务上的最小单据（只为触发跨期判定）。"""
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
        header_json='{"product_name": "版本测试产品", "batch_no": "B-%s"}' % tag,
        total_hours=2.0,
        verify_user="tester",
        task_id=task_id,
    )
    db.add(sh)
    db.flush()
    db.add(
        WorkItem(
            sheet_id=sh.id,
            seq=1,
            product_name="版本测试产品",
            batch_no=f"B-{tag}",
            part_name="版本件",
            process="车",
            process_std="车",
            process_code="车",
            qualified_qty=1,
            unit_hours=2.0,
            total_hours=2.0,
        )
    )
    db.commit()
    return sh


def main() -> int:
    db = SessionLocal()
    client = TestClient(app)
    cleanup(db, client)
    p1 = mk_period(db, YM)
    p2 = mk_period(db, "2031-06")
    db.commit()

    print("\n[1] 建单即 v1，无修订记录")
    r = client.post(
        "/api/tasks",
        json={
            "task_no": f"{PREFIX}-A",
            "title": "版本测试订单",
            "customer": "甲客户",
            "product": "",
            "qty": 100,
            "period_id": p1.id,
            "due_date": "2031-06-30",
        },
    )
    check(r.status_code == 200, f"建单成功 {r.status_code}")
    A = r.json()["task"]
    check(A["version"] == 1, f"新建任务版本 = v1（实得 v{A['version']}）")
    check(A["revisions"] == [], "新建任务没有修订记录")

    print("\n[2] 改数量 → 升版，记清改了什么")
    r = client.patch(
        f"/api/tasks/{A['id']}",
        json={"qty": 120, "user": "tester", "note": "客户追加 20 台"},
    )
    check(r.status_code == 200, f"改数量成功 {r.status_code}")
    body = r.json()
    check(body["bumped"] is True, "改数量被判为升版")
    check(body["task"]["version"] == 2, f"版本升到 v2（实得 v{body['task']['version']}）")
    ch = {c["field"]: c for c in body["changes"]}
    check(set(ch) == {"qty"}, f"只记录了变化的那一项（实得 {sorted(ch)}）")
    check(
        ch["qty"]["old"] == 100 and ch["qty"]["new"] == 120,
        f"新旧值正确（{ch['qty']['old']} → {ch['qty']['new']}）",
    )
    check(ch["qty"]["label"] == "订单数量", "字段有中文标签，界面不用自己翻译")
    check("订单数量 100→120" in body["message"], f"回执直接可读：{body['message']}")
    check(body["task"]["revised_by"] == "tester", "记下了是谁改的")
    revs = body["task"]["revisions"]
    check(len(revs) == 1 and revs[0]["version"] == 2, "修订记录里有 v2 一条")
    check(revs[0]["note"] == "客户追加 20 台", "修订说明原样保存")

    print("\n[3] 原样再保存一次 → 不升版")
    r = client.patch(f"/api/tasks/{A['id']}", json={"qty": 120, "due_date": "2031-06-30"})
    check(r.json()["bumped"] is False, "内容没变不升版（点保存不等于改口径）")
    check(r.json()["task"]["version"] == 2, "版本仍为 v2")

    print("\n[4] 只改状态/备注 → 不升版")
    r = client.patch(f"/api/tasks/{A['id']}", json={"status": "open", "remark": "加急"})
    check(r.json()["bumped"] is False, "改状态与备注不升版")
    check(r.json()["task"]["status"] == "open", "状态确实生效")
    check(r.json()["task"]["remark"] == "加急", "备注确实保存")
    check(r.json()["task"]["version"] == 2, "版本未被状态/备注带涨")

    print("\n[5] 一次改多项 → 只升一版，逐条列变化")
    r = client.patch(
        f"/api/tasks/{A['id']}",
        json={
            "due_date": "2031-07-15",
            "period_id": p2.id,
            "customer": "乙客户",
            "user": "planner",
        },
    )
    body = r.json()
    check(body["task"]["version"] == 3, f"一次操作只升一版（实得 v{body['task']['version']}）")
    fields = sorted(c["field"] for c in body["changes"])
    check(fields == ["customer", "due_date", "period_id"], f"三项都记下了（{fields}）")
    per = next(c for c in body["changes"] if c["field"] == "period_id")
    check(per["new"] == "2031-06", f"期间记成 YYYY-MM 而不是 id（{per['old']} → {per['new']}）")
    check(len(body["task"]["revisions"]) == 2, "修订记录 2 条（v2、v3）")
    check(body.get("warnings") == [], "没有已归集单据时不误报跨期")

    print("\n[6] 任务号撞号 → 拒绝，不能悄悄顺延")
    B = client.post(
        "/api/tasks", json={"task_no": f"{PREFIX}-B", "title": "另一个订单", "qty": 10}
    ).json()["task"]
    r = client.patch(f"/api/tasks/{A['id']}", json={"task_no": f"{PREFIX}-B"})
    check(r.status_code == 400, f"撞号被拒（{r.status_code}）")
    check("已被其它任务占用" in (r.json().get("detail") or ""), f"提示说清原因：{r.json().get('detail')}")
    db.expire_all()
    check(
        db.get(WorkTask, A["id"]).task_no == f"{PREFIX}-A",
        "任务号没有被偷改（不是变成 -B-2 之类）",
    )

    print("\n[7] 改任务号成功 → 升版")
    r = client.patch(
        f"/api/tasks/{A['id']}", json={"task_no": f"{PREFIX}-A1", "user": "reviser"}
    )
    body = r.json()
    check(body["task"]["task_no"] == f"{PREFIX}-A1", "任务号已改")
    check(body["task"]["version"] == 4, f"改任务号也升版（v{body['task']['version']}）")

    print("\n[8] 列表与详情口径一致")
    rows = client.get("/api/tasks", params={"q": PREFIX, "size": 50}).json()["items"]
    row_a = next((x for x in rows if x["id"] == A["id"]), None)
    check(row_a is not None, "列表里能找到该任务")
    check(row_a["version"] == 4, f"列表带版本号（v{row_a['version']}）")
    check(bool(row_a["revised_at"]), "列表带最近修订时间")
    check(row_a["revised_by"] == "reviser", f"列表带修订人（{row_a['revised_by']}）")
    d = client.get(f"/api/tasks/{A['id']}").json()
    check(d["version"] == row_a["version"], "详情与列表版本一致")
    check(len(d["revisions"]) == 3, f"详情带 3 条修订（实得 {len(d['revisions'])}）")
    check(d["revisions"][0]["version"] == 4, "修订记录新的在前")
    r = client.get(f"/api/tasks/{A['id']}/revisions").json()
    check(r["version"] == 4 and len(r["items"]) == 3, "独立修订史接口可用")

    print("\n[9] 修订史摘要可读")
    summary = r["items"][0]["summary"]
    check("任务号" in summary and "→" in summary, f"摘要形如「字段 旧→新」：{summary}")

    print("\n[10] 改核算期间 → 提醒跨期影响")
    pA = mk_period(db, "2031-07")
    pB = mk_period(db, "2031-08")
    db.commit()
    C = client.post(
        "/api/tasks",
        json={"task_no": f"{PREFIX}-C", "title": "跨期测试", "qty": 5, "period_id": pA.id},
    ).json()["task"]
    mk_sheet(db, pA, C["id"], "A1")
    # 物化字段（单据数）靠刷新，不然列表上还是 0
    client.post(f"/api/tasks/{C['id']}/rebuild")
    r = client.patch(f"/api/tasks/{C['id']}", json={"period_id": pB.id, "user": "planner"})
    body = r.json()
    check(body["bumped"] is True, "改期间升版")
    check(len(body["warnings"]) == 1, f"给出 1 条后果提醒（{body.get('warnings')}）")
    check("1 张" in (body["warnings"] or [""])[0], "提醒写明有几张单据受影响")
    check("跨期" in (body["warnings"] or [""])[0], "提醒说清是跨期问题")

    cleanup(db, client)
    db.close()
    print(f"\n=== 通过 {len(PASSED)} 项，失败 {len(FAILED)} 项 ===")
    if FAILED:
        for f in FAILED:
            print("  FAIL:", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
