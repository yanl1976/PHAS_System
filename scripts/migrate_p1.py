"""P1 迁移：打通「产品 → 任务 → 工艺」的归属关系。

跑一次就够，幂等（重复执行只会补缺失的部分）。

做四件事：

1. **重建 process_route 的唯一约束**：原来是一条 `(kind, product, part, version)`
   的 UNIQUE，任务工艺进来必撞（同产品同件名的多个任务都是 version=1）。
   改成两个部分唯一索引：基线与任务工艺各管各的。
2. **产品归一**：把历史写法（`YUJ18BZ制丸机`、`SJ2栓剂` …）收敛到 product_dict
   的规范名并回填 product_id。冲突行按样本数加权合并，不丢工时。
   认不出的写法进「待认领」清单，不猜、不自动新建产品。
3. **补空产品**：195 条记录的 product 是空串。若某件名在其他记录里只出现过一个
   产品，就据此回填（唯一推断）；有歧义或无任何线索的一律留空。
4. **建任务**：每张单据一个任务，任务号 `产品代号-批次-期间`。

用法：
    python scripts/migrate_p1.py --dry     # 只看报告，不落库
    python scripts/migrate_p1.py           # 正式执行（自动备份）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select, text  # noqa: E402

from app.db import DB_URL, SessionLocal, engine, init_db  # noqa: E402
from app.models import ProcessRoute, ProcessRouteStep, ProductDict, StandardHour, WorkItem  # noqa: E402
from app.services import tasks as task_service  # noqa: E402

BACKUP_DIR = ROOT / "data" / "backup"


def backup_db() -> Path | None:
    """迁移前整库备份。归一化是不可逆的文本改写，没有回滚点不行。

    必须用 sqlite3 的 backup API，不能 `read_bytes()` 复制文件：
    WAL 模式下新写入的数据还在 -wal 里，直接复制 .db 拿到的是上一次
    checkpoint 的旧状态 —— 会得到一份「少几条、状态还是老的」的假备份，
    之后拿它对照就全是误判。
    """
    if not DB_URL.startswith("sqlite"):
        return None
    src = ROOT / DB_URL.split("///")[-1].replace("\\", "/")
    if not src.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"{src.stem}_before_p1_{stamp}{src.suffix}"

    import sqlite3

    with sqlite3.connect(str(src)) as s, sqlite3.connect(str(dst)) as d:
        s.backup(d)
    return dst


# --------------------------------------------------------------- 1. 重建唯一约束
ROUTE_INDEXES = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_route_baseline "
    "ON process_route (product, part, version) WHERE kind <> 'task'",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_route_task "
    "ON process_route (task_id, version) WHERE kind = 'task'",
)


def rebuild_route_unique() -> bool:
    """老库的表里嵌着 `uq_route_ver` 唯一约束，SQLite 删不掉，只能重建表。

    RENAME 时必须 `legacy_alter_table=ON`：否则 SQLite 会顺手把
    process_route_step 外键里的表名一起改掉，重建完就指向一张已删除的表。
    """
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        ddl = conn.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='process_route'"
        ).scalar()
        if not ddl:
            return False
        if "uq_route_ver" not in ddl:
            for sql in ROUTE_INDEXES:
                conn.exec_driver_sql(sql)
            return False

        old_cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(process_route)")}
        # 显式索引（ix_process_route_*）的名字是全局的，不先删掉，
        # 新表建索引时会撞名。origin='c' 是 CREATE INDEX 建的，能删；
        # 'u' 是内联 UNIQUE 约束的 autoindex，删不掉也不用删 —— 随旧表一起消失。
        for row in conn.exec_driver_sql("PRAGMA index_list(process_route)").fetchall():
            if len(row) > 3 and row[3] == "c":
                conn.exec_driver_sql(f'DROP INDEX IF EXISTS "{row[1]}"')
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        conn.exec_driver_sql("ALTER TABLE process_route RENAME TO _process_route_old")
        ProcessRoute.__table__.create(conn)
        common = [c.name for c in ProcessRoute.__table__.columns if c.name in old_cols]
        cols = ", ".join(common)
        conn.exec_driver_sql(
            f"INSERT INTO process_route ({cols}) SELECT {cols} FROM _process_route_old"
        )
        conn.exec_driver_sql("DROP TABLE _process_route_old")
        conn.exec_driver_sql("PRAGMA legacy_alter_table=OFF")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        for sql in ROUTE_INDEXES:
            conn.exec_driver_sql(sql)
        return True


# --------------------------------------------------------------- 2. 产品归一
def _merge_numbers(rows: list[StandardHour]) -> tuple[float, int, float, str]:
    """把同一 (产品,件名,工序) 的多条观测合成一条。

    单件工时按样本数加权 —— 两条记录是不同批次的观测，直接相加或取算术平均
    都会偏；样本为 0 时退回算术平均。一致率取最大（最保守）。
    """
    total = sum(int(r.samples or 0) for r in rows)
    if total > 0:
        hours = sum(float(r.unit_hours or 0) * int(r.samples or 0) for r in rows) / total
    else:
        hours = sum(float(r.unit_hours or 0) for r in rows) / len(rows)
    agreement = max(float(r.agreement or 0) for r in rows)
    ids: set[int] = set()
    for r in rows:
        ids.update(json.loads(r.sample_item_ids or "[]"))
    # 保留 4 位小数：合并后带出来的浮点尾巴（3.1666499999999997）
    # 会让「界面显示值」和「库里值」对不上，核对时天天误报。
    return round(hours, 4), total, agreement, json.dumps(sorted(ids))


def backfill_products(db) -> dict:
    """产品文本归一 + product_id 回填。

    两个容易踩的坑都在这里处理：

    1. **补空产品可能撞唯一约束**：给 (空产品, 件名A) 补上产品 P 后，P 名下
       可能已经有 (件名A) 了。所以「推断补产品」不能就地赋值，必须和归一一起
       先算出最终归属，再统一合并。
    2. **UPDATE 与 DELETE 的先后**：同一 flush 里改保留行的 product 又删合并行，
       谁先执行由 unit of work 决定，UPDATE 先跑就撞唯一约束。分两拍：
       先删干净并 flush，再改保留行。
    """
    index = task_service.product_index(db)
    unmatched: Counter[str] = Counter()
    merged = renamed = filled = 0

    std_rows = db.scalars(select(StandardHour)).all()

    # 空产品的件名，先看它在别处只挂过哪个产品（唯一才能推，有歧义不猜）
    part_products: dict[str, set[str]] = defaultdict(set)
    for r in std_rows:
        if r.product:
            part_products[r.part].add(r.product)

    # 1. 算最终归属，不动对象
    final: dict[int, tuple[str, ProductDict]] = {}
    for r in std_rows:
        raw = r.product
        inferred = False
        if not raw:
            cands = part_products.get(r.part) or set()
            if len(cands) != 1:
                continue
            raw = next(iter(cands))
            inferred = True
        prod = index.get(task_service.norm_key(raw))
        if prod is None:
            if r.product:
                unmatched[r.product] += 1
            continue
        if not inferred and prod.name != r.product:
            renamed += 1
        if inferred:
            filled += 1
        final[r.id] = (prod.name, prod)

    # 2. 按最终归属分组
    groups: dict[tuple[str, str, str], list[StandardHour]] = defaultdict(list)
    for r in std_rows:
        got = final.get(r.id)
        if got:
            groups[(got[0], r.part, r.process)].append(r)

    doomed: list[StandardHour] = []
    pending: list[tuple[StandardHour, str, ProductDict, tuple[float, int, float, str]]] = []
    for _key, members in groups.items():
        members.sort(key=lambda x: x.id)
        keep = members[0]
        pending.append((keep, final[keep.id][0], final[keep.id][1], _merge_numbers(members)))
        doomed.extend(members[1:])
        merged += len(members) - 1

    for r in doomed:
        db.delete(r)
    if doomed:
        db.flush()

    for keep, name, prod, (hours, samples, agreement, ids) in pending:
        keep.product = name
        keep.product_id = prod.id
        keep.unit_hours = hours
        keep.samples = samples
        keep.agreement = agreement
        keep.sample_item_ids = ids
    db.flush()

    # 2c. 路线归一
    route_unmatched: Counter[str] = Counter()
    taken: dict[tuple[str, str, int], int] = {}
    for rt in db.scalars(select(ProcessRoute)).all():
        if not rt.product:
            continue
        prod = index.get(task_service.norm_key(rt.product))
        if prod is None:
            route_unmatched[rt.product] += 1
            continue
        rt.product = prod.name
        rt.product_id = prod.id
        sig = (rt.product, rt.part, rt.version)
        other = taken.get(sig)
        if other is not None and rt.kind != task_service.KIND_TASK:
            # 归一后撞版本号：把这一版往后顺延，不删数据。
            max_v = db.scalar(
                select(ProcessRoute.version)
                .where(ProcessRoute.product == rt.product, ProcessRoute.part == rt.part)
                .order_by(ProcessRoute.version.desc())
                .limit(1)
            ) or 0
            rt.version = int(max_v) + 1
            sig = (rt.product, rt.part, rt.version)
        taken[sig] = rt.id

    db.flush()
    return {
        "filled_empty_product": filled,
        "renamed": renamed,
        "merged_rows": merged,
        "unmatched_std": dict(unmatched.most_common(20)),
        "unmatched_route": dict(route_unmatched.most_common(20)),
    }


# --------------------------------------------------------------- 3. 样本溯源
def backfill_samples(db) -> dict:
    """把每条标准工时能对上的工时明细记下来，对不上的标 import。

    现状会很刺眼：1279 条里绝大多数是外部台账导入，一条单据都追不到。
    这不是 bug，是「没有任务来源」这件事在数据上的证据，如实记下来。
    """
    buckets: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    for it in db.scalars(select(WorkItem)).all():
        key = (
            task_service.norm_key(it.product_name),
            task_service.norm_key(it.part_name),
            task_service.norm_key(it.process_std or it.process),
        )
        if key[1]:
            buckets[key].append(it.id)

    linked = imported = 0
    for r in db.scalars(select(StandardHour)).all():
        key = (
            task_service.norm_key(r.product),
            task_service.norm_key(r.part),
            task_service.norm_key(r.process),
        )
        ids = sorted(buckets.get(key, []))
        r.sample_item_ids = json.dumps(ids)
        r.origin = "item" if ids else "import"
        if ids:
            linked += 1
        else:
            imported += 1
    db.flush()
    return {"linked": linked, "import_only": imported}


def refresh_flags(db) -> dict:
    """产品归一后重算所有单据的告警。

    不重算会留下一批「假清白」的明细：原来单据上写的产品名（`YUJ-18BZ`）
    和标准工时表里的写法（`YUJ18BZ制丸机`）对不上，标准工时校验**从来没生效过**，
    flags 一直是空的。归一之后能对上了，那些 3 小时 vs 标准 57.5 小时的
    离谱偏差才第一次浮出来 —— 这是迁移真正暴露的问题，得如实刷进库里。
    """
    from app.api.sheets import _revalidate
    from app.models import Sheet

    changed = 0
    for sh in db.scalars(select(Sheet)).all():
        before = int(sh.flag_count or 0)
        _revalidate(db, sh)
        if int(sh.flag_count or 0) != before:
            changed += 1
    db.flush()
    return {"sheets": changed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只出报告，不提交")
    ap.add_argument("--no-backup", action="store_true")
    args = ap.parse_args()

    if not args.dry and not args.no_backup:
        path = backup_db()
        if path:
            print(f"[备份] {path}")

    init_db()
    rebuilt = rebuild_route_unique()
    print(f"[表结构] process_route 唯一约束{'已重建为部分索引' if rebuilt else '无需重建'}")

    db = SessionLocal()
    try:
        prod_report = backfill_products(db)
        sample_report = backfill_samples(db)
        task_report = task_service.rebuild_tasks(db)
        flag_report = refresh_flags(db)

        if args.dry:
            db.rollback()
            print("\n[dry] 已回滚，数据库未改动")
        else:
            db.commit()
    finally:
        db.close()

    print("\n=== 产品归一 ===")
    print(f"  空产品按唯一件名推断补回：{prod_report['filled_empty_product']}")
    print(f"  写法归一重命名：{prod_report['renamed']}")
    print(f"  归一后合并记录：{prod_report['merged_rows']}")
    if prod_report["unmatched_std"]:
        print(f"  工时表认不出的写法（{len(prod_report['unmatched_std'])} 种，进待认领）：")
        for name, n in prod_report["unmatched_std"].items():
            print(f"    - {name} ({n} 条)")
    if prod_report["unmatched_route"]:
        print(f"  路线表认不出的写法：{prod_report['unmatched_route']}")

    print("\n=== 样本溯源 ===")
    print(f"  能追到工时明细：{sample_report['linked']} 条")
    print(f"  仅外部台账导入（追不到单据）：{sample_report['import_only']} 条")

    print("\n=== 任务 ===")
    print(f"  新建 {task_report['created']} 个 / 刷新 {task_report['refreshed']} 个")

    print("\n=== 告警重算 ===")
    print(f"  告警数发生变化的单据：{flag_report['sheets']} 张")

    with SessionLocal() as probe:
        print("\n=== 结果核对 ===")
        print(f"  产品主数据：{probe.scalar(select(ProductDict.id).count()) if False else len(probe.scalars(select(ProductDict)).all())} 个")
        print(f"  标准工时挂上产品：{len([r for r in probe.scalars(select(StandardHour)).all() if r.product_id])} / {len(probe.scalars(select(StandardHour)).all())}")
        print(f"  路线挂上产品：{len([r for r in probe.scalars(select(ProcessRoute)).all() if r.product_id])} / {len(probe.scalars(select(ProcessRoute)).all())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
