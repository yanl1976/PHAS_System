"""在**生产库的副本**上跑全量回归，跑完删副本。

为什么必须这么做：这些测试会写库（造单据、建任务、发布路线）。虽然都带清理，
但清理逻辑本身也是代码，一样可能有 bug —— 一旦漏删，污染的是真实工资账。
更稳的做法是让测试根本碰不到生产库：用 SQLite 的在线备份 API 拷一份，
把 `WT_DB_URL` 指过去，测试全在副本上跑。

用法：
    .venv/Scripts/python.exe scripts/run_tests.py            # 全跑
    .venv/Scripts/python.exe scripts/run_tests.py task routes # 只跑名字里含这些词的
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "data" / "worktime.db"
# 放系统临时目录：即使删除被环境拦下，也不会在 data/ 里留下一个「像真库」的文件
DST = Path(tempfile.gettempdir()) / "hy_worktime_test.db"


def clone(src: Path, dst: Path) -> None:
    """在线备份：源库在 WAL 模式下直接 cp 会得到一个没有表的空壳，必须走 backup API。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst) + suffix)
        if p.exists():
            p.unlink()
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s:
        # 注意：sqlite3 的 with 只管事务、不关连接；不显式 close 的话
        # Windows 上文件句柄还在，最后删副本会 PermissionError。
        d = sqlite3.connect(dst)
        try:
            s.backup(d)
        finally:
            d.close()


def main() -> int:
    words = [w for w in sys.argv[1:] if not w.startswith("-")]
    scripts = sorted((BASE / "scripts").glob("test_*.py"))
    if words:
        scripts = [p for p in scripts if any(w in p.name for w in words)]
    if not scripts:
        print("没有匹配的测试脚本")
        return 1

    if not SRC.exists():
        print(f"找不到生产库 {SRC}")
        return 1
    clone(SRC, DST)
    print(f"已在副本上运行：{DST.name}（源库 {SRC.name} 不受影响）")
    print("=" * 70)

    env = dict(os.environ)
    env["WT_DB_URL"] = f"sqlite:///{DST.as_posix()}"
    failed: list[str] = []
    t0 = time.time()
    try:
        for p in scripts:
            started = time.time()
            r = subprocess.run(
                [sys.executable, str(p)], cwd=str(BASE), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            tail = [ln for ln in (r.stdout or "").strip().splitlines() if ln.strip()][-3:]
            status = "通过" if r.returncode == 0 else "失败"
            print(f"[{status}] {p.name}  ({time.time() - started:.1f}s)")
            for ln in tail:
                print("      ", ln[:160])
            if r.returncode != 0:
                failed.append(p.name)
                for ln in (r.stderr or "").strip().splitlines()[-6:]:
                    print("        ERR", ln[:160])
    finally:
        left: list[str] = []
        for suffix in ("", "-wal", "-shm"):
            q = Path(str(DST) + suffix)
            if not q.exists():
                continue
            try:
                q.unlink()
            except OSError as exc:
                # 某些环境（沙箱/受管终端）会把删除转交回收站，可能失败。
                # 副本残留不影响生产库，但要说清楚是哪个文件，别让人以为清理干净了。
                left.append(f"{q}（{exc.__class__.__name__}）")
        for x in left:
            print(f"[提示] 副本未能删除：{x}")

    print("=" * 70)
    print(f"共 {len(scripts)} 个脚本，失败 {len(failed)} 个，用时 {time.time() - t0:.0f}s")
    if failed:
        print("失败：", "、".join(failed))
        return 1
    print("生产库未被写入")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
