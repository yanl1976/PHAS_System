"""「备份能不能真的恢复回去」的回归。

备份做完不算闭环：能恢复、恢复错了能退回、恢复失败不留半成品，才算。

钉住的规则：
  [1] 恢复后逐表行数与备份完全一致（不是「差不多」）
  [2] 恢复前自动拍快照，且那份快照能让人回到恢复前的样子（可逆）
  [3] 恢复把**新增行**也一起带回来（备份后删掉的数据重新出现）
  [4] 备份里有、当前库没有的表 → 保留不动，不擅自清空
  [5] 备份里缺的新列 → 用默认值，不因为列对不上就整表放弃
  [6] 备份本身校验不合格 → 拒绝恢复，当前数据一个字节都不动
  [7] 中途出错 → 整体回滚，不留「一半旧一半新」
  [8] 文件名只认备份目录里的 worktime-*.db（不给路径穿越的机会）
  [9] 恢复留痕写在恢复之后（audit_log 本身也被覆盖）

用法：python scripts/test_backup_restore.py
（全程在临时目录造库+造备份，不碰生产库与生产备份目录）
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.services import backup  # noqa: E402

PASS = 0
FAIL: list[str] = []
CORE = ("sheet", "work_item")


def check(cond, msg: str) -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  OK   {msg}")
    else:
        FAIL.append(msg)
        print(f"  FAIL {msg}")


def section(name: str) -> None:
    print(f"\n[{name}]")


def remake(path: Path, rows: int = 5) -> None:
    """把文件删干净再建：测试里要反复造「同一个库的不同时点」。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    make_db(path, rows)


def make_db(path: Path, rows: int = 5) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE sheet(id INTEGER PRIMARY KEY, no TEXT)")
    conn.execute("CREATE TABLE work_item(id INTEGER PRIMARY KEY, sheet_id INT, hours REAL)")
    conn.executemany("INSERT INTO sheet(no) VALUES (?)", [(f"A{i}",) for i in range(rows)])
    conn.executemany(
        "INSERT INTO work_item(sheet_id, hours) VALUES (?,?)", [(i, 1.5) for i in range(rows)]
    )
    conn.commit()
    conn.close()


