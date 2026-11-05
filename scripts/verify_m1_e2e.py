"""端到端验证 M1：同一张纸既扫码报工、又拍照识别，会不会算两次工时。

这个洞是整轮体检里唯一「直接多发钱」的：计划员扫完码，文员又拿同一张纸
拍了一遍，识别流水线照常建单、照常入账。M1 的修法是让识别侧读纸面单号、
反查预印单，命中「已报工」就把识别单标成不计工时的留档。

本脚本要证明的就是这条链在**真流水线**里通了（不是只测那个函数）：
    渲染预印单 → 造一张 status=reported 的预印单 → 上传这张图
    → recognize_image()（真 OCR、真抽取、真入库）→ 看识别单落成什么状态

全程在**生产库的副本**上跑（同 run_tests.py 的做法），跑完删副本。

用法：
    .venv/Scripts/python.exe scripts/verify_m1_e2e.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "data" / "worktime.db"
DST = Path(tempfile.gettempdir()) / "hy_m1_verify.db"

if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# 必须在 import app.* 之前指向副本：config.DB_URL 是导入时求值的
for suffix in ("", "-wal", "-shm"):
    p = Path(str(DST) + suffix)
    if p.exists():
        p.unlink()
with sqlite3.connect(f"file:{SRC}?mode=ro", uri=True) as s:
    d = sqlite3.connect(DST)
    try:
        s.backup(d)
    finally:
        d.close()
os.environ["WT_DB_URL"] = f"sqlite:///{DST.as_posix()}"

import datetime as dt  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.domain import qr  # noqa: E402
from app.models import Batch, Image, Period, PrintSheet, PrintSheetLine, Sheet  # noqa: E402
from app.render import qr_stamp  # noqa: E402
from app.render import route_form as form  # noqa: E402
from app.services import card_print as CP  # noqa: E402
from app.services import pipeline  # noqa: E402

FORM_NO = "RG9911-00077"
TOKEN = "e2e0beef"
CARD = {
    "product": "端到端验证产品",
    "part": "整机装配",
    "code": "99.01.001",
    "material": "304",
    "qty_per_unit": 1,
    "print_qty": 3,
    "steps": [
        {"process_name": "下料", "unit_hours": 0.5, "seq": 10},
        {"process_name": "车削", "unit_hours": 2.0, "seq": 20},
    ],
}


def render_sheet(path: Path) -> None:
    """按真实版式渲染一张**带码**的预印单图片（就是工人手里那张纸）。"""
    values = CP.card_values(CARD, task={"qty": 3, "batch_no": "B-E2E", "dispatcher": "验证"})
    page = form.render_pages(values, dpi=300)[0]
    qr_stamp.stamp(page, FORM_NO, TOKEN, 300)
    path.parent.mkdir(parents=True, exist_ok=True)
    page.save(path)


def main() -> int:
    db = SessionLocal()
    passed, failed = [], []

    def check(ok: bool, label: str) -> None:
        (passed if ok else failed).append(label)
        print(f"  [{'OK' if ok else 'FAIL'}] {label}")

    try:
        # 清掉上次残留（副本里可能已有同名单号）
        for f in db.scalars(select(PrintSheet).where(PrintSheet.form_no == FORM_NO)):
            db.delete(f)
        db.commit()

        period = db.scalar(select(Period).order_by(Period.ym.desc()))
        if period is None:
            print("副本库里没有核算期，无法验证")
            return 1
        batch = Batch(period_id=period.id, name="ZZE2E批次")
        db.add(batch)
        db.flush()

        # 一张「已扫码报工」的预印单 —— 这正是重复计工时的现场条件
        fs = PrintSheet(
            form_no=FORM_NO,
            task_id=None,
            product_name=CARD["product"],
            batch_no="B-E2E",
            part_name=CARD["part"],
            part_no=CARD["code"],
            qty_plan=3,
            part_qty=3,
            total_hours=7.5,
            token=TOKEN,
            status="reported",
            printed_by="e2e",
            reported_at=dt.datetime.now(),
            reported_by="e2e",
        )
        db.add(fs)
        db.flush()
        for i, s in enumerate(CARD["steps"], start=1):
            db.add(
                PrintSheetLine(
                    form_id=fs.id, row_no=i, seq=s["seq"],
                    process_name=s["process_name"], unit_hours=s["unit_hours"],
                    qty_plan=3, total_hours=s["unit_hours"] * 3,
                )
            )
        db.commit()

        # 同一张纸，被拍了一遍
        img_path = Path(tempfile.gettempdir()) / "hy_m1_sheet.png"
        render_sheet(img_path)
        img = Image(
            batch_id=batch.id,
            filename="e2e_sheet.png",
            path=str(img_path),
            sha256="e2e" + dt.datetime.now().isoformat(),
            size_bytes=img_path.stat().st_size,
            status="pending",
        )
        db.add(img)
        db.commit()
        print(f"\n预印单 {FORM_NO} 已报工；把同一张纸当照片送进识别流水线（真 OCR）…")
        pipeline.recognize_image(img.id)

        db.expire_all()
        img = db.get(Image, img.id)
        sheet = db.scalar(select(Sheet).where(Sheet.image_id == img.id))
        check(sheet is not None, "识别单已生成")
        if sheet is None:
            return 1
        check(
            sheet.print_form_no == FORM_NO,
            f"识别侧读出了纸面单号并绑定（{sheet.print_form_no or '空'}）",
        )
        check(
            sheet.status == "rejected",
            f"已报工的纸，识别单自动作废留档（{sheet.status}）",
        )
        check("已扫码报工" in (sheet.reject_reason or ""), f"作废原因写明（{sheet.reject_reason}）")

        # 反证：把预印单改回「待报工」，同一张纸重识别应当正常入账等待核对
        fs = db.scalar(select(PrintSheet).where(PrintSheet.form_no == FORM_NO))
        fs.status = "printed"
        fs.reported_at = None
        db.commit()
        from app.models import AuditLog  # noqa: F401  仅为了确保模块已加载

        img.status = "pending"
        img.error = ""
        db.commit()
        print("\n把这张单改回「待报工」，重识别一次（应正常待核对）…")
        pipeline.recognize_image(img.id)
        db.expire_all()
        sheet = db.scalar(select(Sheet).where(Sheet.image_id == img.id))
        check(
            sheet.status == "recognized",
            f"未报工的纸照常进待核对（{sheet.status}）",
        )
        check(sheet.print_form_no == FORM_NO, "绑定关系仍在（重识别不清空）")

        # 扫码侧反向互斥：识别已入账的纸，扫码必须被拦住
        sheet.status = "verified"
        db.commit()
        from app.services import print_sheet as PS

        res = PS.scan(db, qr.form_payload(FORM_NO, TOKEN))
        # scan 只拦「已报工」，反向互斥在 submit：
        sub = PS.submit(
            db, FORM_NO, [{"row_no": 1, "qty_done": 1, "qualified": 1, "worker_name": "x"}],
            by="e2e", sign_verified=True,
        )
        check(
            not sub["ok"] and "识别" in (sub.get("reason") or ""),
            f"识别已入账的纸，扫码报工被拦（{sub.get('reason')}）",
        )
        _ = res
    finally:
        db.close()

    print("\n" + "=" * 60)
    if failed:
        print(f"失败 {len(failed)} 项 / 共 {len(passed) + len(failed)} 项")
        for f in failed:
            print("  -", f)
        return 1
    print(f"全部通过（{len(passed)} 项）—— 两条通道互认成立，同一张纸算不了两次工时")
    print(f"副本已保留供检查：{DST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
