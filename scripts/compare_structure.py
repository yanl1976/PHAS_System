"""导出 / 比对「产品结构」快照。

结构从 `work_combo` 抽到 `product_node` 时，最怕的是搬完之后有人少挂了一层、
层级错一位、每台用量掉成 1 —— 而这些在页面上要一页页翻才看得出来。
所以先把 889 条结构压成一份 JSON，搬完再导一份逐条比。

用法：
    python scripts/compare_structure.py dump  logs/structure_baseline.json
    python scripts/compare_structure.py dump  logs/structure_after.json
    python scripts/compare_structure.py diff  logs/structure_baseline.json logs/structure_after.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402

# 结构真源在 product_node；迁移期间表可能还不存在，所以两个来源都试
def _rows() -> list[dict]:
    from app.models import ProductNode, WorkCombo

    init_db()
    db = SessionLocal()
    try:
        out: list[dict] = []
        try:
            nodes = db.scalars(select(ProductNode)).all()
        except Exception:
            nodes = []
        if nodes:
            by_id = {n.id: n for n in nodes}
            for n in nodes:
                parent = by_id.get(n.parent_id) if n.parent_id else None
                out.append(
                    {
                        "product": n.product or "",
                        "part": n.part or "",
                        "parent_part": (parent.part or "") if parent else "",
                        "level": int(n.level or 0),
                        "seq": int(n.seq or 0),
                        "qty_per_unit": round(float(n.qty_per_unit or 1), 4),
                    }
                )
        else:
            combos = db.scalars(select(WorkCombo)).all()
            by_id = {c.id: c for c in combos}
            for c in combos:
                parent = by_id.get(c.parent_id) if c.parent_id else None
                out.append(
                    {
                        "product": c.product or "",
                        "part": c.part or "",
                        "parent_part": (parent.part or "") if parent else "",
                        "level": int(c.level or 0),
                        "seq": int(c.seq or 0),
                        "qty_per_unit": round(float(c.qty_per_unit or 1), 4),
                    }
                )
        out.sort(key=lambda x: (x["product"], x["part"]))
        return out
    finally:
        db.close()


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dump"
    if cmd == "dump":
        dest = Path(sys.argv[2]) if len(sys.argv) > 2 else BASE / "logs" / "structure_snapshot.json"
        rows = _rows()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已导出 {len(rows)} 条结构 → {dest}")
        return 0

    if cmd == "diff":
        a = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        b = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
        key = lambda x: (x["product"], x["part"])
        ma = {key(x): x for x in a}
        mb = {key(x): x for x in b}
        only_a = [k for k in ma if k not in mb]
        only_b = [k for k in mb if k not in ma]
        diff = []
        for k in ma:
            if k in mb and ma[k] != mb[k]:
                diff.append((k, ma[k], mb[k]))
        print(f"基线 {len(a)} 条，新快照 {len(b)} 条")
        print(f"只在基线里：{len(only_a)}；只在新的里：{len(only_b)}；字段有差：{len(diff)}")
        for k in only_a[:10]:
            print(f"  缺失 {k}")
        for k in only_b[:10]:
            print(f"  多出 {k}")
        for k, x, y in diff[:15]:
            fields = {f: (x[f], y[f]) for f in x if x[f] != y.get(f)}
            print(f"  差异 {k}: {fields}")
        return 0 if not (only_a or only_b or diff) else 1

    print(f"不认识的命令：{cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
