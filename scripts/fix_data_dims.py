"""按原图补录归集维度（只补「原文上写得明明白白」的，不猜）。

背景：早期几张小单识别时表头没取到产品/件名，被确认进了账，于是汇总里多出
「未填」列。补录不猜：只把原图上明确写着、且能对上字典的值补进去，其余留给
人工在核对页「补录维度」处理（接口 PATCH /api/sheets/{id}/dims）。

本次处理：
- 单据 #4：表头识别漏了产品，但原图「产品代号 YUJ-18BZ / 产品名称 YUJ-18BZ制丸机」
  清楚可读，且同订单其余单据（#1/#2/#3）都记的是 YUJ-18BZ。补齐后按
  「同产品 + 同批次 + 同期间」规则把它并回主订单任务，删掉自动建的空壳任务。

用法：
    .venv/Scripts/python.exe scripts/fix_data_dims.py           # 预演，不写库
    .venv/Scripts/python.exe scripts/fix_data_dims.py --apply   # 落库
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import AuditLog, Period, Sheet, TaskRoute, WorkTask  # noqa: E402
from app.security import set_operator  # noqa: E402
from app.services import tasks as task_service  # noqa: E402

SHEET_ID = 4
PRODUCT = "YUJ-18BZ"


def main() -> int:
    apply = "--apply" in sys.argv
    # 留痕写「数据修复」而不是 operator：事后追这条维度是谁补的，得找得到人（角色）
    set_operator("数据修复", "script")
    db = SessionLocal()
    try:
        sheet = db.get(Sheet, SHEET_ID)
        if sheet is None:
            print(f"单据 #{SHEET_ID} 不存在，可能已经处理过")
            return 0
        header = json.loads(sheet.header_json or "{}")
        item = sheet.items[0] if sheet.items else None
        print(f"单据 #{sheet.id} 状态 {sheet.status}，明细 {len(sheet.items)} 行，工时 {sheet.total_hours}")
        print(f"  表头产品  : {header.get('product_name') or '（空）'} / 代号 {header.get('product_code') or '（空）'}")
        print(f"  明细产品  : {getattr(item, 'product_name', '') or '（空）'}")
        period = db.get(Period, sheet.period_id)
        print(f"  核算期    : {period.ym if period else '?'}（{period.status if period else '?'}）")

        if period and period.status == "locked":
            print("核算期已锁定，不动已出的账")
            return 1

        changed = []
        if (item.product_name or "") != PRODUCT:
            changed.append(f"明细产品 → {PRODUCT}")
        if (header.get("product_code") or "") != PRODUCT:
            changed.append(f"表头产品代号 → {PRODUCT}")
        if (header.get("product_name") or "") != PRODUCT:
            changed.append(f"表头产品名称 → {PRODUCT}")

        # 归集目标：同产品 + 同批次 + 同期间的任务
        batch = str(header.get("batch_no") or "")
        target = None
        for t in db.scalars(select(WorkTask).where(WorkTask.period_id == sheet.period_id)).all():
            if t.id == sheet.task_id:
                continue
            if (t.product or "") != PRODUCT:
                continue
            for s in db.scalars(select(Sheet).where(Sheet.task_id == t.id)).all():
                if str(json.loads(s.header_json or "{}").get("batch_no") or "") == batch:
                    target = t
                    break
            if target:
                break
        if target:
            changed.append(f"归属任务 #{sheet.task_id} → #{target.id} {target.task_no}")
        else:
            print("  未找到同产品同批次的任务，仅补维度、不改归属")

        if not changed:
            print("无需处理")
            return 0
        print("将执行：")
        for c in changed:
            print("  -", c)
        if not apply:
            print("\n（预演模式，未写库；加 --apply 落库）")
            return 0

        if item is not None and (item.product_name or "") != PRODUCT:
            db.add(
                AuditLog(
                    entity="work_item",
                    entity_id=item.id,
                    sheet_id=sheet.id,
                    seq=item.seq,
                    field="item:product_name",
                    old_value=item.product_name or "",
                    new_value=PRODUCT,
                    user="数据修复",
                )
            )
            item.product_name = PRODUCT
            item.row_state = "edited"
        if (header.get("product_code") or "") != PRODUCT or (header.get("product_name") or "") != PRODUCT:
            db.add(
                AuditLog(
                    entity="sheet",
                    entity_id=sheet.id,
                    sheet_id=sheet.id,
                    field="header",
                    old_value=json.dumps(
                        {"product_code": header.get("product_code", ""), "product_name": header.get("product_name", "")},
                        ensure_ascii=False,
                    ),
                    new_value=json.dumps({"product_code": PRODUCT, "product_name": PRODUCT}, ensure_ascii=False),
                    user="数据修复",
                )
            )
            header["product_code"] = PRODUCT
            header["product_name"] = PRODUCT
            sheet.header_json = json.dumps(header, ensure_ascii=False)
        db.flush()

        if target is not None:
            out = task_service.move_sheet_task(db, sheet, target.id, user="数据修复")
            print("  改挂结果：", {k: out.get(k) for k in ("unchanged", "sheet_id")}, out.get("checks"))

        db.commit()

        # 空壳任务（自动建出来、又没有任何单据）一并清掉，否则任务列表里
        # 会长期挂着一条没有产品、没有单据的「S4-...」记录。
        for t in db.scalars(select(WorkTask)).all():
            left = db.scalars(select(Sheet).where(Sheet.task_id == t.id)).all()
            if left or (target is not None and t.id == target.id):
                continue
            if not (t.task_no or "").startswith(("S4-", "S5-")):
                continue
            print(f"  清理空任务 #{t.id} {t.task_no}（归属单据 {len(left)} 张）")
            db.query(TaskRoute).filter(TaskRoute.task_id == t.id).delete(synchronize_session=False)
            db.delete(t)
        db.commit()
        print("已落库")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
