"""教会系统一条对应关系：这段签名原文 = 这个人。

检验员/操作者两列是手写签名，模型认不准是常态。界面上人工改正时会自动记别名
（见 app/services/learn.py），但遇到「历史单据已经识别错了、又不想逐张点开改」的情况，
用这个脚本一条命令补上，并把受影响的单据重新归一一遍。

用法：
  python scripts/learn_alias.py 杜文杰 定生              # 只记别名（名册里能看到）
  python scripts/learn_alias.py 杜文杰 定生 --sheet 7    # 记完把单据 7 重新归一
  python scripts/learn_alias.py 杜文杰 定生 --all        # 记完把库里所有单据重新归一
  python scripts/learn_alias.py 杜文杰 定生 --dry        # 只看会改什么，不落库
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Sheet, Worker  # noqa: E402
from app.services import learn  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="把签名原文记成人员别名，并重新归一单据")
    ap.add_argument("name", help="人员的规范名（须在名册里）")
    ap.add_argument("raw", help="签名格的识别原文，如「定生」")
    ap.add_argument("--sheet", type=int, default=0, help="重新归一这张单据")
    ap.add_argument("--all", action="store_true", help="重新归一库里所有单据")
    ap.add_argument("--dry", action="store_true", help="只看不写")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        worker = db.scalar(select(Worker).where(Worker.name == args.name))
        if not worker:
            print(f"✗ 名册里没有「{args.name}」，先在「系统管理 → 人员」建人")
            return 1
        before = worker.aliases or ""
        if args.dry:
            print(f"[dry] 将把「{args.raw}」记成「{args.name}」的别名（当前别名：{before or '空'}）")
            return 0
        got = learn.remember(db, args.raw, args.name)
        if got:
            db.commit()
            print(f"✓ 「{args.name}」别名：{before or '空'} → {worker.aliases}")
        else:
            print(f"· 未写入：要么已存在，要么「{args.raw}」已被别人占用（当前别名：{before or '空'}）")

        if not (args.sheet or args.all):
            return 0

        from app.api.sheets import _revalidate
        from app.services.dicts import dict_bundle, rematch_item

        stmt = select(Sheet)
        if args.sheet:
            stmt = stmt.where(Sheet.id == args.sheet)
        sheets = db.scalars(stmt).all()
        bundle = dict_bundle(db)
        touched = 0
        for sheet in sheets:
            changed = False
            for it in sheet.items:
                # 老单据没存签名原文：当前值就是原文（重新识别之前的识别结果）
                if not (it.inspector_raw or ""):
                    it.inspector_raw = it.inspector or ""
                old = (it.inspector, it.worker_id, it.process_code, it.product_name, it.material)
                for field, value in rematch_item(it, bundle).items():
                    if hasattr(it, field) and field != "worker_score":
                        setattr(it, field, value)
                if (it.inspector, it.worker_id, it.process_code, it.product_name, it.material) != old:
                    changed = True
            if changed:
                _revalidate(db, sheet)
                touched += 1
                print(f"· 单据 {sheet.id} 已重新归一，提示 {sheet.flag_count} 条")
        db.commit()
        print(f"完成：重算 {len(sheets)} 张，其中 {touched} 张有变化")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
