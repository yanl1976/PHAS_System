"""重建工艺路线两张表（一次性迁移）。

原因：process_route_step.route_id 原来没带 ondelete="CASCADE"。
关系上的 cascade="all, delete-orphan" 只在 ORM 层生效，
db.query(...).delete() 这类批量删除绕过它 → 删父行撞外键。
SQLite 改外键必须重建表，所以走一次 DROP + init_db。

表内只有测试算例数据（测试产品/算例产品），无真实业务数据，可安全重建。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.db import engine, init_db  # noqa: E402

TABLES = ("process_route_step", "process_route")


def main() -> int:
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    with engine.begin() as conn:
        for t in TABLES:
            if t in existing:
                conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
                print(f"dropped {t}")
    init_db()
    insp = inspect(engine)
    for t in TABLES:
        print(t, "->", t in insp.get_table_names())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
