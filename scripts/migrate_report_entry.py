"""把 work_item.sheet_id 的 NOT NULL 去掉（扫码报工没有纸质单据）。

## 为什么要重建整张表

SQLite 的 `ALTER TABLE` 只支持「加列 / 改名」，**改不了约束**。
而 `CREATE TABLE IF NOT EXISTS`（`init_db` 用的就是它）对已存在的表什么也不做，
所以新库是「可空」、老库还是「必填」，两边行为不一致 —— 这种差异最难查，
因为开发机上永远是对的。

所以这里按 SQLAlchemy 的模型定义重造一张表，把数据搬过去，再换名。
模型里 `sheet_id` 已经是 `nullable=True`，生成的 DDL 天然带对的约束，
不需要手写列清单（手写就一定会漏掉后来 ALTER 加的那些列）。

## 上线前的三件保险

1. 先备份整个库文件（不是 dump，是 cp）——出问题直接换回来。
2. 全程在事务里；失败就 rollback，旧表一个字节都不动。
3. 迁移后自检：行数、采样值、`PRAGMA table_info` 的 notnull 标志。

用法：`.venv/Scripts/python.exe scripts/migrate_report_entry.py [--apply]`
不加 --apply 只体检，不写库。
"""
from __future__ import annotations

import datetime as dt
import shutil
import sqlite3
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from app.config import DB_URL  # noqa: E402
from app.db import _ADDED_COLUMNS, engine, init_db  # noqa: E402
from app.models import WorkItem  # noqa: E402

DB_FILE = BASE / "data" / "worktime.db"


def _info(cur, table: str) -> dict[str, dict]:
    rows = cur.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1]: {"type": r[2], "notnull": r[3], "default": r[4], "pk": r[5]} for r in rows}


def main() -> int:
    apply = "--apply" in sys.argv
    if not DB_URL.startswith("sqlite"):
        print(f"这个脚本只处理 SQLite，当前是 {DB_URL}")
        return 1
    if not DB_FILE.exists():
        print(f"找不到库文件 {DB_FILE}")
        return 1

    print(f"库文件：{DB_FILE}（{DB_FILE.stat().st_size / 1048576:.1f} MB）")

    # init_db 先把新表建出来、把新列补上（ALTER ADD COLUMN 对 NOT NULL 无影响）
    init_db()

    con = sqlite3.connect(str(DB_FILE))
    con.execute("PRAGMA foreign_keys=OFF")
    cur = con.cursor()

    info = _info(cur, "work_item")
    total = cur.execute("SELECT COUNT(*) FROM work_item").fetchone()[0]
    nullable_ocr = cur.execute("SELECT COUNT(*) FROM work_item WHERE sheet_id IS NULL").fetchone()[0]
    print(f"work_item：{total} 行，sheet_id 可空={info['sheet_id']['notnull'] == 0}，当前空值 {nullable_ocr} 行")

    new_cols = [c for c in _ADDED_COLUMNS["work_item"] if c not in info]
    if new_cols:
        print(f"缺列：{new_cols}")

    need_rebuild = info["sheet_id"]["notnull"] == 1
    if not need_rebuild:
        print("\n[OK] sheet_id 已经可空，无需重建。")
        print("新表检查：", cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'print_sheet%'"
        ).fetchall())
        con.close()
        return 0

    print("\n[需要重建] sheet_id 是 NOT NULL，扫码报工写不进来。")

    # 先看一遍「重建会不会丢东西」：模型里的列 vs 表里的列
    model_cols = [c.name for c in WorkItem.__table__.columns]
    old_cols = set(info)
    lost = sorted(old_cols - set(model_cols))
    if lost:
        print(f"  [警告] 表里有模型没定义的列，重建会丢：{lost}")
        return 2
    print(f"  模型 {len(model_cols)} 列，表 {len(old_cols)} 列，逐列可对上")

    if not apply:
        print("\n（未加 --apply，什么也没改。确认无误后重跑加 --apply）")
        con.close()
        return 0

    backup = BASE / "data" / f"worktime.db.bak-pre-report-{dt.datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(DB_FILE, backup)
    print(f"  已备份 → {backup.name}")

    ddl = str(CreateTable(WorkItem.__table__).compile(engine))
    ddl = ddl.replace("CREATE TABLE work_item", "CREATE TABLE work_item__new", 1)
    if "NOT NULL" in ddl.split("sheet_id")[1].split(",")[0]:
        print("  [中止] 生成的 DDL 里 sheet_id 仍是 NOT NULL，模型没改干净")
        return 3

    try:
        cur.execute("BEGIN")
        cur.execute(ddl)
        shared = [c for c in model_cols if c in old_cols]
        cols = ", ".join(shared)
        cur.execute(f"INSERT INTO work_item__new ({cols}) SELECT {cols} FROM work_item")
        cur.execute("DROP TABLE work_item")
        cur.execute("ALTER TABLE work_item__new RENAME TO work_item")
        # 索引跟着丢，重建（列上的 index=True 会自动出现在 table.indexes 里）
        created = []
        for idx in WorkItem.__table__.indexes:
            try:
                cur.execute(str(CreateIndex(idx).compile(engine)))
                created.append(idx.name)
            except sqlite3.OperationalError as exc:
                print(f"  [跳过索引] {idx.name}: {exc}")
        con.commit()
        print(f"  重建完成，索引：{created}")
    except Exception as exc:  # noqa: BLE001
        con.rollback()
        print(f"  [失败并已回滚] {exc}")
        return 4

    # 自检
    info2 = _info(cur, "work_item")
    total2 = cur.execute("SELECT COUNT(*) FROM work_item").fetchone()[0]
    print("\n迁移后自检：")
    print(f"  行数 {total} → {total2} {'OK' if total == total2 else '不一致！'}")
    print(f"  sheet_id 可空：{info2['sheet_id']['notnull'] == 0}")
    print(f"  工时合计 {cur.execute('SELECT ROUND(SUM(total_hours), 4) FROM work_item').fetchone()[0]}")
    print(f"  索引 {[r[0] for r in cur.execute('PRAGMA index_list(work_item)').fetchall()]}")
    new_tables = [
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'print_sheet%'"
        ).fetchall()
    ]
    print(f"  新表 {new_tables}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
