"""端到端联调：上传 -> 识别 -> 核对确认 -> 汇总 -> 勾稽 -> 导出。

**它会写生产库**，所以有两条硬约束（原先都没有，导致跑一次就多 4 张单据、
还会把真实单据的确认人和确认时间改成 tester）：

1. 只确认本次自己上传出来的单据，绝不碰库里已有的单；
2. 跑完自动清理自己造的数据（`--keep` 保留现场排查用）。

用法：python scripts/test_e2e.py [--keep] [--ym 2026-09]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
SAMPLES = sorted((BASE / "data" / "samples").glob("sample_*.jpg"))

KEEP = False


def cleanup(client, batch_id: int, sheet_ids: list[int], task_ids: list[int]) -> None:
    """只删本次联调造出来的东西：单据 → 明细 → 图片（含文件）→ 批次 → 空任务。

    删完必须重算受影响任务的派生字段：任务的单据数/实报工时是物化值，
    绕过服务层删单据不会触发重算，留下一个「永远对不上」的实报工时。
    """
    from app.db import SessionLocal
    from app.models import (
        AuditLog,
        Batch,
        Image,
        Sheet,
        TaskRoute,
        WorkItem,
        WorkTask,
    )
    from app.services.tasks import refresh_task

    db = SessionLocal()
    try:
        affected = {
            s.task_id
            for s in db.query(Sheet).filter(Sheet.id.in_(sheet_ids or [0])).all()
            if s.task_id
        }
        for sid in sheet_ids:
            db.query(WorkItem).filter(WorkItem.sheet_id == sid).delete()
            db.query(AuditLog).filter(AuditLog.sheet_id == sid).delete()
            db.query(Sheet).filter(Sheet.id == sid).delete()
        db.flush()
        imgs = db.query(Image).filter(Image.batch_id == batch_id).all()
        paths = [p for img in imgs for p in (img.path, img.processed_path, img.thumb_path) if p]
        n_img = len(imgs)
        db.query(Image).filter(Image.batch_id == batch_id).delete()
        db.flush()
        db.query(Batch).filter(Batch.id == batch_id).delete()
        # 归集时若新建了任务，删完单据它会变成空任务，一并清掉
        for tid in set(list(task_ids) + list(affected)):
            t = db.get(WorkTask, tid)
            if t is None:
                continue
            left = db.query(Sheet).filter(Sheet.task_id == tid).count()
            if left == 0:
                # 先拆 task_route：它挂着 route 与 task 两个外键，
                # 直接删任务会撞 FK。以前任务多半空着才没暴露这个顺序问题。
                db.query(TaskRoute).filter(TaskRoute.task_id == tid).delete()
                db.flush()
                db.delete(t)
            else:
                refresh_task(db, t)
        db.commit()
        for p in paths:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError:
                pass
        print(f"[清理] 批次 {batch_id}、单据 {len(sheet_ids)} 张、图片 {n_img} 张、空任务已清")
    finally:
        db.close()


def main() -> None:
    global KEEP
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留本次联调数据（排查用）")
    ap.add_argument("--ym", default="2026-09")
    args = ap.parse_args()
    KEEP = args.keep

    from app.main import app
    from app.db import SessionLocal
    from app.models import Sheet, WorkTask

    client = TestClient(app)

    periods = client.get("/api/periods").json()
    period = next((p for p in periods if p["ym"] == args.ym), periods[0])
    print(f"[1] 核算期 {period['ym']} (id={period['id']})")

    db = SessionLocal()
    try:
        before_task_ids = {t.id for t in db.query(WorkTask).all()}
    finally:
        db.close()

    r = client.post("/api/batches", json={"period_id": period["id"], "name": "联调批次"}).json()
    batch_id = r["id"]
    print(f"[2] 创建批次 id={batch_id}")

    sheet_ids: list[int] = []
    new_task_ids: list[int] = []

    files = [("files", (p.name, p.read_bytes(), "image/jpeg")) for p in SAMPLES]
    r = client.post(f"/api/batches/{batch_id}/images?auto_recognize=true", files=files).json()
    ok = sum(1 for x in r["results"] if x["ok"])
    print(f"[3] 上传 {len(SAMPLES)} 张，入队 {ok} 张")

    deadline = time.time() + 300
    while time.time() < deadline:
        imgs = client.get(f"/api/batches/{batch_id}/images").json()
        done = sum(1 for i in imgs if i["status"] == "done")
        failed = sum(1 for i in imgs if i["status"] == "failed")
        if done + failed == len(imgs):
            break
        time.sleep(3)
    print(f"[4] 识别完成 {done} 张，失败 {failed} 张")
    for i in imgs:
        if i["status"] == "failed":
            print("    失败：", i["filename"], i["error"][:200])

    # 只看本次批次产生的单据：原先按「整个期间」取，会把真实单据一并确认掉
    mine = client.get(f"/api/batches/{batch_id}/images").json()
    my_image_ids = {i["id"] for i in mine}
    db = SessionLocal()
    try:
        sheet_ids = [
            s.id for s in db.query(Sheet).all() if s.image_id in my_image_ids
        ]
    finally:
        db.close()
    sheets = {"items": [client.get(f"/api/sheets/{sid}").json() for sid in sheet_ids]}
    print(f"[5] 本次生成单据 {len(sheets['items'])} 张（期间内其它单据不参与本次联调）")
    for s in sheets["items"]:
        print(f"    单据 #{s['id']} 明细 {len(s.get('items') or [])} 行 工时 {s.get('total_hours') or 0:.2f}")

    verified = 0
    for s in sheets["items"]:
        detail = s
        for it in detail["items"]:
            print(
                f"      · {it['process']}/{it['part_name']} 单件{it['unit_hours']} x 合格{it['qualified_qty']}"
                f" = {it['total_hours']} 人={it['worker_name']} 日={it['finish_date']}"
                f" {'FLAGS:' + ','.join(f['code'] for f in it['flags']) if it['flags'] else 'OK'}"
            )
        resp = client.post(f"/api/sheets/{s['id']}/verify", json={"user": "tester", "force": False})
        if resp.status_code == 200:
            verified += 1
            tid = resp.json().get("task_id")
            if tid and tid not in before_task_ids:
                new_task_ids.append(tid)
        else:
            print(f"      确认失败：{resp.json().get('detail')}")
    print(f"[6] 已确认 {verified}/{len(sheets['items'])} 张")
    if verified == 0:
        print("[!] 本次没有任何单据确认成功，后面的汇总/勾稽只反映库里原有数据")

    summary = client.get("/api/summary", params={"ym": period["ym"]}).json()
    print(f"[7] 汇总：{summary['totals']}")
    for p in summary["persons"]:
        procs = "/".join(f"{x['process']}{x['hours']:.2f}" for x in p["processes"])
        print(f"    {p['worker_name']:<6} {p['hours']:>8.2f} h  ({procs})")

    matrix = client.get("/api/summary/matrix", params={"ym": period["ym"]}).json()
    print(f"[8] 矩阵 {len(matrix['rows'])} 人 x {len(matrix['processes'])} 工序 总计 {matrix['grand_total']:.2f} h")

    rec = client.get("/api/reconcile", params={"ym": period["ym"]}).json()
    print(f"[9] 勾稽：{'平' if rec['balanced'] else '不平'}  明细{rec['levels']['detail_sum']:.2f} "
          f"人员{rec['levels']['person_sum']:.2f} 差{rec['levels']['diff']:.2f}")
    for issue in rec["issues"]:
        print(f"    [{issue['level']}] {issue['code']} {issue['message']}")

    # 导出接口会把文件写进 data/exports（真实留档目录）。联调产生的文件里
    # 混着测试单据，留在「历史导出」里会被当成真账，所以跑完连同归档一起删。
    before_files = {p.name for p in (BASE / "data" / "exports").glob("*.xlsx")}
    resp = client.get("/api/export", params={"ym": period["ym"]})
    print(f"[10] 导出 HTTP {resp.status_code}")
    if resp.status_code == 200:
        import tempfile

        out = Path(tempfile.gettempdir()) / "hy_e2e_check.xlsx"
        out.write_bytes(resp.content)
        print(f"     已保存 {out} ({len(resp.content)/1024:.1f} KB)")
        verify_workbook(out)
        after_files = {p.name for p in (BASE / "data" / "exports").glob("*.xlsx")}
        for name in after_files - before_files:
            (BASE / "data" / "exports" / name).unlink(missing_ok=True)
            print(f"     [清理] 删除联调产生的归档 {name}")
    else:
        print("     ", resp.json())

    if KEEP:
        print(f"[!] --keep：批次 {batch_id}、单据 {sheet_ids} 保留在库里，排查完请自行清理")
    else:
        cleanup(client, batch_id, sheet_ids, new_task_ids)


def verify_workbook(path: Path) -> None:
    """校验导出文件：sheet 名、合计、是否存在 days 乱码。"""
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    print(f"[11] 导出校验：{len(wb.sheetnames)} 个 sheet -> {wb.sheetnames}")
    days_bug = 0
    total_row_hours = {}
    for name in wb.sheetnames:
        if name in ("总工时", "口径说明"):
            continue
        ws = wb[name]
        title = ws.cell(1, 1).value
        total = None
        for row in ws.iter_rows(min_row=4, max_col=12):
            if row[6].value == "合计":
                total = row[7].value
            for cell in row:
                if isinstance(cell.value, str) and "days," in cell.value:
                    days_bug += 1
                if cell.number_format and "d" in cell.number_format and "yyyy" not in cell.number_format:
                    pass
        total_row_hours[name] = total
        print(f"     {name}: {title} 合计={total}")
    print(f"     'days' 乱码单元格：{days_bug} 个")
    assert days_bug == 0, "导出文件仍存在 days 乱码"

    ws = wb["总工时"]
    grand = None
    for row in ws.iter_rows(min_row=4, max_col=5):
        if row[1].value == "总计":
            grand = row[3].value
    print(f"     总工时表总计 = {grand}")


if __name__ == "__main__":
    main()
