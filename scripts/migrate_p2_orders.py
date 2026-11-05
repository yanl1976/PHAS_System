"""P2 订单化迁移：一个订单 = 一个任务，工艺路线改为引用复用。

旧模型是「一张加工路线单 = 一个任务」，任务上带 product/part/batch_no/sheet_id，
每建一个任务就把基线复制成一份 kind='task' 的快照。结果：同一份标准工艺被复制 N 份，
「标准」这件事立刻失去意义；订单拆成几张单，就变成几个互不相干的「任务」。

新模型：

    work_task（订单）
      └─ task_route × N ──→ process_route（基线，多任务共用）
                            └─ 或用 kind='task' 的副本（fork 后才存在）

本脚本做四件事：

1. 把现有 7 个「单级任务」按（产品 + 批次 + 期间）合并成订单级任务；
2. 每个任务下建 task_route：指向基线（origin=baseline）；
   原 kind='task' 快照若与基线内容完全一致 → 删掉，改引用（不丢信息，只是去重）；
   不一致 → 保留为 origin=copy，从此只属于这个任务；
3. 单据反过来挂到任务上（sheet.task_id），一个订单可以有多张单；
4. 清掉 work_task 上已无意义的列（part / batch_no / sheet_id），重算聚合。

跑之前先整库备份（用 sqlite3 backup API，WAL 模式下直接复制文件会拿到旧 checkpoint）。

用法：
    python scripts/migrate_p2_orders.py --dry      # 只看报告
    python scripts/migrate_p2_orders.py            # 真跑
"""
from __future__ import annotations

import argparse
import datetime as dt
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import DB_URL  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import ProcessRoute, ProcessRouteStep, Sheet, TaskRoute, WorkTask  # noqa: E402
from app.services import routes as route_service  # noqa: E402
from app.services import tasks as task_service  # noqa: E402

BACKUP_DIR = ROOT / "data" / "backup"


def db_path() -> Path | None:
    if not DB_URL.startswith("sqlite"):
        return None
    raw = DB_URL.split("///")[-1].replace("\\", "/")
    p = Path(raw)
    return p if p.is_absolute() else (ROOT / p)


def backup() -> Path | None:
    src = db_path()
    if src is None or not src.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"{src.stem}_before_p2_{stamp}{src.suffix}"
    with sqlite3.connect(src) as s, sqlite3.connect(dst) as d:
        s.backup(d)
    return dst


def read_old_tasks() -> list[dict]:
    """直接读原始列。

    新模型已经删掉 part/batch_no/sheet_id，ORM 摸不到它们 —— 迁移恰恰要用，
    所以这里绕开 ORM 走 SQL，字段缺失时给默认值（同脚本可重复跑）。
    """
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    cols = {r[1] for r in conn.execute("PRAGMA table_info(work_task)")}
    want = ["id", "task_no", "product_id", "product", "part", "batch_no", "period_id",
            "sheet_id", "qty", "actual_hours", "status", "source"]
    select = ", ".join(c if c in cols else f"NULL AS {c}" for c in want)
    rows = [dict(r) for r in conn.execute(f"SELECT {select} FROM work_task ORDER BY id")]
    conn.close()
    return rows


