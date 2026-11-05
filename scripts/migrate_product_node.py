"""把 `work_combo` 上的层级字段搬到 `product_node`。

用法：
    python scripts/migrate_product_node.py            # 真的写库
    python scripts/migrate_product_node.py --dry-run   # 只看要搬多少

为什么搬：结构（谁装在谁下面、每台几件、先后）是装配事实，跟「这个件有没有工艺」
无关。字段寄居在工时组合行上，等于「没编工艺的件在结构里不存在」，
而且一个件只能有一个上级。详见 `ProductNode` 的说明。

搬法（两轮，避免顺序依赖）：

1. 第一轮：每个 `work_combo` 建一条 `product_node`，parent 暂时空。
2. 第二轮：按 `work_combo.parent_id → product_node.id` 的映射回填 parent。

`level / seq / qty_per_unit / level_src`（→ `source`）原样抄，一个都不改 ——
件号是按 `parent_id + seq` 算出来的，抄错一个字就是印错一批单子。

幂等：已有的 `(product, part)` 直接跳过，只补缺的。可以反复跑。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import ProductNode, WorkCombo  # noqa: E402


def main() -> int:
    dry = "--dry-run" in sys.argv
    init_db()
    db = SessionLocal()
    try:
        combos = db.scalars(select(WorkCombo)).all()
        existing = {
            (n.product or "", n.part or ""): n
            for n in db.scalars(select(ProductNode)).all()
        }
        print(f"工时组合 {len(combos)} 条，已有结构节点 {len(existing)} 条")

        combo_to_node: dict[int, int] = {}
        created = 0

        # ---- 第一轮：建节点（parent 先空）
        for c in combos:
            key = (c.product or "", c.part or "")
            node = existing.get(key)
            if node is None:
                node = ProductNode(
                    product=c.product or "",
                    part=c.part or "",
                    parent_id=None,
                    level=int(c.level or 0),
                    qty_per_unit=float(c.qty_per_unit or 1),
                    seq=int(c.seq if c.seq is not None else 100),
                    source=(c.level_src or "auto"),
                )
                db.add(node)
                db.flush()  # 拿 id
                existing[key] = node
                created += 1
            combo_to_node[c.id] = node.id

        # ---- 第二轮：回填 parent（只在同一产品内认）
        fixed = 0
        for c in combos:
            if not c.parent_id:
                continue
            pid = combo_to_node.get(c.parent_id)
            if not pid:
                continue
            node = db.get(ProductNode, combo_to_node[c.id])
            parent = db.get(ProductNode, pid)
            if node is None or parent is None:
                continue
            # 跨产品指向是脏数据，宁可当顶层也不要造出一棵混着两个产品的树
            if (parent.product or "") != (node.product or ""):
                continue
            if node.parent_id != pid:
                node.parent_id = pid
                fixed += 1

        if dry:
            db.rollback()
            print(f"[dry-run] 将新建 {created} 条、修 parent {fixed} 条（已回滚）")
        else:
            db.commit()
            print(f"新建 {created} 条、修 parent {fixed} 条 → product_node 共 {len(existing)} 条")

        # 自检：顶层（无 parent）的节点应该正好是每个产品的总装，不该有散件掉在根上
        rows = db.scalars(select(ProductNode)).all()
        roots: dict[str, int] = {}
        for n in rows:
            if not n.parent_id:
                roots[n.product or ""] = roots.get(n.product or "", 0) + 1
        multi = {k: v for k, v in roots.items() if v > 1}
        print(f"产品 {len(roots)} 个；根多于 1 个的产品：{len(multi)} 个")
        for k, v in list(multi.items())[:5]:
            parts = [n.part for n in rows if (n.product or "") == k and not n.parent_id]
            print(f"  {k}: {v} 个根 → {parts[:5]}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
