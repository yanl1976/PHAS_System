"""数据库连接与会话管理（SQLite + WAL）。"""
from __future__ import annotations

from collections.abc import Iterator
import json

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import DB_URL


class Base(DeclarativeBase):
    pass


engine = create_engine(
    DB_URL,
    # timeout：多进程/测试并发写同一个 SQLite 文件时给足等锁时间，
    # 否则「database is locked」会以 500 的形式冒到界面上
    connect_args=(
        {"check_same_thread": False, "timeout": 30} if DB_URL.startswith("sqlite") else {}
    ),
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _rec):  # pragma: no cover - 连接级设置
    if not DB_URL.startswith("sqlite"):
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  确保模型注册

    Base.metadata.create_all(bind=engine)
    _ensure_columns()


# 轻量迁移：create_all 只建缺失的表，不会给已有表补列。
# 新增字段时在这里登记一次，启动即自动补，避免存量库报 no such column。
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "work_item": {
        "process_raw": "VARCHAR(64) DEFAULT ''",
        "declared_total": "FLOAT",
        "material": "VARCHAR(64) DEFAULT ''",
        "handover": "VARCHAR(32) DEFAULT ''",
        "inspector_raw": "VARCHAR(32) DEFAULT ''",
        "finish_date_raw": "VARCHAR(32) DEFAULT ''",
        "process_std": "VARCHAR(32) DEFAULT ''",
        # 报工来源与扫码报工字段（P4）。sheet_id 的 NOT NULL 约束 SQLite 不能
        # ALTER 掉，只能靠 scripts/migrate_report_entry.py 重建表，这里补不了。
        "source": "VARCHAR(8) DEFAULT 'ocr'",
        "period_id": "INTEGER",
        "task_id": "INTEGER",
        "print_form_no": "VARCHAR(32) DEFAULT ''",
        "qty_done": "FLOAT",
        "reported_at": "DATETIME",
        "reported_by": "VARCHAR(32) DEFAULT ''",
        "hours_actual": "FLOAT",
        "sign_verified": "BOOLEAN DEFAULT 0",
        "adjust_reason": "VARCHAR(64) DEFAULT ''",
    },
    "process_dict": {"active": "BOOLEAN DEFAULT 1", "default_seq": "INTEGER DEFAULT 999"},
    "worker": {"employee_no": "VARCHAR(32) DEFAULT ''"},
    "audit_log": {"sheet_id": "INTEGER", "seq": "INTEGER"},
    # P1 任务层：路线与工时都要能挂到产品主数据和任务上。
    # 这里用纯 INTEGER 而不是 REFERENCES —— SQLite 的 ADD COLUMN 不支持
    # 带外键约束的列，归属约束由 ORM 与迁移脚本保证。
    "process_route": {
        "product_id": "INTEGER",
        "task_id": "INTEGER",
        "snapshot_of_id": "INTEGER",
        "snapshot_version": "INTEGER",
        "snapshot_status": "VARCHAR(16) DEFAULT ''",
        # P3 发布留痕：基准是谁定的、什么时候定的
        "published_at": "DATETIME",
        "published_by": "VARCHAR(32) DEFAULT ''",
    },
    "standard_hour": {
        "product_id": "INTEGER",
        "sample_item_ids": "TEXT DEFAULT '[]'",
        "origin": "VARCHAR(16) DEFAULT 'import'",
        "computed_at": "DATETIME",
    },
    # P2 订单化：任务从「一张单=一个任务」改成「一个订单=一个任务」，
    # 工艺路线通过 task_route 引用复用，单据反过来挂到任务上。
    "sheet": {
        "task_id": "INTEGER",
        # 预印单号：识别时用它反查「这张纸是不是我们自己印的」，
        # 也是拦住「同一张纸既扫码报工、又被识别入账」的幂等键。
        "print_form_no": "VARCHAR(32) DEFAULT ''",
    },
    "work_task": {
        "title": "VARCHAR(128) DEFAULT ''",
        "customer": "VARCHAR(128) DEFAULT ''",
        "order_date": "VARCHAR(10) DEFAULT ''",
        "due_date": "VARCHAR(10) DEFAULT ''",
        "sheet_count": "INTEGER DEFAULT 0",
        "remark": "TEXT DEFAULT ''",
        "created_by": "VARCHAR(32) DEFAULT ''",
        # 任务版本号：存量任务一律视为 v1（从未改过口径），不回填历史
        "version": "INTEGER DEFAULT 1",
        "revised_at": "DATETIME",
        "revised_by": "VARCHAR(32) DEFAULT ''",
        # 待核对口径与实报口径分开：汇总只认已确认单据，任务上也不能把未确认的算进实报
        "pending_hours": "FLOAT DEFAULT 0",
        "pending_count": "INTEGER DEFAULT 0",
    },
    # P4 升版提醒：任务引用要记住「本单确认到哪一版」。
    # 存量任务回填成当前引用版本（见 _backfill_seen_route），
    # 否则升级当天全厂任务会一起变红，提醒就没人看了。
    "task_route": {
        "seen_route_id": "INTEGER",
        "seen_at": "DATETIME",
        "seen_by": "VARCHAR(32) DEFAULT ''",
    },
    # C1 队列外置：识别任务从「进程内存里的线程池」搬到 image 表，
    # Web 多进程 + 独立 OCR 进程才能共用一条队列。
    "image": {
        "attempts": "INTEGER DEFAULT 0",
        "claimed_at": "DATETIME",
        "keeps_json": "TEXT DEFAULT ''",
    },
    # 产品结构上的「计划生产数量」：这一批做几件，与每台用量分开。
    # 存量库补列后按「父件计划量 × 本级用量」自顶向下回填一次，
    # 否则 541 条全是默认 1，×2 的部件下面的零件对不上。
    "product_node": {
        "plan_qty": "FLOAT DEFAULT 1",
        "plan_src": "VARCHAR(16) DEFAULT 'auto'",
    },
    # 账号对应的名册人员：让「登录账号」与「车间里那个人」能对上号。
    # 只做展示与追溯，权限仍以 app_user.role（系统角色）为准。
    "app_user": {
        "worker_id": "INTEGER",
    },
    # 岗位职能（逗号分隔）。空串=没配，账号按系统角色的默认职能走。
    # 内置岗位也留空：不替用户猜「计调员该不该报工」，由管理员显式勾。
    "role": {
        "duties": "TEXT DEFAULT ''",
    },
}


