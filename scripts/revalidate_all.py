"""按当前规则重算全库明细告警（把历史落库的 flags 与现在的规则对齐）。

什么时候要跑：
- 新增/修改了校验规则（比如把「工序为空」从提示升级为阻断），
  历史单据里存的还是旧告警，台账会显示旧徽标，体检脚本也会误报漂移；
- 补录了维度（产品/件名/工序）或补了名册/路线，需要清掉对应告警。

只重算 flags_json / total_hours / flag_count，不动单据状态、不改工时数量。
默认预演（只打印会变的行），加 --apply 才落库。

用法：
    python scripts/revalidate_all.py              # 预演
    python scripts/revalidate_all.py --apply      # 落库
    python scripts/revalidate_all.py --apply --quiet
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.api.sheets import _revalidate  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Sheet  # noqa: E402


def codes_of(item) -> list[str]:
    return [f.get("code") for f in json.loads(item.flags_json or "[]")]


def main() -> int:
    apply = "--apply" in sys.argv
    quiet = "--quiet" in sys.argv

    db = SessionLocal()
    changed_rows: list[tuple[int, int, list[str], list[str]]] = []
    sheets_touched: set[int] = set()

    try:
        sheets = list(db.scalars(select(Sheet).order_by(Sheet.id)))
        for sh in sheets:
            before = {it.id: codes_of(it) for it in sh.items}
            try:
                _revalidate(db, sh)
            except Exception as exc:  # 单张失败不影响其它
                print(f"  单据 {sh.id} 重算失败：{exc}")
                continue
            for it in sh.items:
                aft = codes_of(it)
                if before.get(it.id, []) != aft:
                    changed_rows.append((sh.id, it.seq, before.get(it.id, []), aft))
                    sheets_touched.add(sh.id)

        print(f"单据 {len(sheets)} 张，告警有变化的明细 {len(changed_rows)} 行，涉及 {len(sheets_touched)} 张单据")
        if not quiet:
            for sid, seq, old, new in changed_rows:
                add = [c for c in new if c not in old]
                rm = [c for c in old if c not in new]
                bits = []
                if add:
                    bits.append("新增 " + "、".join(add))
                if rm:
                    bits.append("清除 " + "、".join(rm))
                print(f"  {sid}:{seq}  {'；'.join(bits)}")

        if not apply:
            db.rollback()
            print("\n预演结束，未写库。加 --apply 落库。")
            return 0

        db.commit()
        print("\n已落库。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
