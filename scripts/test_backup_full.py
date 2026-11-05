"""「备份必须是整库全量」的回归。

现场要求（用户原话）：系统管理里的备份要的是**系统数据库全量备份**。

钉住的规则：
  [1] 快照 = 整库副本：建几张表、各塞几行，备份里表数与行数逐表一致
  [2] WAL 里已提交未 checkpoint 的数据必须进备份（这正是不许 cp 的理由）
  [3] 少一张表就判不合格 —— 不管那张表在不在「核心表」名单里
  [4] 空壳文件（一张表都没有）必须判不合格，不能报「可用」
  [5] 基准库读不到时降级：不抛异常、不误报缺表
  [6] 行数比当前库少不算错（备份是历史快照），但要报出 row_delta
  [7] 缓存不能盖住文件变化
  [8] create() 的名字/目录/保留份数

用法：python scripts/test_backup_full.py
（不碰生产库：全程在临时目录造库；create() 的备份目录也临时改道）
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

# 故意用业务里真实存在、但**不在** CORE_TABLES 名单里的表名：
# 旧口径只核对 9 张核心表，这张丢了也照样报「可用」。
SIDE_TABLES = ("worker_rate", "product_node", "print_sheet", "sheet_piece_note")


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


def make_db(path: Path) -> sqlite3.Connection:
    """造一个像生产库的小库：核心表 + 非核心表都有数据。

    核心表要建齐 —— 判「备份合不合格」要拿当前库的表清单当基准，
    基准本身缺表会让结果失真（这也正是要修的口径之一）。

    返回**仍然打开**的连接：WAL 模式要靠它把「已提交、未 checkpoint」的状态留在
    现场，连接一 close 就会 checkpoint 回主文件，那就测不出区别了。
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE sheet(id INTEGER PRIMARY KEY, no TEXT)")
    conn.execute("CREATE TABLE work_item(id INTEGER PRIMARY KEY, sheet_id INT, hours REAL)")
    for t in backup.CORE_TABLES:
        if t in ("sheet", "work_item"):
            continue
        conn.execute(f"CREATE TABLE {t}(id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute(f"INSERT INTO {t}(v) VALUES ('seed')")
    for t in SIDE_TABLES:
        conn.execute(f"CREATE TABLE {t}(id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO sheet(no) VALUES (?)", [(f"A{i}",) for i in range(7)])
    conn.executemany(
        "INSERT INTO work_item(sheet_id, hours) VALUES (?,?)",
        [(i, 1.5) for i in range(1, 24)],
    )
    for t in SIDE_TABLES:
        conn.executemany(f"INSERT INTO {t}(v) VALUES (?)", [("x",), ("y",), ("z",)])
    conn.commit()
    # 模拟「服务正在跑、刚提交、还没落主文件」：这些行只在 -wal 里
    conn.executemany("INSERT INTO sheet(no) VALUES (?)", [("W1",), ("W2",)])
    conn.commit()
    return conn


def table_rows(path: Path) -> dict[str, int]:
    """直接读某个 .db 文件（不做任何补救），用来对比「拷主文件」与「VACUUM INTO」。"""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return {}
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {n: conn.execute(f'SELECT count(*) FROM "{n}"').fetchone()[0] for n in names}
    except sqlite3.Error:
        return {}  # 直接拷出来的主文件可能「连表都没有」
    finally:
        conn.close()


def body(tmp: Path, src: Path, truth: dict[str, int]) -> None:
    # ---------------------------------------------------------------- 1
    section("1 整库全量：表与行逐一对上")
    bak = backup.snapshot(src=src, dst=tmp / "full.db")
    info = backup.verify(bak, src=src)
    check(info["ok"], f"备份合格（{info['tables']} 表 / {info['rows']} 行）")
    check(info["tables"] == len(truth), f"表数一致：{info['tables']} == {len(truth)}")
    check(info["counts"] == truth, "逐表行数与源库完全一致")
    check(not info["missing_tables"], "没有缺表")
    check(info["rows"] == sum(truth.values()), f"总行数一致：{info['rows']}")
    check(info["row_delta"] == 0, "与当前库无行数差")

    # ---------------------------------------------------------------- 2
    section("2 WAL 未 checkpoint 也不丢数据")
    check(truth.get("sheet") == 9, f"源库 sheet 有 9 行（含 WAL 里 2 行）：{truth.get('sheet')}")
    check(
        info["counts"].get("sheet") == 9,
        f"备份里 sheet 也是 9 行，WAL 数据进快照：{info['counts'].get('sheet')}",
    )
    plain = tmp / "plain_copy.db"
    plain.write_bytes(src.read_bytes())  # 模拟「直接拷主文件」
    naive = table_rows(plain)
    check(
        naive.get("sheet", 0) < 9,
        f"反证：直接拷主文件只剩 {naive.get('sheet', 0)} 行（WAL 里的丢了）",
    )

    # ---------------------------------------------------------------- 3
    section("3 缺任意一张表都判不合格（不只核心表）")
    hole = tmp / "hole.db"
    hole.write_bytes(bak.read_bytes())
    backup.meta_path(hole).write_bytes(backup.meta_path(bak).read_bytes())  # 正常备份是「库 + 清单」
    conn = sqlite3.connect(hole)
    conn.execute("DROP TABLE worker_rate")
    conn.commit()
    conn.close()
    info2 = backup.verify(hole, src=src)
    check(info2["ok"] is False, "缺 worker_rate 后判不合格")
    check("worker_rate" in info2["missing_tables"], "缺表清单点名 worker_rate")
    check(not info2["core_missing"], "核心表其实都在（旧口径会放过）")
    check(info2["tables"] == len(truth) - 1, f"表数变成 {info2['tables']}")
    check("worker_rate" in info2["schema_gap"]["missing_vs_now"], "同时标出与当前库的差")

    # 清单丢了（有人只拷了 .db）：退回核心表兜底，不误判成坏 —— 这时能保证的只有核心表
    lone = tmp / "lone.db"
    lone.write_bytes(hole.read_bytes())
    info2b = backup.verify(lone, src=src)
    check(info2b["meta"] is None, "没有清单时不假装有")
    check(info2b["ok"] is True, "只拷库文件时退回核心表口径，不无谓标红")
    check(info2b["missing_tables"] == [], "没清单就不报「相对清单缺表」")

    # ---------------------------------------------------------------- 4
    section("4 空壳快照不能报可用")
    shell = tmp / "shell.db"
    sqlite3.connect(shell).close()
    info3 = backup.verify(shell, src=src)
    check(info3["ok"] is False, "空库判不合格")
    check(info3["empty"] is True, "标记 empty")
    check(info3["tables"] == 0, "表数为 0")
    check(len(info3["core_missing"]) > 0, "没有清单时退回核心表兜底，能看出它是空壳")

    # ---------------------------------------------------------------- 5
    section("5 基准库不在时降级，不误报")
    info4 = backup.verify(bak, src=tmp / "nope.db")
    check(info4["ok"] is True, "基准读不到时不因此判坏")
    check(info4["source"]["compared"] is False, "标记未对比")
    check(not info4["missing_tables"], "不误报缺表")
    check(info4["row_delta"] == 0, "没有行数差")
    check(info4["meta"] is not None, "认得出出厂清单")

    # ---------------------------------------------------------------- 6
    section("6 备份后新增的行只提示、不算错")
    conn = sqlite3.connect(src)
    conn.executemany("INSERT INTO work_item(sheet_id, hours) VALUES (?,?)", [(1, 2.0)] * 4)
    conn.commit()
    conn.close()
    info5 = backup.verify(bak, src=src)
    check(info5["ok"] is True, "备份仍是合格的历史快照")
    check(info5["row_delta"] == 4, f"报出比当前库少 4 行：{info5['row_delta']}")
    check(info5["counts"]["work_item"] == info["counts"]["work_item"], "备份自身行数没被改写")

    # ---------------------------------------------------------------- 7
    section("7 出厂清单是权威：当前库删了表也照样判坏")
    hole2 = tmp / "hole2.db"
    hole2.write_bytes(bak.read_bytes())
    backup.meta_path(hole2).write_bytes(backup.meta_path(bak).read_bytes())
    conn = sqlite3.connect(hole2)
    conn.execute("DROP TABLE product_node")
    conn.commit()
    conn.close()
    conn = sqlite3.connect(src)
    conn.execute("DROP TABLE product_node")  # 当前库也删掉 → 只比当前库的话看不出坏
    conn.commit()
    conn.close()
    info6 = backup.verify(hole2, src=src)
    check(info6["ok"] is False, "清单说当时有 product_node，备份里没有 → 不合格")
    check("product_node" in info6["missing_tables"], "点名缺 product_node")
    check("product_node" not in info6["schema_gap"]["missing_vs_now"], "不会误算成「当时还没有」")
    check(info6["meta"]["tables"] == len(truth), f"清单记着当时 {info6['meta']['tables']} 张表")

    # ---------------------------------------------------------------- 8
    section("8 缓存不盖住文件变化")
    again = backup.verify(bak, src=src)
    check(again["rows"] == info["rows"], "文件没变时复用扫描结果")
    conn = sqlite3.connect(bak)
    conn.execute("DELETE FROM work_item")
    conn.commit()
    conn.close()
    os.utime(bak, None)  # 显式刷新 mtime，模拟文件被替换
    changed = backup.verify(bak, src=src)
    check(changed["rows"] < info["rows"], f"文件被改后行数重算：{changed['rows']}")
    check(changed["counts"]["work_item"] == 0, "work_item 已空（缓存没盖住）")

    # ---------------------------------------------------------------- 9
    section("9 生成的备份名、清单与保留策略")
    # 备份目录临时改到 tmp：create() 会按 keep 清旧文件，不能让它碰机器上真实的备份
    real_dir = backup.BACKUP_DIR
    backup.BACKUP_DIR = tmp / "bk"
    try:
        made = backup.create(keep=3, label="t")
        check(made["ok"] is True, "create 走的是当前库，校验通过")
        check(made["name"].endswith("-t.db"), f"名字带标注：{made['name']}")
        check(made["keep"] == 3, "保留份数写进返回值")
        check(Path(made["file"]).parent == backup.BACKUP_DIR, "落在指定目录")
        check(backup.meta_path(Path(made["file"])).exists(), "同时写出出厂清单")
        listed = backup.listing()
        check(len(listed) == 1, "listing 只数 .db，不会把 .json 当备份")
        # 保留策略要把清单一起收走，别在目录里留孤儿
        nested = backup.BACKUP_DIR / "worktime-20200101-000000-old.db"
        nested.write_bytes(b"x")
        backup.meta_path(nested).write_text("{}", encoding="utf-8")
        os.utime(nested, (0, 0))  # 装成很旧的备份，prune 才会轮到它
        backup.prune(1)
        check(not nested.exists(), "超出保留份数的旧备份被清掉")
        check(not backup.meta_path(nested).exists(), "旧备份的清单一起清掉")
    finally:
        backup.BACKUP_DIR = real_dir


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hy_bak_"))
    src = tmp / "src.db"
    live = make_db(src)  # 保持打开，WAL 不落盘
    try:
        body(tmp, src, table_rows(src))
    finally:
        live.close()

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
        tmp.rmdir()
    except OSError:
        pass

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