def _ensure_columns() -> None:
    if not DB_URL.startswith("sqlite"):
        return
    added: set[tuple[str, str]] = set()
    with engine.begin() as conn:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if not existing:
                continue
            for name, ddl in columns.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
                    added.add((table, name))
        _ensure_indexes(conn)
        _ensure_triggers(conn)
        _backfill_audit_sheet()
    # 只在**这一版刚加上这一列**时回填。每次启动都回填的后果很严重：
    # 引用被升版重指到新版之后 seen_route_id 仍停在旧版（这正是待确认的来源），
    # 每重启一次就被"自动确认"一次，提醒会凭空空消失。
    if ("task_route", "seen_route_id") in added:
        _backfill_seen_route()
    # 计划数量这一列刚加上：按结构自顶向下推一次。
    # 只在加列那一版跑，之后人改过的值（含 plan_src='manual'）不再被覆盖。
    if ("product_node", "plan_qty") in added:
        _backfill_node_plans()


def _backfill_node_plans() -> None:
    """`plan_qty` 首次补列后的回填：根按 1 台、下级按「父 × 每台用量」推。

    用递归 CTE 而不是拉进 Python：这里在启动路径上，多 worker 会并发进来，
    一条语句既快又只有一次写。
    """
    with engine.begin() as conn:
        try:
            conn.exec_driver_sql(
                "WITH RECURSIVE t(id, qty) AS ("
                "  SELECT id, CAST(COALESCE(plan_qty, 1) AS REAL) FROM product_node"
                "   WHERE parent_id IS NULL"
                "  UNION ALL"
                "  SELECT n.id, ROUND(t.qty * COALESCE(n.qty_per_unit, 1), 4)"
                "    FROM product_node n JOIN t ON n.parent_id = t.id"
                "   WHERE COALESCE(n.plan_src, 'auto') = 'auto'"
                ") "
                "UPDATE product_node "
                "   SET plan_qty = COALESCE("
                "         (SELECT t.qty FROM t WHERE t.id = product_node.id), plan_qty) "
                " WHERE COALESCE(plan_src, 'auto') = 'auto'"
            )
        except Exception as exc:  # pragma: no cover - 老 SQLite / 结构异常
            print(f"[db] 计划数量回填失败（不影响启动，可在结构页重新保存）: {exc}")


def _ensure_triggers(conn) -> None:
    """数据库级兜底：路线被删掉时，指向它的升版通知一起删。

    service 层 delete_route 已经清了一遍，但只要有别的入口（脚本、批量删、
    ORM 直删）绕过它，就会留下「最近一次升到 v7」而 v7 已经不存在这种记录。
    SQLite 的触发器是这里唯一能兜住所有写法的位置。
    """
    try:
        conn.exec_driver_sql(
            "CREATE TRIGGER IF NOT EXISTS trg_route_notice_cleanup "
            "AFTER DELETE ON process_route "
            "BEGIN "
            "  DELETE FROM route_version_notice WHERE route_id = OLD.id; "
            "END"
        )
    except Exception as exc:  # pragma: no cover - 老库缺表时才触发
        print(f"[db] 触发器 trg_route_notice_cleanup 建立失败：{exc}")


