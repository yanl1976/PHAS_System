"""重算任务上的物化字段（单据数、实报工时、主产品），并报告漂移。

为什么需要独立工具：任务的 `sheet_count` / `actual_hours` 是**派生值**，
正常路径（确认、改挂、驳回、重开）都会重算。但只要有一次绕过服务层
（脚本直改库、批量修数、异常中断），派生值就会和事实脱节，而且没有任何
页面会报错 —— 任务列表照常显示一个错的实报工时。

默认只报不改，加 `--fix` 才写库。用法：
    .venv/Scripts/python.exe scripts/recalc_tasks.py          # 体检
    .venv/Scripts/python.exe scripts/recalc_tasks.py --fix    # 修正
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import WorkTask  # noqa: E402
from app.services.tasks import refresh_task  # noqa: E402


def main() -> int:
    fix = "--fix" in sys.argv
    db = SessionLocal()
    drift = 0
    try:
        for task in db.scalars(select(WorkTask).order_by(WorkTask.id)).all():
            before = (
                task.sheet_count,
                round(float(task.actual_hours or 0), 4),
                task.pending_count or 0,
                round(float(task.pending_hours or 0), 4),
                task.product,
            )
            refresh_task(db, task)
            after = (
                task.sheet_count,
                round(float(task.actual_hours or 0), 4),
                task.pending_count or 0,
                round(float(task.pending_hours or 0), 4),
                task.product,
            )
            if before != after:
                drift += 1
                print(
                    f"  漂移 {task.task_no}：已确认单据 {before[0]} → {after[0]}，"
                    f"实报 {before[1]} → {after[1]} h，"
                    f"待核 {before[2]} 张/{before[3]}h → {after[2]} 张/{after[3]}h"
                    + ("" if before[4] == after[4] else f"，产品 {before[4]!r} → {after[4]!r}")
                )
        if fix:
            db.commit()
            print(f"\n已修正 {drift} 个任务的派生字段" if drift else "\n无漂移，无需修正")
        else:
            db.rollback()
            print(f"\n发现 {drift} 个任务派生字段与实际不符（--fix 才会写库）" if drift else "\n全部一致")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
