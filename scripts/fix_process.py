"""按字典重算存量明细的工序显示值 / 标准名 / 工序编码。

背景：早期版本把「包板折弯」这类字典别名归并成标准名「折弯」显示，核对时
和原图对不上。现在口径改成「字典里有的写法就照单据写」，code 与标准名单独存。
本脚本把已经落库的明细按同一口径刷一遍，改动逐条留痕。

    python scripts/fix_process.py --dry     # 只看会改什么
    python scripts/fix_process.py           # 真写
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import AuditLog, WorkItem  # noqa: E402
from app.services.dicts import dict_bundle, rematch_item  # noqa: E402

USER = "system:fix_process"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印，不写库")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        bundle = dict_bundle(db)
        changed = 0
        for it in db.query(WorkItem).order_by(WorkItem.sheet_id, WorkItem.seq).all():
            changes = {
                k: v
                for k, v in rematch_item(it, bundle).items()
                if k in ("process", "process_std", "process_code")
            }
            if not changes:
                continue
            changed += 1
            print(
                f"单据 {it.sheet_id} #{it.seq}: "
                + "  ".join(f"{k} {getattr(it, k)!r} → {v!r}" for k, v in changes.items())
            )
            if args.dry:
                continue
            for field, value in changes.items():
                db.add(
                    AuditLog(
                        entity="work_item",
                        entity_id=it.id,
                        sheet_id=it.sheet_id,
                        seq=it.seq,
                        field=f"item:{field}",
                        old_value=str(getattr(it, field) or ""),
                        new_value=str(value or ""),
                        user=USER,
                    )
                )
                setattr(it, field, value)
        if not args.dry and changed:
            db.commit()
        print(f"\n共 {changed} 条需要更新" + ("（干跑，未写库）" if args.dry else ""))
    finally:
        db.close()


if __name__ == "__main__":
    main()
