r"""数据库恢复（命令行，停服后使用）。

系统管理页面里的「恢复」走的是**在线整表替换**（不用停服务，能做单事务回滚），
日常误删数据用它。这个脚本是另一条路，专门对付「库文件本身打不开/损坏」：
先停服务，再整文件替换 —— 连库结构一起回到备份时点。

用法：
    .venv/Scripts/python.exe scripts/restore_db.py --list
    .venv/Scripts/python.exe scripts/restore_db.py --file worktime-20261105-125029-manual.db
    .venv/Scripts/python.exe scripts/restore_db.py --file D:\U盘\worktime-xxx.db --yes
    .venv/Scripts/python.exe scripts/restore_db.py --file xxx.db --check-only   # 只校验，不动库

为什么必须停服务：
    Web 4 个 worker + 独立 OCR 进程都开着这个库。文件替换（os.replace）在目标被占用时
    会直接失败，这是好事 —— 失败说明你没停服，库一点没动。真硬替换过去更糟：那些进程
    手里还有旧的 WAL 和页面缓存，接着写就把恢复结果冲掉了。

三个不能省的步骤（脚本按顺序做）：
    1. 校验备份能打开、表清单完整；不完整就退出，绝不拿残缺的库覆盖现场；
    2. 把当前库先完整备份一份（`-pre-restore.db`），恢复错了还能退；
    3. 复制到临时文件 → os.replace 原子替换 → **删掉 -wal / -shm**。
       这一步不能少：残留的旧 -wal 会被 SQLite 当成新库的日志重放，等于把旧数据盖回来。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def human(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def audit(path: Path, user: str, note: str, before: str) -> None:
    """恢复也要留痕。此时库就是备份里的那份，直接插一条即可。"""
    try:
        conn = sqlite3.connect(str(path), timeout=10)
        conn.execute(
            "INSERT INTO audit_log(entity, entity_id, sheet_id, field, old_value, new_value, user) "
            "VALUES ('backup', 0, NULL, 'backup:restore', ?, ?, ?)",
            (before, note, user),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        print(f"[!] 留痕写入失败（不影响已恢复的数据）：{exc}")


def main() -> int:
    ap = argparse.ArgumentParser(description="工时系统数据库恢复（停服整文件替换）")
    ap.add_argument("--list", action="store_true", help="列出备份并校验")
    ap.add_argument("--file", default="", help="要恢复的备份文件（名字或完整路径）")
    ap.add_argument("--check-only", action="store_true", help="只校验备份，不改动当前库")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认（脚本化时用）")
    args = ap.parse_args()

    from app.services import backup

    if args.list:
        rows = backup.listing()
        if not rows:
            print("还没有备份")
            return 0
        src = backup.source_info()
        print(f"当前库：{src['tables']} 张表 / {human(src['rows'])} 行")
        for r in rows:
            miss = ""
            if not r.get("ok"):
                miss = "  缺：" + "、".join((r.get("missing_tables") or r.get("core_missing") or [])[:5])
            print(
                f"  {'OK ' if r.get('ok') else '坏 '}{r['name']}  {r['mtime']}  "
                f"{r.get('tables', '?')} 表 / {human(r.get('rows'))} 行  {r['size_kb']}KB{miss}"
            )
        return 0

    if not args.file:
        ap.print_help()
        return 2

    # 允许任意路径（应急时备份常在 U 盘或另一台机器上），但一定打印实际用的是哪个文件
    raw = Path(args.file)
    bak = raw if (raw.is_absolute() or raw.exists()) else backup.backup_dir() / raw
    if not bak.exists():
        print(f"[X] 找不到备份文件：{bak}")
        return 1

    dbp = backup.db_path()
    info = backup.verify(bak, src=dbp if dbp.exists() else None)
    print(f"备份：{bak}")
    print(
        f"  {info['tables']} 张表 / {human(info['rows'])} 行 / {info['size_kb']}KB  "
        f"quick_check={info['quick_check']}"
    )
    if info.get("missing_tables"):
        print(f"  [!] 比出厂清单少：{'、'.join(info['missing_tables'])}")
    if not info["ok"]:
        print("[X] 这份备份校验不合格，拒绝用它覆盖当前库。")
        return 1
    if args.check_only:
        print("[OK] 校验通过（未改动当前库）")
        return 0

    before = backup.source_info()
    print(f"当前库：{dbp}")
    print(f"  {before['tables']} 张表 / {human(before['rows'])} 行")
    print("即将用备份覆盖当前库（服务必须先停掉）。")
    if not args.yes:
        try:
            got = input("确认继续？输入 yes 回车：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            got = ""
        if got not in ("yes", "y"):
            print("已取消，什么都没动。")
            return 1

    # 1) 先给当前库留退路。这一步失败就不往下走 —— 没有回退方案的覆盖不做。
    pre = None
    if dbp.exists():
        pre = backup.create(keep=30, label="pre-restore")
        if not pre.get("ok"):
            print(f"[X] 恢复前备份校验未通过，已中止：{pre.get('name')}")
            return 1
        print(f"[1/3] 已备份当前库：{pre['name']}")
    else:
        print("[1/3] 当前库不存在（全新机器），跳过恢复前备份")

    # 2) 复制到同目录临时文件再原子替换：目标被占用时 os.replace 会失败，
    #    失败即说明服务没停，此时原库完好无损。
    fd, tmp_name = tempfile.mkstemp(prefix="restore-", suffix=".db", dir=str(dbp.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copyfile(bak, tmp)
        os.replace(tmp, dbp)
        print("[2/3] 已替换库文件")
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        print(f"[X] 替换失败：{exc}")
        print("    多半是服务还在跑（库文件被占用）。请先停 Web 与 OCR worker，再重试。")
        print(f"    当前库未改动；刚才的备份在 {pre['name'] if pre else '（无需）'}")
        return 1

    # 3) 旧 WAL / SHM 必须删：不然 SQLite 会把备份时点之后的旧日志重放进新库
    dropped = []
    for suffix in ("-wal", "-shm"):
        side = Path(str(dbp) + suffix)
        if side.exists():
            side.unlink()
            dropped.append(side.name)
    print(f"[3/3] 清理残留日志：{'、'.join(dropped) if dropped else '无'}")

    after = backup.verify(dbp)
    note = f"{bak.name}；恢复到 {(info.get('meta') or {}).get('at') or '备份时点'}"
    if pre:
        note += f"；回退快照 {pre['name']}"
    audit(dbp, "cli", note, f"恢复前 {before['tables']} 表/{before['rows']} 行")

    print(
        f"[OK] 已恢复：{after['tables']} 张表 / {human(after['rows'])} 行  "
        f"quick_check={after['quick_check']}"
    )
    if pre:
        print(f"     回退快照：{pre['name']}（要退回再跑一次 --file {pre['name']}）")
    if not after["ok"]:
        print("[!] 恢复后的库校验没通过，请立刻用回退快照退回。")
        return 1
    print("现在可以启动服务了。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
