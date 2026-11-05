"""P3 迁移：把现有草稿基线发布为可用基准。

背景：规则收紧成「只有已发布的工艺路线才能被任务引用」之后，全库 85 条基线
全是草稿，等于一个可用基准都没有 —— 现有 6 条在制引用会全部变成违规。

处理口径（宁可少发也不多发）：
  1. 每个「产品×件名」只发布**版本号最大**的那条草稿，更早的草稿是历史残留，不动；
  2. 没有工序的草稿不发布（空路线当基准会让核对把所有工序判成「多做的」）；
  3. 该组合已有已发布版本的，不重复发布（那是升版中间态，要人来定）。

用法：
    python scripts/migrate_p3_publish.py            # 预演，只打印
    python scripts/migrate_p3_publish.py --apply    # 真正落库（先自动备份）
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from sqlalchemy import select  # noqa: E402

from app.config import DB_URL  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import ProcessRoute  # noqa: E402
from app.services import routes as R  # noqa: E402


def backup() -> Path | None:
    """用 SQLite backup API 备份。

    不能直接复制 .db 文件：WAL 模式下未 checkpoint 的改动还在 -wal 里，
    复制出来的是旧状态。上次就是因为这个误判「丢了一条路线」。
    """
    if not DB_URL.startswith("sqlite"):
        return None
    src_path = DB_URL.split("///")[-1]
    backup_dir = BASE / "data" / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dst = backup_dir / f"worktime_before_p3_{dt.datetime.now():%Y%m%d_%H%M%S}.db"
    src = sqlite3.connect(src_path)
    target = sqlite3.connect(dst)
    with target:
        src.backup(target)
    src.close()
    target.close()
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正落库（默认只预演）")
    ap.add_argument("--limit", type=int, default=0, help="最多发布多少条（0=不限）")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        drafts = db.scalars(
            select(ProcessRoute)
            .where(ProcessRoute.kind == "route", ProcessRoute.status == "draft")
            .order_by(ProcessRoute.product, ProcessRoute.part, ProcessRoute.version.desc())
        ).all()

        newest: dict[tuple[str, str], ProcessRoute] = {}
        for r in drafts:
            newest.setdefault((r.product, r.part), r)

        todo, skipped = [], []
        for (product, part), r in newest.items():
            if args.limit and len(todo) >= args.limit:
                break
            current = R.pick_baseline(db, product, part)
            if current is not None:
                skipped.append((r, f"已有已发布 v{current.version}"))
                continue
            if not r.steps:
                skipped.append((r, "没有工序"))
                continue
            todo.append(r)

        # 被在制任务引用的草稿：这些必须发布，否则违反了「只能引用已发布」
        ref_ok = {}
        for r in todo:
            ref_ok[r.id] = R.active_ref_count(db, r.id)

        print(f"待发布 {len(todo)} 条，跳过 {len(skipped)} 条")
        for r in todo:
            tag = f"（被 {ref_ok[r.id]} 个任务引用）" if ref_ok.get(r.id) else ""
            print(f"  + #{r.id:<4} {r.product} / {r.part}  v{r.version}  {len(r.steps)} 道{tag}")
        for r, why in skipped:
            print(f"  - #{r.id:<4} {r.product} / {r.part}  v{r.version}  跳过：{why}")

        if not args.apply:
            print("\n预演结束。确认无误后加 --apply 落库。")
            return 0

        dst = backup()
        if dst:
            print(f"\n已备份到 {dst}")

        published = 0
        for r in todo:
            R.publish(db, r, published_by="migration")
            published += 1
        db.commit()

        # 复查：还有没有「引用指向非已发布基线」
        bad = []
        from app.models import TaskRoute

        for tr in db.scalars(select(TaskRoute).where(TaskRoute.status == "active")).all():
            route = db.get(ProcessRoute, tr.route_id)
            if route is None:
                bad.append((tr.id, "路线已不存在"))
            elif tr.origin == "baseline" and (route.kind != "route" or route.status != "released"):
                bad.append((tr.id, f"{route.kind}/{route.status} v{route.version}"))

        print(f"\n已发布 {published} 条")
        print(f"剩余违规引用 {len(bad)} 条")
        for tr_id, why in bad:
            print(f"  ! task_route #{tr_id}: {why}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