def step_signature(db, route_id: int) -> list[tuple]:
    rows = db.query(ProcessRouteStep).filter(ProcessRouteStep.route_id == route_id).all()
    return [
        (
            s.seq,
            (s.process_code or "").strip(),
            (s.process_name or "").strip(),
            round(float(s.unit_hours or 0), 4),
            round(float(s.setup_hours or 0), 4),
        )
        for s in sorted(rows, key=lambda x: x.seq or 0)
    ]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只出报告，不写库")
    args = ap.parse_args()

    if not DB_URL.startswith("sqlite"):
        print("本脚本只处理 SQLite")
        return 1

    bkp = backup()
    print(f"备份：{bkp.name if bkp else '（跳过）'}")

    init_db()  # 建 task_route 表、补 sheet.task_id 与新列

    db = SessionLocal()
    try:
        if db.query(TaskRoute).count() and not args.dry:
            print("task_route 已有数据，说明迁移跑过了。要重跑请先手工清理。")
            return 1

        old = read_old_tasks()
        if not old:
            print("没有旧任务，无需迁移")
            return 0

        # ---- 1. 按（产品 + 批次 + 期间）分组：同一订单的多张单合成一个任务
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for t in old:
            if (t["product"] or "").strip():
                key = (t["product"], t["batch_no"] or "", t["period_id"])
            else:
                key = ("#unknown", t["id"])
            groups[key].append(t)

        plan = []
        for key, members in groups.items():
            keeper = members[0]
            pairs: list[tuple[str, str]] = []
            for m in members:
                pair = ((m["product"] or "").strip(), (m["part"] or "").strip())
                if pair[0] and pair not in pairs:
                    pairs.append(pair)
            plan.append({"keeper": keeper, "members": members, "pairs": pairs})

        print(f"\n=== 合并计划：{len(old)} 个任务 → {len(plan)} 个订单级任务 ===")
        for p in plan:
            k = p["keeper"]
            names = "、".join(m["task_no"] for m in p["members"])
            print(f"  → {k['task_no']}  [产品 {k['product'] or '—'} / 批次 {k['batch_no'] or '—'}]")
            print(f"     并入：{names}")
            print(f"     路线：{'、'.join(f'{a}/{b}' for a, b in p['pairs']) or '（无，待补）'}")

        # ---- 2. 规划每条路线挂什么
        dropped_copies: list[str] = []
        kept_copies: list[str] = []
        no_baseline: list[str] = []
        route_plan: list[dict] = []

        for p in plan:
            keeper = p["keeper"]
            member_ids = [m["id"] for m in p["members"]]
            seq = 0
            for product, part in p["pairs"]:
                seq += 1
                baseline = (
                    route_service.pick_baseline(db, product, part) if part else None
                )
                copies = [
                    r
                    for r in db.query(ProcessRoute)
                    .filter(
                        ProcessRoute.kind == "task",
                        ProcessRoute.task_id.in_(member_ids),
                        ProcessRoute.product == product,
                        ProcessRoute.part == part,
                    )
                    .order_by(ProcessRoute.version.desc())
                    .all()
                ]
                # 同一件名可能被复制过好几份（每张单一份）。逐份比对：
                # 与基线一致的直接删；确有差异的只留最新一份当副本，其余视为重复删掉。
                sig_base = step_signature(db, baseline.id) if baseline else None
                differing = [c for c in copies if sig_base is None or step_signature(db, c.id) != sig_base]
                identical = [c for c in copies if c not in differing]
                keep_copy = differing[0] if differing else None
                drops = list(identical)
                if keep_copy is not None:
                    sig_keep = step_signature(db, keep_copy.id)
                    drops += [c for c in differing[1:] if step_signature(db, c.id) == sig_keep]

                origin = "baseline"
                route_id = baseline.id if baseline else None
                if keep_copy is not None:
                    route_id, origin = keep_copy.id, "copy"
                    kept_copies.append(f"#{keep_copy.id} {product}/{part}")
                elif baseline is None:
                    no_baseline.append(f"{product}/{part}")

                for c in drops:
                    if c is keep_copy:
                        continue
                    if baseline is not None and c in differing:
                        # 有差异但内容与保留的那份相同 → 也是重复
                        dropped_copies.append(f"#{c.id} {product}/{part}")
                    else:
                        dropped_copies.append(f"#{c.id} {product}/{part}（与基线一致）")

                route_plan.append(
                    {
                        "task_id": keeper["id"],
                        "task_no": keeper["task_no"],
                        "route_id": route_id,
                        "origin": origin,
                        "product": product,
                        "part": part,
                        "batch_no": keeper["batch_no"] or "",
                        "qty": keeper["qty"] or 0.0,
                        "seq": seq,
                        "drops": [c for c in drops if c is not keep_copy],
                        "rebind_copy": keep_copy,
                    }
                )

        print("\n=== 路线挂载 ===")
        for r in route_plan:
            print(
                f"  {r['task_no']} ← {r['product']}/{r['part']}"
                f" [{r['origin']}] route={r['route_id']}"
            )
        if dropped_copies:
            print(f"\n  与基线一致、去重的副本（{len(dropped_copies)}）：{', '.join(dropped_copies)}")
        if kept_copies:
            print(f"  与基线不同、保留为副本（{len(kept_copies)}）：{', '.join(kept_copies)}")
        if no_baseline:
            print(f"  没有基线、暂时挂不上路线：{', '.join(no_baseline)}")

        if args.dry:
            print("\n[dry] 未写库")
            return 0

        # ---- 3. 写库
        for r in route_plan:
            if r["rebind_copy"] is not None:
                r["rebind_copy"].task_id = r["task_id"]
            for c in r["drops"]:
                db.query(ProcessRouteStep).filter(
                    ProcessRouteStep.route_id == c.id
                ).delete(synchronize_session=False)
                db.delete(c)
            if r["route_id"]:
                db.add(
                    TaskRoute(
                        task_id=r["task_id"],
                        route_id=r["route_id"],
                        origin=r["origin"],
                        product=r["product"],
                        part=r["part"],
                        batch_no=r["batch_no"],
                        qty=r["qty"],
                        seq=r["seq"],
                        status="active",
                    )
                )

        # ---- 4. 单据归到合并后的任务，删掉被并掉的任务行
        sheet_map: dict[int, int] = {}
        for p in plan:
            keep_id = p["keeper"]["id"]
            for m in p["members"]:
                if m["sheet_id"]:
                    sheet_map[m["sheet_id"]] = keep_id
        for sid, tid in sheet_map.items():
            sh = db.get(Sheet, sid)
            if sh is not None:
                sh.task_id = tid
        db.flush()
        for p in plan:
            for m in p["members"]:
                if m["id"] != p["keeper"]["id"]:
                    dead = db.get(WorkTask, m["id"])
                    if dead is not None:
                        db.delete(dead)
        db.flush()

        # ---- 5. 重算聚合
        for p in plan:
            task = db.get(WorkTask, p["keeper"]["id"])
            if task is not None:
                task_service.refresh_task(db, task)
        db.commit()
    finally:
        db.close()

    # ---- 6. 去掉废弃列（part / batch_no / sheet_id）
    dropped = drop_legacy_columns()
    print(f"清理旧列：{'、'.join(dropped) if dropped else '无需清理'}")

    db = SessionLocal()
    try:
        print("\n=== 结果 ===")
        print(f"  任务：{db.query(WorkTask).count()}")
        print(f"  任务路线：{db.query(TaskRoute).count()}")
        print(f"  基线路线：{db.query(ProcessRoute).filter(ProcessRoute.kind == 'route').count()}")
        print(f"  本单副本：{db.query(ProcessRoute).filter(ProcessRoute.kind == 'task').count()}")
        print(f"  单据挂任务：{db.query(Sheet).filter(Sheet.task_id.isnot(None)).count()}")
    finally:
        db.close()
    return 0


