"""工资核算测试：单价留档、月度工资、工资表导出。

原则同其它脚本：只用自己造的数据（前缀 ZZPAY、核算期 2099-01），
跑完连同核算期一起清干净，不碰真实月份的账。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.domain import export_excel  # noqa: E402
from app.domain.wages import compute_wages, rate_cards  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    Batch,
    Image,
    Period,
    Sheet,
    Worker,
    WorkerRate,
    WorkItem,
)

TAG = "ZZPAY"
YM = "2099-01"
YM_LATER = "2099-04"
WORKER = f"{TAG}工人"

PASSED: list[str] = []
FAILED: list[str] = []
EXPORTS: list[Path] = []

# 导出目录改到临时目录。后端接口每被调用一次就会往 EXPORT_DIR 落一个
# 「2099-01工时工资表_时间戳.xlsx」—— 跑几遍测试，生产导出目录里就堆一串
# 2099 年的假账单，而且会直接出现在界面的「最近导出」里。
import tempfile  # noqa: E402

TEST_EXPORT_DIR = Path(tempfile.mkdtemp(prefix="hy_wage_export_"))
export_excel.EXPORT_DIR = str(TEST_EXPORT_DIR)


def check(ok: bool, label: str) -> None:
    if ok:
        PASSED.append(label)
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def section(text: str) -> None:
    print(f"\n{text}")


def cleanup() -> None:
    db = SessionLocal()
    for p in db.scalars(select(Period).where(Period.ym.in_([YM, YM_LATER]))).all():
        for b in db.scalars(select(Batch).where(Batch.period_id == p.id)).all():
            for img in db.scalars(select(Image).where(Image.batch_id == b.id)).all():
                for sh in db.scalars(select(Sheet).where(Sheet.image_id == img.id)).all():
                    for it in db.scalars(select(WorkItem).where(WorkItem.sheet_id == sh.id)).all():
                        db.delete(it)
                    db.flush()
                    db.delete(sh)
                db.flush()
                db.delete(img)
            db.flush()
            db.delete(b)
        db.flush()
        db.delete(p)
    for w in db.scalars(select(Worker).where(Worker.name.like(f"{TAG}%"))).all():
        for r in db.scalars(select(WorkerRate).where(WorkerRate.worker_id == w.id)).all():
            db.add(
                AuditLog(
                    entity="worker_rate_cleanup",
                    entity_id=r.id,
                    field="delete",
                    old_value=f"{r.effective_ym}:{r.rate}",
                    new_value="",
                    user="test",
                )
            )
            db.delete(r)
        db.flush()
        db.delete(w)
    db.commit()
    db.close()
    for f in EXPORTS:
        if f.exists():
            f.unlink()


def main() -> int:
    cleanup()
    db = SessionLocal()

    section("0 造测试数据：2099-01 一张已确认报工单、一个工人、两行明细")
    period = Period(ym=YM, title="ZZPAY 测试期", status="open")
    db.add(period)
    db.flush()
    batch = Batch(period_id=period.id, name=f"{TAG}批次")
    db.add(batch)
    db.flush()
    img = Image(
        batch_id=batch.id, filename=f"{TAG}.jpg", path="data/none.jpg",
        sha256=f"{TAG}1", status="done",
    )
    db.add(img)
    db.flush()
    worker = Worker(name=WORKER, default_process="铣工", team="2车间", active=True)
    db.add(worker)
    db.flush()
    sheet = Sheet(
        image_id=img.id, period_id=period.id, form_type="加工路线单",
        status="verified", total_hours=15.0,
        header_json='{"product_name": "ZZPAY产品", "part_name": "ZZPAY件名"}',
        verify_user="tester",
    )
    db.add(sheet)
    db.flush()
    db.add(
        WorkItem(
            sheet_id=sheet.id, seq=1, product_name=f"{TAG}产品", part_name=f"{TAG}件名",
            process="铣", process_std="铣", unit_hours=2.5, qualified_qty=4.0,
            total_hours=10.0, worker_id=worker.id, inspector="杜文杰",
        )
    )
    db.add(
        WorkItem(
            sheet_id=sheet.id, seq=2, product_name=f"{TAG}产品", part_name=f"{TAG}件名2",
            process="铣", process_std="铣", unit_hours=2.5, qualified_qty=2.0,
            total_hours=5.0, worker_id=worker.id, inspector="杜文杰",
        )
    )
    db.commit()
    db.expire_all()
    check(True, f"核算期 {YM} / 明细 2 行 / 工时 15h")

    section("1 没设单价：工时算得出来，金额为 0 且被标成「未设单价」")
    d = compute_wages(db, YM)
    person = next((p for p in d["persons"] if p["worker_name"] == WORKER), None)
    check(person is not None, "该工人出现在工资表里")
    check(person and abs(person["hours"] - 15.0) < 1e-6, f"工时 15h（{person and person['hours']}）")
    check(person and person["rate"] == 0 and not person["has_rate"], "未设单价时 rate=0、has_rate=False")
    check(d["totals"]["missing_rate"] >= 1, f"未设单价人数被统计（{d['totals']['missing_rate']}）")

    client = TestClient(app)
    section("2 写入单价：工资 = 工时 × 单价")
    r = client.put(
        "/api/wages/rates",
        json={"ym": YM, "items": [{"worker_id": worker.id, "rate": 40}], "note": "测试单价"},
    )
    check(r.status_code == 200, f"写单价 200（{r.status_code}）")
    d = compute_wages(db, YM)
    person = next(p for p in d["persons"] if p["worker_name"] == WORKER)
    check(person["rate"] == 40.0, f"取到单价 40（{person['rate']}）")
    check(abs(person["amount"] - 600.0) < 1e-6, f"15h × 40 = 600 元（{person['amount']}）")

    section("3 单价按生效月留档：后月调价不改前月工资")
    client.put(
        "/api/wages/rates",
        json={"ym": YM_LATER, "items": [{"worker_id": worker.id, "rate": 50}], "note": "后月调价"},
    )
    d = compute_wages(db, YM)
    person = next(p for p in d["persons"] if p["worker_name"] == WORKER)
    check(person["rate"] == 40.0, f"{YM} 仍是 40 元（{person['rate']}）")
    check(abs(person["amount"] - 600.0) < 1e-6, f"{YM} 工资仍是 600（{person['amount']}）")
    cards = {c["worker_id"]: c for c in rate_cards(db, YM_LATER)}
    check(cards[worker.id]["rate"] == 50.0, f"{YM_LATER} 取 50 元（{cards[worker.id]['rate']}）")
    check(cards[worker.id]["effective_ym"] == YM_LATER, "生效月记在 2099-04")

    section("4 同月改价是「改这条」而不是再插一条，且留痕")
    before = len(
        db.scalars(
            select(WorkerRate).where(
                WorkerRate.worker_id == worker.id, WorkerRate.effective_ym == YM
            )
        ).all()
    )
    r = client.put(
        "/api/wages/rates",
        json={"ym": YM, "items": [{"worker_id": worker.id, "rate": 42}]},
    )
    check(r.status_code == 200 and r.json()["changed"] == 1, "改价返回 changed=1")
    db.expire_all()
    rows = db.scalars(
        select(WorkerRate).where(
            WorkerRate.worker_id == worker.id, WorkerRate.effective_ym == YM
        )
    ).all()
    check(len(rows) == before == 1, f"同月只有一条记录（{len(rows)}）")
    check(abs(float(rows[0].rate) - 42.0) < 1e-6, f"记录改成 42（{rows[0].rate}）")
    audits = db.scalars(
        select(AuditLog).where(
            AuditLog.entity == "worker_rate",
            AuditLog.field == f"rate@{YM}",
        )
    ).all()
    check(len(audits) >= 2, f"两次写价都有审计（{len(audits)} 条）")

    section("5 负数单价被拒")
    r = client.put(
        "/api/wages/rates",
        json={"ym": YM, "items": [{"worker_id": worker.id, "rate": -5}]},
    )
    check(r.status_code == 400, f"负数单价返回 400（{r.status_code}）")

    section("6 不存在的核算期：工资接口 404")
    r = client.get("/api/wages", params={"ym": "1999-01"})
    check(r.status_code == 404, f"未知核算期 404（{r.status_code}）")

    section("7 工资表导出：原格式不动，右侧追加单价与金额")
    r = client.get("/api/wages/export", params={"ym": YM})
    check(r.status_code == 200, f"导出 200（{r.status_code}）")
    out = TEST_EXPORT_DIR / f"{TAG}_wage.xlsx"
    out.write_bytes(r.content)
    EXPORTS.append(out)
    wb = load_workbook(out, data_only=True)
    check("工资汇总" in wb.sheetnames, "含「工资汇总」表")
    check("总工时" in wb.sheetnames, "保留原「总工时」表")
    ws = wb[WORKER] if WORKER in wb.sheetnames else wb.worksheets[0]
    heads = [c.value for c in ws[3]]
    check(heads[:11] == [
        "序号", "产品名称/代号", "批次", "件名", "工序", "合格件数",
        "单件工时", "合计工时", "完成时间", "检验员", "备注",
    ], f"前 11 列与原表一致（{heads[:11]}）")
    check(heads[12] == "工时单价" and heads[13] == "工时工资", f"追加两列（{heads[12]}/{heads[13]}）")
    # 数据行：工时 10 / 5，单价 42，金额 420 / 210
    row1 = [c.value for c in ws[4]]
    check(abs(float(row1[7]) - 10.0) < 1e-6, f"第 1 行工时 10（{row1[7]}）")
    check(abs(float(row1[12]) - 42.0) < 1e-6, f"第 1 行单价 42（{row1[12]}）")
    check(abs(float(row1[13]) - 420.0) < 1e-6, f"第 1 行金额 420（{row1[13]}）")
    pay = wb["工资汇总"]
    pay_rows = [[c.value for c in row] for row in pay.iter_rows(min_row=4, max_row=pay.max_row)]
    me = next((r for r in pay_rows if r[1] == WORKER), None)
    check(me is not None, "工资汇总里有这个工人")
    check(me and abs(float(me[6]) - 630.0) < 1e-6, f"汇总金额 630（{me and me[6]}）")
    total = next((r for r in pay_rows if r[1] == "总计"), None)
    check(total is not None and abs(float(total[6]) - 630.0) < 1e-6, "总计金额 = 630")

    section("8 未设单价在导出里写明「未设单价」而不是 0")
    # 造第二个工人：有工时、没单价
    w2 = Worker(name=f"{TAG}工人2", default_process="钳工", team="2车间", active=True)
    db.add(w2)
    db.flush()
    db.add(
        WorkItem(
            sheet_id=sheet.id, seq=3, product_name=f"{TAG}产品", part_name=f"{TAG}件名3",
            process="钳", process_std="钳", unit_hours=4.0, qualified_qty=1.0,
            total_hours=4.0, worker_id=w2.id, inspector="杜文杰",
        )
    )
    db.commit()
    db.expire_all()

    # 有人没设单价 → 正式工资表不许导：那张表上他会是 0 元，而表面看不出少了谁
    r_block = client.get("/api/wages/export", params={"ym": YM})
    check(r_block.status_code == 400, f"有人没设单价时不许导正式表（{r_block.status_code}）")
    check("工时单价" in r_block.json().get("detail", ""), "拒绝理由写明是单价问题")

    # 预览版照旧可以导：核对数据需要它，文件名/提示里已标明不能作为发放依据
    r = client.get("/api/wages/export", params={"ym": YM, "preview": True})
    check(r.status_code == 200, f"预览版可以导出（{r.status_code}）")
    out2 = TEST_EXPORT_DIR / f"{TAG}_wage2.xlsx"
    out2.write_bytes(r.content)
    EXPORTS.append(out2)
    wb2 = load_workbook(out2, data_only=True)
    rows = [[c.value for c in row] for row in wb2["工资汇总"].iter_rows(min_row=4, max_row=wb2["工资汇总"].max_row)]
    me2 = next((x for x in rows if x[1] == f"{TAG}工人2"), None)
    check(me2 is not None and me2[5] == "未设单价", f"未设单价写成文字（{me2 and me2[5]}）")

    total_hours = compute_wages(db, YM)["totals"]["hours"]
    check(abs(total_hours - 19.0) < 1e-6, f"总工时含未定价的人也照实统计（{total_hours}h）")

    section("9 有单据待核对时，工资表同样不许导（半成品不能拿去发钱）")
    img2 = Image(
        batch_id=batch.id, filename=f"{TAG}_p.jpg", path="data/none.jpg",
        sha256=f"{TAG}2", status="done",
    )
    db.add(img2)
    db.flush()
    pending = Sheet(
        image_id=img2.id, period_id=period.id, form_type="加工路线单",
        status="recognized", total_hours=3.0,
        header_json='{"product_name": "ZZPAY产品"}',
    )
    db.add(pending)
    db.flush()
    db.add(
        WorkItem(
            sheet_id=pending.id, seq=1, product_name=f"{TAG}产品", part_name=f"{TAG}件名4",
            process="铣", process_std="铣", unit_hours=3.0, qualified_qty=1.0,
            total_hours=3.0, worker_id=worker.id, inspector="杜文杰",
        )
    )
    db.commit()
    db.expire_all()
    r_pending = client.get("/api/wages/export", params={"ym": YM})
    check(r_pending.status_code == 400, f"有待核对单据时不许导（{r_pending.status_code}）")
    check("待核对" in r_pending.json().get("detail", ""), "拒绝理由写明还有几张待核对")
    check(
        client.get("/api/wages/export", params={"ym": YM, "preview": True}).status_code == 200,
        "预览版仍可导出，用于先看版式",
    )
    # 作废的单不算「待办」：否则一张废单能永久挡住整月导出，逼人去关校验
    pending.status = "rejected"
    db.commit()
    db.expire_all()
    pending_count = compute_wages(db, YM)["totals"]["pending_sheets"]
    check(pending_count == 0, f"作废单不再计入「待核对」（{pending_count}）")

    cleanup()
    print("\n" + "=" * 60)
    if FAILED:
        print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print(f"全部通过（{len(PASSED)} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
