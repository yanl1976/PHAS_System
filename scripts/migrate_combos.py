"""一次性迁移：给工时组合建身份、铺部件层级、把「批量标出来的假发布」降回草稿。

三件事，顺序不能乱：

1. **登记**。889 个 `产品 × 件名` 从「现算的并集」变成 `work_combo` 里的行，
   状态一律 `draft`。从此台账答得出「这条工艺谁确认过」。
2. **降级**。首期迁移脚本把 85 条路线一次性标成 `released`（`published_by='migration'`，
   时间集中在同一秒），于是台账上凭空出现 81 个「已发布组合」—— 没有工艺员签过字。
   这些降回草稿。`--keep-inflight` 可以保留那几条正被在制任务引用的，代价是
   它们继续以「假发布」的身份当基准。
3. **铺层级**。「整机装配/总装」当根，`xx组件/xx总成/xx总包` 当部件挂根下，
   其余零件先试着按核心词前缀挂到某个部件里，挂不上就留在根下。

默认**预演**，只打印不改库。确认后加 `--apply`。

    .venv/Scripts/python.exe scripts/migrate_combos.py
    .venv/Scripts/python.exe scripts/migrate_combos.py --apply
    .venv/Scripts/python.exe scripts/migrate_combos.py --apply --keep-inflight
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import ProcessRoute, TaskRoute, WorkCombo  # noqa: E402
from app.security import set_operator  # noqa: E402
from app.services import combo_registry as CR  # noqa: E402

# 这些 published_by 说明发布不是人点的，是脚本批量写进去的
FAKE_PUBLISHERS = {"migration", "migrate", "system", "script", ""}


def inflight_count(db) -> dict[tuple[str, str], int]:
    """每个组合被多少在制任务引用。降级前必须知道，否则是在拆别人的脚手架。"""
    out: dict[tuple[str, str], int] = defaultdict(int)
    for product, part in db.execute(
        select(TaskRoute.product, TaskRoute.part).where(TaskRoute.status == "active")
    ).all():
        out[((product or "").strip(), (part or "").strip())] += 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="给工时组合建身份、铺层级、把假发布降回草稿（默认预演）"
    )
    ap.add_argument("--apply", action="store_true", help="真的落库（不加只预演）")
    ap.add_argument(
        "--keep-inflight",
        action="store_true",
        help="保留被在制任务引用的那几条「已发布」，其余照降",
    )
    ap.add_argument("--actor", default="migrate", help="写入留痕的操作人")
    args = ap.parse_args()

    set_operator(args.actor, "script")
    init_db()
    db = SessionLocal()

    try:
        before = db.scalar(select(WorkCombo).limit(1))
        print(f"库内组合（迁移前）：{'已有数据' if before else '空'}")

        # ---- 1. 登记 ----
        synced = CR.sync_combos(db, actor=args.actor)
        pid_fixed = CR.backfill_product_ids(db)
        print(f"[1] 登记组合：新建 {synced['created']}，库内合计 {synced['total']}；补产品外键 {pid_fixed}")

        # ---- 2. 降级 ----
        refs = inflight_count(db)
        as_is = {(c.product, c.part): c for c in db.scalars(select(WorkCombo)).all()}
        candidates: list[ProcessRoute] = []
        for r in db.scalars(
            select(ProcessRoute).where(
                ProcessRoute.kind == "route", ProcessRoute.status == "released"
            )
        ).all():
            publisher = (r.published_by or "").strip().lower()
            if publisher not in FAKE_PUBLISHERS:
                continue  # 真人点过发布，不动
            key = ((r.product or "").strip(), (r.part or "").strip())
            n = refs.get(key, 0)
            if n and args.keep_inflight:
                print(f"    保留 #{r.id} {key[0]} / {key[1]}：{n} 个在制任务在用")
                # 保留的这条，组合状态跟着是已发布，否则「任务在用、组合是草稿」自相矛盾
                combo = as_is.get(key)
                if combo is not None:
                    combo.status = "released"
                    combo.route_id = r.id
                    combo.published_at = r.published_at
                    combo.published_by = r.published_by or args.actor
                    combo.note = "迁移保留：被在制任务引用"
                    db.add(combo)
                continue
            candidates.append(r)

        print(f"[2] 待降级路线：{len(candidates)} 条（published_by 属于 {sorted(FAKE_PUBLISHERS)}）")
        by_product: dict[str, int] = defaultdict(int)
        for r in candidates:
            by_product[r.product or "(空产品)"] += 1
            key = ((r.product or "").strip(), (r.part or "").strip())
            combo = as_is.get(key)
            ref_n = refs.get(key, 0)
            tag = f"（{ref_n} 个在制任务在用）" if ref_n else ""
            print(f"    - #{r.id} v{r.version} {key[0]} / {key[1]}{tag}")
            if not args.apply:
                continue
            r.status = "draft"
            r.updated_at = r.updated_at or None
            db.add(r)
            if combo is not None:
                combo.status = "draft"
                combo.route_id = None
                combo.demoted_from = "released"
                combo.published_by = ""
                combo.published_at = None
                combo.note = "迁移降级：原为脚本批量标记的已发布，未经工艺员确认"
                db.add(combo)
        if by_product:
            print("    按产品：" + "、".join(f"{k} {v}条" for k, v in sorted(by_product.items())))

        if args.apply:
            db.flush()

        # ---- 3. 铺层级 ----
        layouts = CR.layout_all(db)
        print(f"[3] 铺层级：{len(layouts)} 个产品")
        lvl = Counter()
        for row in layouts:
            if row["components"] or row["leaves"]:
                print(
                    f"    {row['product'] or '(空产品)'}：根={row['root'] or '无'}，"
                    f"部件 {row['components']}，直接挂根 {row['leaves']}"
                    + (f"，跳过人工 {row['skipped']}" if row["skipped"] else "")
                )
            lvl["components"] += row["components"]
            lvl["leaves"] += row["leaves"]
        print(f"    合计：部件 {lvl['components']}，零件 {lvl['leaves']}")

        # ---- 汇总 ----
        if args.apply:
            db.commit()
        counts = Counter(
            c.status for c in db.scalars(select(WorkCombo)).all()
        )
        print(
            "\n结果："
            + f"草稿 {counts.get('draft', 0)}，已发布 {counts.get('released', 0)}，"
            f"归档 {counts.get('archived', 0)}"
        )
        if not args.apply:
            print("\n以上为预演，未写入。确认无误后加 --apply 落库。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
