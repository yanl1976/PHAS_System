"""把当前所有产品的工艺卡件号导出来当基线。

用法：
    python scripts/dump_card_codes.py logs/card_codes_baseline.json

为什么要有这个东西：件号（01-02）是要签字、要装订、要跟车间对单的。
把「结构」从 `work_combo` 抽到独立表，本质上是换了一次数据来源 ——
换完之后每个产品的每一张卡，件号、父件号、层级、每台用量、含子件工时
都必须和换之前一个字不差，否则就是把真实单据改坏了。

所以先存一份，改完再导一份，用 `scripts/compare_card_codes.py` 比。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.db import SessionLocal, init_db  # noqa: E402
from app.services import process_card  # noqa: E402


def dump() -> dict:
    init_db()
    db = SessionLocal()
    out: dict[str, list] = {}
    try:
        for p in process_card.products(db):
            name = p.get("product") or ""
            data = process_card.build_cards(db, product=name, limit=2000)
            out[name] = [
                {
                    "part": c["part"],
                    "code": c["code"],
                    "parent_code": c["parent_code"],
                    "parent_part": c["parent_part"],
                    "level": c["level"],
                    "qty_per_unit": c["qty_per_unit"],
                    "own_hours": round(c["own_hours"], 4),
                    "subtotal_hours": round(c["subtotal_hours"], 4),
                    "print_qty": c["print_qty"],
                }
                for c in data["cards"]
            ]
    finally:
        db.close()
    return out


def main() -> int:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "logs" / "card_codes_baseline.json"
    data = dump()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    total = sum(len(v) for v in data.values())
    print(f"已导出 {len(data)} 个产品 / {total} 张卡 → {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
