r"""数据库备份命令行（逻辑在 app/services/backup.py，界面上的「立即备份」共用）。

用法：
    .venv/Scripts/python.exe scripts/backup_db.py                # 备份一次，保留最近 14 份
    .venv/Scripts/python.exe scripts/backup_db.py --keep 30      # 保留 30 份
    .venv/Scripts/python.exe scripts/backup_db.py --list         # 看已有备份与校验结果
    .venv/Scripts/python.exe scripts/backup_db.py --verify FILE  # 校验某个备份能否打开

定时执行（Windows 任务计划，每天 21:30；用管理员 PowerShell 跑一次即可）：
    schtasks /Create /SC DAILY /ST 21:30 /TN "工时系统备份" /TR
      "\"E:\tedri_project\hy\.venv\Scripts\python.exe\" \"E:\tedri_project\hy\scripts\backup_db.py\""
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))


def main() -> int:
    ap = argparse.ArgumentParser(description="工时系统数据库备份")
    ap.add_argument("--keep", type=int, default=14, help="保留最近几份（默认 14）")
    ap.add_argument("--dir", default="", help="备份目录（默认 data/backups）")
    ap.add_argument("--label", default="", help="给备份名加后缀，如 manual")
    ap.add_argument("--list", action="store_true", help="列出已有备份并校验")
    ap.add_argument("--verify", default="", help="校验指定备份文件")
    args = ap.parse_args()

    if args.dir:
        # 必须在导入 config 之前设好：BACKUP_DIR 是模块级常量
        os.environ["WT_BACKUP_DIR"] = args.dir

    from app.services import backup

    if args.list:
        rows = backup.listing()
        if not rows:
            print("还没有备份")
            return 0
        src = backup.source_info()
        print(f"当前库：{src['tables']} 张表 / {src['rows']} 行（备份应与之一致）")
        for r in rows:
            flag = "OK  " if r.get("ok") else "坏  "
            miss = ""
            if not r.get("ok"):
                miss = "  缺：" + "、".join((r.get("missing_tables") or r.get("core_missing") or [])[:5])
            gap = (r.get("schema_gap") or {}).get("missing_vs_now") or []
            if gap:
                # 老备份比现在少表，是功能后来才加的（如 worker_rate / print_sheet），不是损坏
                miss += f"  本份之后新增 {len(gap)} 张表"
            print(
                f"{flag}{r['name']}  {r['mtime']}  "
                f"{r.get('tables', '?')} 表 / {r.get('rows', '?')} 行  {r['size_kb']}KB{miss}"
            )
        return 0

    if args.verify:
        info = backup.verify(Path(args.verify))
        print(
            f"{'OK' if info['ok'] else '不合格'}  {info['tables']} 张表 / {info['rows']} 行  "
            f"quick_check={info['quick_check']}  大小 {info['size_kb']}KB"
        )
        if info["missing_tables"]:
            print("  缺表：" + "、".join(info["missing_tables"]))
        if info["core_missing"]:
            print("  缺核心表：" + "、".join(info["core_missing"]))
        if info["extra_tables"]:
            print("  当前库没有的表（旧备份/已删表）：" + "、".join(info["extra_tables"]))
        return 0 if info["ok"] else 1

    out = backup.create(keep=args.keep, label=args.label)
    if not out["ok"]:
        print(f"备份校验失败：{out}")
        return 1
    print(
        f"已备份 {out['name']}（{out['tables']} 张表 / {out['rows']} 行，"
        f"{out['size_kb']}KB，quick_check {out['quick_check']}）"
    )
    print("  逐表行数：" + "，".join(f"{k} {v}" for k, v in out["counts"].items()))
    if out["pruned"]:
        print("  已清理旧备份：" + "、".join(out["pruned"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
