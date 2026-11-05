"""造/清一批 UI 验收用的临时数据（产品 ZZUI，任务 ZZUI-甲/乙）。

只在界面验收时用：让浏览器点到的是自己造的订单，绝不拿真实基线和真实订单试手。

    python scripts/ui_fixture.py prep
    python scripts/ui_fixture.py clean
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ProcessRoute, ProcessRouteStep, TaskRoute, WorkTask  # noqa: E402

PRODUCT = "ZZUI产品"
PART = "件U"
PREFIX = "ZZUI"


def step(seq, name, unit, setup=0.0):
    return {
        "seq": seq,
        "process_code": name,
        "process_name": name,
        "op_type": "self",
        "category": "机加工",
        "work_center": "1#车床",
        "setup_hours": setup,
        "unit_hours": unit,
        "is_key": False,
        "inspect_required": False,
        "sop": "",
        "remark": "",
    }


def clean() -> None:
    with SessionLocal() as db:
        for t in db.scalars(select(WorkTask).where(WorkTask.task_no.like(f"{PREFIX}%"))).all():
            for tr in db.scalars(select(TaskRoute).where(TaskRoute.task_id == t.id)).all():
                route = db.get(ProcessRoute, tr.route_id)
                db.delete(tr)
                db.flush()
                if route is not None and route.kind == "task":
                    db.query(ProcessRouteStep).filter(
                        ProcessRouteStep.route_id == route.id
                    ).delete(synchronize_session=False)
                    db.delete(route)
            db.delete(t)
            db.flush()
        for r in db.scalars(select(ProcessRoute).where(ProcessRoute.product == PRODUCT)).all():
            db.query(ProcessRouteStep).filter(ProcessRouteStep.route_id == r.id).delete(
                synchronize_session=False
            )
            db.delete(r)
        db.commit()
    print("已清理 ZZUI 临时数据")


def prep() -> None:
    clean()
    client = TestClient(app)
    r = client.post(
        "/api/routes",
        json={
            "product": PRODUCT,
            "part": PART,
            "kind": "route",
            "title": "U 验收基线",
            "steps": [step(10, "车", 1.5), step(20, "铣", 2.5), step(30, "磨", 1.0)],
        },
    )
    route_id = r.json()["id"]
    client.post(f"/api/routes/{route_id}/publish")
    for name in ("甲", "乙"):
        client.post(
            "/api/tasks",
            json={
                "task_no": f"{PREFIX}-{name}",
                "title": f"U验收订单{name}",
                "product": PRODUCT,
                "customer": "验收客户",
                "qty": 20,
                "due_date": "2026-12-31",
                "routes": [
                    {"product": PRODUCT, "part": PART, "route_id": route_id, "qty": 20}
                ],
            },
        )
    print(f"已创建基线 v1（{route_id}）与两个订单")


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "prep"
    (clean if action == "clean" else prep)()