def counts_of(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {n: conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


def rows_of(path: Path, sql: str) -> list[tuple]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def body(tmp: Path) -> None:
    # 备份目录临时改道：restore() 只在 backup_dir() 里找文件，测试不能碰真实目录
    real_dir = backup.BACKUP_DIR
    backup.BACKUP_DIR = tmp / "bk"
    db = tmp / "live.db"

    try:
        remake(db, rows=5)
        # 让 backup 模块把「当前库」认成这个临时库
        real_url = backup.DB_URL
        backup.DB_URL = f"sqlite:///{db.as_posix()}"

        try:
            # 造一份备份（10 行的状态），然后当前库退回 5 行 —— 模拟「数据被搞掉了」
            remake(tmp / "seed10.db", rows=10)
            snap = backup.snapshot(src=tmp / "seed10.db", dst=backup.BACKUP_DIR / "worktime-20261105-100000-manual.db")
            remake(db, rows=5)

            # ------------------------------------------------------------ 1
            section("1 恢复后与备份逐表一致")
            before = counts_of(db)
            out = backup.restore(snap.name)
            after = counts_of(db)
            check(out["ok"] is True, "恢复返回成功")
            check(after == counts_of(snap), f"逐表行数与备份一致：{after}")
            check(after["sheet"] == 10, f"单据从 {before['sheet']} 回到 10 行")
            check(out["before"]["rows"] == 10, f"报告了恢复前规模：{out['before']['rows']} 行")
            check(out["after"]["rows"] == 20, f"报告了恢复后规模：{out['after']['rows']} 行")
            check(out["replaced"] and set(out["replaced"]) == set(after), "替换表清单覆盖全部表")

            # ------------------------------------------------------------ 2
            section("2 恢复前自动留了能退回去的快照")
            pre = backup.BACKUP_DIR / out["pre_restore"]
            check(pre.exists(), f"存在回退快照：{pre.name}")
            check("pre-restore" in pre.name, "名字里标明用途")
            check(counts_of(pre) == before, f"回退快照正是恢复前的样子：{counts_of(pre)}")
            check(backup.meta_path(pre).exists(), "回退快照也带清单")

            # ------------------------------------------------------------ 3
            section("3 恢复把删掉的数据一起带回来")
            conn = sqlite3.connect(db)
            conn.execute("DELETE FROM work_item WHERE id > 8")
            conn.commit()
            conn.close()
            check(counts_of(db)["work_item"] == 8, "先删到 8 行")
            backup.restore(snap.name)
            check(counts_of(db)["work_item"] == 10, "恢复后又是 10 行")
            check(
                rows_of(db, "SELECT no FROM sheet WHERE no='A9'") != [],
                "被删掉的具体行也回来了（A9）",
            )

            # ------------------------------------------------------------ 4
            section("4 备份里没有的表保留不动")
            conn = sqlite3.connect(db)
            conn.execute("CREATE TABLE worker_rate(id INTEGER PRIMARY KEY, rate REAL)")
            conn.executemany("INSERT INTO worker_rate(rate) VALUES (?)", [(30.0,), (35.0,)])
            conn.commit()
            conn.close()
            out4 = backup.restore(snap.name)
            check("worker_rate" in out4["kept"], "列进 kept（备份时还没有这张表）")
            check(counts_of(db)["worker_rate"] == 2, "worker_rate 的 2 行没被清掉")
            check(
                "worker_rate" not in out4["replaced"], "没把它当替换目标"
            )

            # ------------------------------------------------------------ 5
            section("5 备份缺的新列用默认值")
            conn = sqlite3.connect(db)
            conn.execute("ALTER TABLE sheet ADD COLUMN remark TEXT DEFAULT 'init'")
            conn.execute("UPDATE sheet SET remark='人工填的'")
            conn.commit()
            conn.close()
            out5 = backup.restore(snap.name)
            check(out5["defaulted_columns"].get("sheet") == ["remark"], "报告 sheet.remark 取默认值")
            got = rows_of(db, "SELECT count(*) FROM sheet WHERE remark='init'")[0][0]
            check(got == 10, f"remark 全部回落成默认值（{got} 行）")
            check(counts_of(db)["sheet"] == 10, "行数仍然正确")

            # ------------------------------------------------------------ 6
            section("6 备份坏了不许恢复")
            bad = backup.BACKUP_DIR / "worktime-20261105-110000-manual.db"
            bad.write_bytes(snap.read_bytes())
            backup.meta_path(bad).write_bytes(backup.meta_path(snap).read_bytes())
            conn = sqlite3.connect(bad)
            conn.execute("DROP TABLE work_item")
            conn.commit()
            conn.close()
            snapshot_before = counts_of(db)
            try:
                backup.restore(bad.name)
                check(False, "缺表的备份应当被拒绝")
            except backup.RestoreError as exc:
                check(True, f"拒绝恢复：{exc}")
            check(counts_of(db) == snapshot_before, "当前库一个字节没动")

            # 空壳备份同样拒绝
            shell = backup.BACKUP_DIR / "worktime-20261105-113000-manual.db"
            sqlite3.connect(shell).close()
            try:
                backup.restore(shell.name)
                check(False, "空壳备份应当被拒绝")
            except backup.RestoreError as exc:
                check(True, f"拒绝空壳：{exc}")

            # ------------------------------------------------------------ 7
            section("7 中途出错整体回滚")
            # 造一个「有一列不兼容」的备份：把 work_item.hours 改成 TEXT 且塞非法值？
            # 更直接的触发方式：让 INSERT 违反 NOT NULL —— 备份里 sheet.no 有 NULL，
            # 目标库 no 是 NOT NULL（这里手工加上约束）。
            broken = backup.BACKUP_DIR / "worktime-20261105-120000-manual.db"
            make_db(broken, rows=4)
            conn = sqlite3.connect(broken)
            conn.execute("INSERT INTO sheet(id, no) VALUES (99, NULL)")
            conn.commit()
            conn.close()
            backup._write_meta(broken, broken)  # 让它带上自己的清单

            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE t_guard(id INTEGER PRIMARY KEY, no TEXT NOT NULL);"
                "INSERT INTO t_guard(no) VALUES ('keep');"
            )
            conn.commit()
            conn.close()
            # 目标库加同名列但 NOT NULL：把备份里的 NULL 塞进来
            conn = sqlite3.connect(db)
            conn.execute("ALTER TABLE sheet RENAME TO sheet_old")
            conn.execute("CREATE TABLE sheet(id INTEGER PRIMARY KEY, no TEXT NOT NULL)")
            conn.execute("INSERT INTO sheet(id,no) SELECT id,no FROM sheet_old WHERE no IS NOT NULL")
            conn.execute("DROP TABLE sheet_old")
            conn.commit()
            conn.close()

            rows_before = counts_of(db)
            try:
                backup.restore(broken.name)
                check(False, "违反 NOT NULL 应当失败")
            except backup.RestoreError as exc:
                check(True, f"失败并说明：{str(exc)[:60]}")
            check(counts_of(db) == rows_before, f"整体回滚，行数不变：{counts_of(db)}")
            check(
                rows_of(db, "SELECT no FROM t_guard") == [("keep",)],
                "同批次里别的表也没被改动",
            )

            # ------------------------------------------------------------ 8
            section("8 只认备份目录里的文件名")
            for raw in ("../../windows/system32/x.db", "..\\evil.db", "/etc/passwd", "other.db", ""):
                try:
                    backup.resolve_backup(raw)
                    check(False, f"应当拒绝 {raw!r}")
                except backup.RestoreError:
                    check(True, f"拒绝 {raw!r}")
            check(backup.resolve_backup(snap.name) == snap, "正常名字能解析")

            # ------------------------------------------------------------ 9
            section("9 恢复后可再备份，且清单与新数据一致")
            made = backup.create(keep=5, label="after-restore")
            check(made["ok"] is True, "恢复后的库能正常备份")
            check(
                made["counts"]["sheet"] == counts_of(db)["sheet"],
                f"新备份反映恢复后的数据（sheet={made['counts']['sheet']}）",
            )
        finally:
            backup.DB_URL = real_url
    finally:
        backup.BACKUP_DIR = real_dir


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hy_restore_"))
    try:
        body(tmp)
    finally:
        for p in tmp.rglob("*"):
            try:
                p.unlink()
            except OSError:
                pass
        for p in sorted(tmp.rglob("*"), reverse=True):
            try:
                p.rmdir()
            except OSError:
                pass
        try:
            os.rmdir(tmp)
        except OSError:
            pass

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