def drop_legacy_columns() -> list[str]:
    """重建 work_task，去掉 part/batch_no/sheet_id 三个语义已失效的列。

    SQLite 的 DROP COLUMN 遇到索引会拒绝，而 sheet_id 上还挂着唯一索引；
    直接重建表最稳。RENAME 必须开 legacy_alter_table，否则 SQLite 会把
    task_route / process_route 里的外键引用一起改写成 work_task_new。
    """
    conn = sqlite3.connect(db_path())
    conn.isolation_level = None
    cur = conn.cursor()
    cols = {r[1] for r in cur.execute("PRAGMA table_info(work_task)")}
    legacy = [c for c in ("part", "batch_no", "sheet_id") if c in cols]
    if not legacy:
        conn.close()
        return []

    keep = [
        "id", "task_no", "title", "customer", "product_id", "product", "period_id",
        "order_date", "due_date", "qty", "actual_hours", "sheet_count", "status",
        "source", "remark", "created_by", "created_at", "updated_at",
    ]
    ddl = """
    CREATE TABLE work_task_new (
        id INTEGER NOT NULL PRIMARY KEY,
        task_no VARCHAR(128) NOT NULL,
        title VARCHAR(128) DEFAULT '',
        customer VARCHAR(128) DEFAULT '',
        product_id INTEGER,
        product VARCHAR(128) DEFAULT '',
        period_id INTEGER,
        order_date VARCHAR(10) DEFAULT '',
        due_date VARCHAR(10) DEFAULT '',
        qty FLOAT DEFAULT 0,
        actual_hours FLOAT DEFAULT 0,
        sheet_count INTEGER DEFAULT 0,
        status VARCHAR(16) DEFAULT 'open',
        source VARCHAR(16) DEFAULT 'manual',
        remark TEXT DEFAULT '',
        created_by VARCHAR(32) DEFAULT '',
        created_at DATETIME,
        updated_at DATETIME
    )
    """
    try:
        cur.execute("PRAGMA foreign_keys=OFF")
        cur.execute("PRAGMA legacy_alter_table=ON")
        cur.execute("BEGIN")
        cur.execute("DROP TABLE IF EXISTS work_task_new")
        cur.execute(ddl)
        common = [c for c in keep if c in cols]
        cur.execute(
            f"INSERT INTO work_task_new ({','.join(common)}) SELECT {','.join(common)} FROM work_task"
        )
        cur.execute("DROP TABLE work_task")
        cur.execute("ALTER TABLE work_task_new RENAME TO work_task")
        cur.execute("CREATE UNIQUE INDEX ix_work_task_task_no ON work_task(task_no)")
        cur.execute("CREATE INDEX ix_work_task_product_id ON work_task(product_id)")
        cur.execute("CREATE INDEX ix_work_task_product ON work_task(product)")
        cur.execute("CREATE INDEX ix_work_task_period_id ON work_task(period_id)")
        cur.execute("CREATE INDEX ix_work_task_status ON work_task(status)")
        cur.execute("COMMIT")
        cur.execute("PRAGMA legacy_alter_table=OFF")
        cur.execute("PRAGMA foreign_keys=ON")
        print(f"  work_task 重建完成，保留 {len(common)} 列")
        return legacy
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
