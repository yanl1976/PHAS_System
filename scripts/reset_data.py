"""清空业务数据，保留人员 / 工序字典 / 核算期。

用途：正式投产前把联调用的演示数据清掉。

用法：
    python scripts/reset_data.py            # 只清数据库
    python scripts/reset_data.py --files    # 同时删除上传原图与处理图
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import delete  # noqa: E402

from app.config import PROCESSED_DIR, THUMB_DIR, UPLOAD_DIR  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.models import AuditLog, Batch, Image, Sheet, WorkItem  # noqa: E402


def main() -> None:
    drop_files = "--files" in sys.argv
    init_db()
    db = SessionLocal()
    try:
        for model in (AuditLog, WorkItem, Sheet, Image, Batch):
            db.execute(delete(model))
        db.commit()

        counts = {
            "work_item": db.query(WorkItem).count(),
            "sheet": db.query(Sheet).count(),
            "image": db.query(Image).count(),
            "batch": db.query(Batch).count(),
        }
        print("已清空业务数据：", counts)

        if drop_files:
            removed = 0
            for folder in (UPLOAD_DIR, PROCESSED_DIR, THUMB_DIR):
                for f in Path(folder).glob("*"):
                    if f.is_file():
                        f.unlink()
                        removed += 1
            print(f"已删除图片文件 {removed} 个")
    finally:
        db.close()


if __name__ == "__main__":
    main()