def _backfill_seen_route() -> None:
    """给存量任务引用补上「已确认版本」。

    只在 `seen_route_id IS NULL` 时补，且只补「当前引用就是该组合最新已发布版」的行：
    这类引用本来就是生效中的基准，没有人需要被提醒。指向旧版/草稿的引用故意留空，
    它们对应的正是要靠提醒暴露出来的历史欠账。
    """
    with engine.begin() as conn:
        tables = {
            row[0]
            for row in conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "task_route" not in tables or "process_route" not in tables:
            return
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(task_route)")}
        if "seen_route_id" not in cols:
            return
        conn.exec_driver_sql(
            """
            UPDATE task_route
               SET seen_route_id = route_id,
                   seen_at = CURRENT_TIMESTAMP,
                   seen_by = 'migrate'
             WHERE seen_route_id IS NULL
               AND status = 'active'
               AND origin = 'baseline'
               AND EXISTS (
                     SELECT 1 FROM process_route r
                      WHERE r.id = task_route.route_id
                        AND r.kind = 'route'
                        AND r.status = 'released'
                        AND NOT EXISTS (
                              SELECT 1 FROM process_route o
                               WHERE o.kind = 'route'
                                 AND o.product = r.product
                                 AND o.part = r.part
                                 AND o.status = 'released'
                                 AND o.version > r.version
                        )
               )
            """
        )


# create_all 只给「新建的表」建索引；存量库要单独补。
# 这些索引承载的是业务约束，不是性能优化 —— 建不上就得让人看见。
_ADDED_INDEXES: list[tuple[str, str]] = [
    (
        "uq_route_released",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_route_released "
        "ON process_route (product, part) "
        "WHERE kind <> 'task' AND status = 'released'",
    ),
    # 识别队列的领取扫描：多进程/独立 worker 每次取「最早一条 pending」，
    # 没有这个索引就得全表扫 image（历史图片会一直留着）。
    (
        "ix_image_queue",
        "CREATE INDEX IF NOT EXISTS ix_image_queue ON image (status, id)",
    ),
]


def _ensure_indexes(conn) -> None:
    for name, ddl in _ADDED_INDEXES:
        try:
            conn.exec_driver_sql(ddl)
        except Exception as exc:  # pragma: no cover - 存量数据本身违规时才会触发
            # 唯一索引建不上，说明库里已经存在两条并行基准。
            # 不能静默跳过：这会让「只能引用已发布工艺」变成一句空话。
            print(f"[db] 索引 {name} 建立失败：{exc}")


def _backfill_audit_sheet() -> None:
    """把留痕的 sheet_id 补上（老库升级用）。

    只做确定性的补：单据自身的留痕 entity_id 就是 sheet_id；明细留痕用当前还存在的
    明细行反查。明细行被「重新识别」删掉重建的那部分无法确定归属，宁可不猜。
    """
    with engine.connect() as probe:
        tables = {row[0] for row in probe.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
    if "audit_log" not in tables:
        return
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE audit_log SET sheet_id = entity_id WHERE entity = 'sheet' AND sheet_id IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE audit_log SET sheet_id = ("
            "  SELECT wi.sheet_id FROM work_item wi WHERE wi.id = audit_log.entity_id"
            ") WHERE entity = 'work_item' AND sheet_id IS NULL"
        )
        # 明细行号：行 id 会被「重新识别」整批换掉，行号才是稳定锚点。
        # 先按现存明细行反查，再用「重新识别覆盖」快照里的 _entity_id → seq 映射
        # 把已经重建掉的旧行补上，否则上一轮保留的人工值在下一轮就找不回来了。
        conn.exec_driver_sql(
            "UPDATE audit_log SET seq = ("
            "  SELECT wi.seq FROM work_item wi WHERE wi.id = audit_log.entity_id"
            ") WHERE entity = 'work_item' AND seq IS NULL"
        )
        snap_rows = conn.exec_driver_sql(
            "SELECT entity_id, old_value FROM audit_log WHERE field = 'item:overwritten'"
        ).fetchall()
        seq_by_item: dict[int, int] = {}
        for eid, raw in snap_rows:
            try:
                snap = json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(snap, dict):
                continue
            seq = snap.get("seq")
            if isinstance(seq, (int, float)) and isinstance(eid, (int, float)):
                seq_by_item[int(eid)] = int(seq)
        for eid, seq in seq_by_item.items():
            conn.exec_driver_sql(
                "UPDATE audit_log SET seq = ? WHERE entity = 'work_item' AND seq IS NULL AND entity_id = ?",
                (seq, eid),
            )
