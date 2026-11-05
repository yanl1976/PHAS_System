"""数据库**全量**备份与校验（VACUUM INTO）。

口径只有一句：备份 = 当前系统数据库的完整副本，一张表都不筛、一行都不漏。

**不要用 cp / 复制粘贴备份**：库跑在 WAL 模式，已提交但还没 checkpoint 的事务
留在 `worktime.db-wal` 里，只拷主文件会得到一个「一张表都没有」的空壳——
恢复时才发现，那时已经晚了。`VACUUM INTO` 由 SQLite 自己写出一致性快照，
不停服务，也顺带整理碎片。

校验同理不能只看几个数字：早先只核对 9 张「核心表」在不在，
结果是**没被列进来的表丢了也照样报「可用」**（`worker_rate` 一丢，工资表
全部算成 0 元，界面还说备份正常）。现在一律：
  1) quick_check 通过、快照不是空壳；
  2) 对着**备份自己的出厂清单**核 —— 快照旁边那个 `.json` 记着打这份备份时
     源库有哪些表、各多少行，少了任何一张都判不合格；
  3) 与当前库的结构差只作**提示**：老备份比现在少几张表是正常的（功能后来才加，
     比如 `worker_rate`/`print_sheet`），那不是损坏，不能一上来就标红吓人。

清单做成库文件外的 sidecar（`xxx.db.json`）而不是库里一张 `_backup_meta` 表：
备份文件必须与源库**一模一样**，恢复回去不能凭空多出一张表。清单丢了（有人只拷
了 .db）也只是退回「只核核心表」的老口径，不影响恢复。

脚本入口见 scripts/backup_db.py；系统管理页的「立即备份」走同一套函数，
两条路的口径必须一致，否则界面上说的和定时任务做的不是一件事。
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
import threading
import time
from pathlib import Path

from ..config import BACKUP_DIR, DB_URL

# 重点业务表：**只用于界面上的可读性**（优先显示单据/工时明细/审计几个数字），
# 以及老备份（没有出厂清单）的兜底校验，见模块说明。
CORE_TABLES = (
    "period",
    "batch",
    "image",
    "sheet",
    "work_item",
    "work_task",
    "task_route",
    "process_route",
    "audit_log",
)

# SQLite 自己维护的内部表（sqlite_sequence 等）不算业务数据，也不参与「缺表」判定。
SYSTEM_PREFIX = "sqlite_"

# 备份文件自身扫描结果（quick_check + 逐表行数）。文件 mtime/size 不变就复用，
# 免得每次打开管理页都把十几份上百 MB 的快照全表扫一遍。
_SCAN_CACHE: dict[str, tuple[tuple[int, int], dict]] = {}
# 当前库的表与行数：备份对比的基准。30 秒内复用，同一屏里的多份备份只统计一次。
_SRC_TTL = 30.0
_SRC_CACHE: dict[str, object] = {"at": 0.0, "data": {}}
_LOCK = threading.Lock()


def db_path() -> Path:
    if not DB_URL.startswith("sqlite"):
        raise RuntimeError("当前数据库不是 SQLite，请用对应数据库的备份工具")
    return Path(DB_URL.split("///", 1)[1])


def backup_dir() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    return BACKUP_DIR


def meta_path(bak: Path) -> Path:
    """出厂清单：`worktime-xxx.db` 旁边一个 `worktime-xxx.db.json`。"""
    return Path(str(bak) + ".json")


def _connect_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)


def _user_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE ? ORDER BY name",
            (SYSTEM_PREFIX + "%",),
        )
    ]


def _counts(conn: sqlite3.Connection, tables: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for t in tables:
        try:
            out[t] = conn.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        except sqlite3.Error:
            out[t] = -1  # 表在但读不出来：保留名字，计数记 -1，交给 ok 判定
    return out


def _scan(path: Path) -> dict:
    """读一份备份：完整性、表清单、逐表行数。结果按文件状态缓存。"""
    st = path.stat()
    key = str(path)
    # 用 mtime_ns：秒级 mtime 在同一秒内两次改动上分辨不出来，缓存会盖住变化
    stamp = (st.st_mtime_ns, st.st_size)
    with _LOCK:
        hit = _SCAN_CACHE.get(key)
        if hit and hit[0] == stamp:
            return dict(hit[1])

    conn = _connect_ro(path)
    try:
        check = conn.execute("PRAGMA quick_check").fetchone()[0]
        tables = _user_tables(conn)
        counts = _counts(conn, tables)
    finally:
        conn.close()

    data = {
        "quick_check": check,
        "tables": len(tables),
        "rows": sum(v for v in counts.values() if v > 0),
        "counts": counts,
        "table_list": tables,
        "size_kb": round(st.st_size / 1024, 1),
        "bad_tables": [t for t, n in counts.items() if n < 0],
    }
    with _LOCK:
        _SCAN_CACHE[key] = (stamp, dict(data))
    return data


def _source_counts(path: Path | None = None) -> dict:
    """当前库（对比基准）的表与行数。库不存在/读不了时返回空，不做「缺表」误判。"""
    path = path or db_path()
    if not path.exists():
        return {"compared": False, "tables": 0, "rows": 0, "counts": {}, "name": path.name}
    now = time.monotonic()
    if path == db_path():
        with _LOCK:
            if now - float(_SRC_CACHE["at"]) < _SRC_TTL and _SRC_CACHE["data"]:
                return dict(_SRC_CACHE["data"])  # type: ignore[arg-type]
    try:
        conn = _connect_ro(path)
        try:
            tables = _user_tables(conn)
            counts = _counts(conn, tables)
        finally:
            conn.close()
    except sqlite3.Error:
        return {"compared": False, "tables": 0, "rows": 0, "counts": {}, "name": path.name}

    data = {
        "compared": True,
        "tables": len(tables),
        "rows": sum(v for v in counts.values() if v > 0),
        "counts": counts,
        "name": path.name,
    }
    if path == db_path():
        with _LOCK:
            _SRC_CACHE["at"] = now
            _SRC_CACHE["data"] = dict(data)
    return data


def _read_meta(bak: Path) -> dict | None:
    """读出厂清单；文件不在/坏了都返回 None（老备份），不让它影响恢复。"""
    p = meta_path(bak)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_meta(bak: Path, src: Path) -> dict | None:
    """把「打这份备份时源库长什么样」写在旁边一格。

    失败不算备份失败：清单只是校验用的旁证，没有它照样能恢复。
    """
    try:
        conn = _connect_ro(src)
        try:
            tables = _user_tables(conn)
            counts = _counts(conn, tables)
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    data = {
        "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": src.name,
        "tables": len(tables),
        "rows": sum(v for v in counts.values() if v > 0),
        "table_list": tables,
        "counts": counts,
        "note": "备份出厂清单：恢复时对照这份表清单判断备份是否完整，不属于数据库内容",
    }
    try:
        meta_path(bak).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        return None
    return data


def verify(path: Path, src: Path | None = None) -> dict:
    """这份备份能不能**完整**恢复。

    判定顺序：
      1) quick_check 通过、不是空表壳；
      2) 有出厂清单 → 对着清单核表，缺一张就是真损坏（**与当前库无关**：
         哪怕现在库里也已经没有这张表，清单说当时有，那就是备份坏了）；
      3) 没有清单（老备份）→ 退回「核心表都在」的兜底口径；
      4) 与当前库的结构差/行数差只作提示，不参与合格判定。

    `src` 只给测试用（拿临时库当基准），线上永远比当前库。
    """
    path = Path(path)
    scan = _scan(path)
    base = _source_counts(src)
    meta = _read_meta(path)

    counts = scan["counts"]
    empty = scan["tables"] == 0
    bad_tables = scan["bad_tables"]

    expected = set(meta.get("table_list") or []) if meta else set()
    # 相对出厂清单缺的表 = 真损坏；清单里没有的历史备份，这一项必须为空。
    missing = sorted(expected - set(counts))
    # 与当前库的结构差：老备份少几张是功能后来才加的，只是提示。
    gap_missing = sorted(t for t in base["counts"] if t not in counts)
    gap_new = sorted(t for t in counts if base["counts"] and t not in base["counts"])
    core_missing = [t for t in CORE_TABLES if t not in counts]

    # 备份后新增的行不算错（快照本来就停在那一刻），只把方向报出来给界面提示。
    row_delta = base["rows"] - scan["rows"] if base["compared"] else 0

    ok = (
        scan["quick_check"] == "ok"
        and not empty
        and not bad_tables
        and (not missing if meta else not core_missing)
    )
    return {
        "ok": ok,
        "empty": empty,
        "quick_check": scan["quick_check"],
        "tables": scan["tables"],
        "rows": scan["rows"],
        "missing_tables": missing,
        "extra_tables": sorted(set(counts) - expected) if meta else [],
        "core_missing": core_missing if not meta else [],
        "bad_tables": bad_tables,
        "meta": {"at": meta.get("at"), "tables": meta.get("tables"), "rows": meta.get("rows")}
        if meta
        else None,
        "schema_gap": {
            "missing_vs_now": gap_missing,  # 当前库有、备份没有：多半是备份那会儿还没这功能
            "new_in_now": gap_new,  # 备份有、当前库没有：删过表或恢复了更老的版本
        },
        "row_delta": row_delta,
        "source": {
            "compared": base["compared"],
            "name": base["name"],
            "tables": base["tables"],
            "rows": base["rows"],
        },
        "counts": counts,
        "core": {t: counts.get(t, -1) for t in CORE_TABLES},
        "size_kb": scan["size_kb"],
    }


def snapshot(src: Path | None = None, dst: Path | None = None) -> Path:
    src = src or db_path()
    if not src.exists():
        raise RuntimeError(f"数据库不存在：{src}")
    if dst is None:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = backup_dir() / f"worktime-{stamp}.db"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    # VACUUM INTO 拷的是**整个库**：所有表、所有索引、所有数据，不经过应用层筛选。
    conn = _connect_ro(src)
    try:
        conn.execute("VACUUM INTO ?", (str(dst),))
    finally:
        conn.close()
    # 出厂清单：记下这一刻源库有哪些表、各多少行。校验靠它分辨
    # 「备份少了一张表（坏）」和「那会儿还没有这张表（正常）」。
    _write_meta(dst, src)
    return dst


def prune(keep: int) -> list[str]:
    files = sorted(backup_dir().glob("worktime-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for f in files[max(1, keep):]:
        try:
            f.unlink()
            removed.append(f.name)
        except OSError:
            continue
        # 清单跟着库文件一起走，别在目录里留一堆指向已删文件的 .json
        try:
            meta_path(f).unlink()
        except OSError:
            pass
    return removed


def create(keep: int = 14, label: str = "") -> dict:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"worktime-{stamp}{('-' + label) if label else ''}.db"
    dst = snapshot(dst=backup_dir() / name)
    info = verify(dst)
    return {
        "file": str(dst),
        "name": dst.name,
        **info,
        "pruned": prune(keep),
        "keep": keep,
        "at": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def listing(limit: int = 20) -> list[dict]:
    files = sorted(backup_dir().glob("worktime-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for f in files[:limit]:
        try:
            info = verify(f)
        except Exception as exc:  # 坏文件也要列出来，而不是假装没有
            info = {
                "ok": False,
                "error": str(exc),
                "counts": {},
                "core": {},
                "tables": 0,
                "rows": 0,
                "size_kb": 0,
            }
        out.append(
            {
                "name": f.name,
                "path": str(f),
                "mtime": dt.datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                **info,
            }
        )
    return out


def source_info() -> dict:
    """当前库规模，给界面证明「备份覆盖了全部这些表」。

    `counts` 是逐表行数，备份明细里用它做对照（备份后新增的行会标出来）。
    """
    base = _source_counts()
    return {
        "name": base["name"],
        "tables": base["tables"],
        "rows": base["rows"],
        "compared": base["compared"],
        "counts": base["counts"],
        "core": {t: base["counts"].get(t, -1) for t in CORE_TABLES},
    }


# ------------------------------------------------------------------ 恢复
class RestoreError(RuntimeError):
    """恢复失败，消息本身就是给操作员看的话（接口直接回 400）。"""


def resolve_backup(name: str) -> Path:
    """把界面/命令行传进来的名字解析成备份目录里的真实路径。

    只认备份目录里的 `worktime-*.db`：恢复是往库里写整份数据，
    绝不能让请求里的路径（`..\\..\\随便什么.db`）决定往数据库里灌什么。
    """
    raw = (name or "").strip()
    if not raw:
        raise RestoreError("请指定要恢复的备份文件")
    target = Path(raw).name  # 去掉任何目录成分
    if target != raw:
        raise RestoreError("只能填备份文件名，不要带路径")
    if not (target.startswith("worktime-") and target.endswith(".db")):
        raise RestoreError("只能恢复本系统的备份文件（worktime-*.db）")
    path = backup_dir() / target
    if not path.exists():
        raise RestoreError(f"备份文件不存在：{target}")
    return path


def _queue_busy() -> int:
    """识别队列里还有多少张在跑：恢复时 worker 也在写这个库，必须错开。"""
    try:
        conn = _connect_ro(db_path())
        try:
            n = conn.execute(
                "SELECT count(*) FROM image WHERE status IN ('pending','processing')"
            ).fetchone()[0]
        finally:
            conn.close()
        return int(n or 0)
    except sqlite3.Error:
        return 0


def _cols(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    prefix = "" if schema == "main" else schema + "."
    return [r[1] for r in conn.execute(f'PRAGMA {prefix}table_info("{table}")')]


def restore(name: str, *, force: bool = False) -> dict:
    """把某份备份的**数据**整表写回当前库。

    ## 为什么不是「把 .db 覆盖回去」

    服务是多进程（Web 4 worker + 独立 OCR worker），库文件被一堆连接占着。
    在 Windows 上替换文件本身就会失败；就算替换成功，其他进程的连接还在旧页面/
    旧 WAL 上，接着写回去就把恢复结果冲掉了。

    所以走**标准 SQL 事务 + 整表替换**：
      ATTACH 备份 → BEGIN IMMEDIATE → 逐表 DELETE + INSERT → COMMIT
    * 单事务：中途任何一张表失败就整体回滚，不会留下「一半旧一半新」的库；
    * 走 WAL 正常路径：其他进程的连接读到的是事务后的新版本，不需要重启服务；
    * 结构不动：只覆盖数据，当前库的表/索引/触发器保留 —— 恢复旧备份不会把
      新版本加的表结构一起降级。

    ## 三条不能省的护栏

    1. **先给当前库拍一份快照**（`*-pre-restore.db`）：恢复错了还能退回来。
       这一步失败就直接放弃恢复，绝不硬上。
    2. **识别队列必须空闲**：worker 正在写时恢复，两边会互相盖。
    3. **备份必须校验通过**：拿一份残缺的快照恢复，等于把残缺变成现状。

    ## 备份里有、当前库里没有的表

    保留不动，只在结果里列出来（`kept_tables`）。默认「少丢东西」：
    从旧备份恢复时，那几张表（`worker_rate`、`print_sheet`…）当时还不存在，
    清空它们等于把后来录的工时单价一起抹掉。宁可提示，不可自作主张删数据。
    """
    started = time.monotonic()
    bak = resolve_backup(name)
    info = verify(bak)
    if not info["ok"]:
        raise RestoreError(f"这份备份本身校验不合格，不能用来恢复：{_why_bad(info)}")
    if info["tables"] == 0:
        raise RestoreError("备份里一张表都没有，拒绝用它覆盖当前库")

    if not force:
        busy = _queue_busy()
        if busy:
            raise RestoreError(
                f"识别队列还有 {busy} 张在跑，恢复会与识别进程互相覆盖，请等队列清空后重试"
            )

    dbp = db_path()
    with _LOCK:  # 基准数字要现算：缓存里可能还是上一次的
        _SRC_CACHE["at"] = 0.0
        _SRC_CACHE["data"] = {}
    before = _source_counts()

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    pre = snapshot(dst=backup_dir() / f"worktime-{stamp}-pre-restore.db")

    conn = sqlite3.connect(str(dbp), timeout=30, isolation_level=None)
    replaced: list[str] = []
    created: list[str] = []
    kept: list[str] = []
    defaulted: dict[str, list[str]] = {}
    try:
        conn.execute("PRAGMA foreign_keys=OFF")  # 整表替换不按依赖顺序来
        conn.execute("ATTACH DATABASE ? AS bak", (str(bak),))
        cur_tables = _user_tables(conn)
        bak_tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM bak.sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        bak_set = set(bak_tables)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for t in cur_tables:
                if t not in bak_set:
                    kept.append(t)
                    continue
                here = _cols(conn, t)
                there = _cols(conn, t, "bak")
                common = [c for c in here if c in there]
                if not common:
                    # 列名完全对不上（换过表结构）：宁可保留原数据，也不清空
                    kept.append(t)
                    continue
                collist = ", ".join(f'"{c}"' for c in common)
                conn.execute(f'DELETE FROM main."{t}"')
                conn.execute(f'INSERT INTO main."{t}"({collist}) SELECT {collist} FROM bak."{t}"')
                replaced.append(t)
                lost = [c for c in here if c not in there]
                if lost:
                    defaulted[t] = lost
            # 备份里有、当前库没有的表：按备份的建表语句补上（跨版本降级恢复时才会遇到）
            for t in bak_tables:
                if t in cur_tables:
                    continue
                row = conn.execute(
                    "SELECT sql FROM bak.sqlite_master WHERE type='table' AND name=?", (t,)
                ).fetchone()
                if not row or not row[0]:
                    continue
                conn.execute(row[0])
                there = _cols(conn, t, "bak")
                collist = ", ".join(f'"{c}"' for c in there)
                conn.execute(f'INSERT INTO main."{t}"({collist}) SELECT {collist} FROM bak."{t}"')
                created.append(t)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            raise RestoreError(f"恢复失败，已整体回滚（当前数据未改动）：{exc}") from exc
    finally:
        try:
            conn.execute("DETACH DATABASE bak")
        except sqlite3.Error:
            pass
        conn.close()

    with _LOCK:
        _SRC_CACHE["at"] = 0.0
        _SRC_CACHE["data"] = {}
    after = _source_counts()
    # 回退快照也要受份数上限约束，否则每恢复一次就多一份，目录迟早堆满。
    # 上限放到 30（比日常 14 宽松），避免恢复动作把正常备份挤掉。
    pruned = prune(30)

    return {
        "ok": True,
        "name": bak.name,
        "restored_from": {
            "name": bak.name,
            "at": (info.get("meta") or {}).get("at"),
            "mtime": dt.datetime.fromtimestamp(bak.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "tables": info["tables"],
            "rows": info["rows"],
        },
        "before": {"tables": before["tables"], "rows": before["rows"], "counts": before["counts"]},
        "after": {"tables": after["tables"], "rows": after["rows"], "counts": after["counts"]},
        "pre_restore": pre.name,
        "replaced": replaced,
        "created": created,
        "kept": kept,
        "defaulted_columns": defaulted,
        "pruned": pruned,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }


def _why_bad(info: dict) -> str:
    if info.get("empty"):
        return "快照里一张表都没有"
    miss = info.get("missing_tables") or []
    if miss:
        return "缺表 " + "、".join(miss[:5])
    core = info.get("core_missing") or []
    if core:
        return "缺核心表 " + "、".join(core)
    return f"quick_check={info.get('quick_check')}"
