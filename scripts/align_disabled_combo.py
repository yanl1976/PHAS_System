"""把「历史工时行全停用、组合状态却没变」的残留对齐成已归档。

停用以前只翻 `standard_hour.active`，组合状态原地不动，所以那些点过停用的
组合在库里长这样：工时标记为不用了，状态还是「草稿」，看着像没停用。

判据只认一条：这条组合有历史工时行，且**每一行**都被停用，而组合状态不是
已归档。有一行还在用就不动 —— 那是「部分工时不用」，不是「整条组合下架」。

用法：
  python scripts/align_disabled_combo.py            # 预演（默认，不写库）
  python scripts/align_disabled_combo.py --apply    # 落库
  python scripts/align_disabled_combo.py --apply --include-released
      默认跳过挂着已发布路线的组合：归档会让工艺当场下架，
      这种要人看过再动（会单独列出来）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import ProcessRoute, StandardHour, WorkCombo  # noqa: E402
from app.services import combo_registry as CR  # noqa: E402


def _key(a: str, b: str) -> tuple[str, str]:
    return ((a or "").strip(), (b or "").strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写库（默认只预演）")
    ap.add_argument(
        "--include-released",
        action="store_true",
        help="连挂着已发布路线的也一起归档（默认跳过，人工确认）",
    )
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        hours: dict[tuple[str, str], list[StandardHour]] = {}
        for h in db.scalars(select(StandardHour)).all():
            hours.setdefault(_key(h.product, h.part), []).append(h)
        released = {
            _key(r.product, r.part)
            for r in db.scalars(
                select(ProcessRoute).where(
                    ProcessRoute.kind == "route", ProcessRoute.status == "released"
                )
            ).all()
        }

        combos = db.scalars(select(WorkCombo)).all()
        todo: list[WorkCombo] = []
        held: list[WorkCombo] = []
        for c in combos:
            if c.status == "archived":
                continue
            rows = hours.get(_key(c.product, c.part)) or []
            if not rows or any(h.active for h in rows):
                continue
            if _key(c.product, c.part) in released and not args.include_released:
                held.append(c)
                continue
            todo.append(c)

        print(f"组合总数 {len(combos)}，其中历史工时全停用却未归档：{len(todo) + len(held)} 条")
        if todo:
            print("\n将对齐为「已归档」：")
            for c in todo:
                n = len(hours.get(_key(c.product, c.part)) or [])
                print(f"  {c.product or '（空产品）'} × {c.part}  当前 {c.status}，停用工时 {n} 行")
        if held:
            print("\n挂着已发布路线，跳过（人工确认后可加 --include-released）：")
            for c in held:
                print(f"  {c.product or '（空产品）'} × {c.part}  当前 {c.status}")

        if not args.apply:
            print("\n预演结束。要落库加 --apply。")
            return 0
        for c in todo:
            CR.archive_combo(db, c, by="align", note="历史工时已停用，状态对齐为归档")
        db.commit()
        print(f"\n已归档 {len(todo)} 条。")
        counts: dict[str, int] = {}
        for c in db.scalars(select(WorkCombo)).all():
            counts[c.status] = counts.get(c.status, 0) + 1
        print("状态分布：" + " / ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
