"""数据体检：P1/P2 之后随时可跑，确认「产品 → 任务 → 工艺」这条链没断。

不修改任何数据，只出报告。任何一项 FAIL 都说明归属断了，
界面上的「按产品统管」就是假的。

用法：python scripts/check_data.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Period,
    ProcessRoute,
    ProcessRouteStep,
    ProductDict,
    RouteVersionNotice,
    Sheet,
    StandardHour,
    WorkItem,
    TaskRevision,
    TaskRoute,
    WorkTask,
)
from app.services import tasks as task_service  # noqa: E402

FAIL = 0


def line(label: str, value, ok: bool | None = None) -> None:
    global FAIL
    mark = "" if ok is None else ("  OK" if ok else "  FAIL")
    if ok is False:
        FAIL += 1
    print(f"  {label:<34}{str(value):>10}{mark}")


def main() -> int:
    init_db()
    db = SessionLocal()
    try:
        c = lambda m: db.scalar(select(func.count()).select_from(m))

        print("\n[规模]")
        line("产品主数据", c(ProductDict))
        line("单据 / 已确认", f"{c(Sheet)} / {db.scalar(select(func.count()).select_from(Sheet).where(Sheet.status == 'verified'))}")
        line("工时明细行", c(WorkItem))
        line("标准工时", c(StandardHour))
        line("基线路线", db.scalar(select(func.count()).select_from(ProcessRoute).where(ProcessRoute.kind == "route")))
        line("任务工艺", db.scalar(select(func.count()).select_from(ProcessRoute).where(ProcessRoute.kind == "task")))
        line("产品字典", c(ProductDict))

        print("\n[归属完整性]")
        std_total = c(StandardHour)
        std_ok = db.scalar(select(func.count()).select_from(StandardHour).where(StandardHour.product_id.is_not(None)))
        line("标准工时挂上产品", f"{std_ok}/{std_total}")

        base_total = db.scalar(select(func.count()).select_from(ProcessRoute).where(ProcessRoute.kind == "route"))
        base_ok = db.scalar(
            select(func.count()).select_from(ProcessRoute).where(
                ProcessRoute.kind == "route", ProcessRoute.product_id.is_not(None)
            )
        )
        line("基线挂上产品", f"{base_ok}/{base_total}")

        # 任务 = 订单，一张单据挂到一个任务上（多对一），不再是一单一个任务
        valid_sheets = db.scalar(select(func.count()).select_from(Sheet).where(Sheet.status != "rejected"))
        tasks = c(WorkTask)
        linked = db.scalar(
            select(func.count()).select_from(Sheet).where(
                Sheet.status != "rejected", Sheet.task_id.is_not(None)
            )
        )
        line("有效单据 / 已归任务", f"{valid_sheets} / {linked}", valid_sheets == linked)
        line("任务数（一个订单一个）", tasks, tasks <= valid_sheets)

        bad_link = db.scalar(
            select(func.count()).select_from(Sheet).where(
                Sheet.task_id.is_not(None), ~Sheet.task_id.in_(select(WorkTask.id))
            )
        )
        line("单据挂到不存在的任务", bad_link, bad_link == 0)

        # 归集是自动的（产品+批次+期间），所以这三类偏差一定会有：
        # 有偏差不可怕，可怕的是没有入口改、也没有地方看见。
        cross_period = db.scalar(
            select(func.count())
            .select_from(Sheet)
            .join(WorkTask, WorkTask.id == Sheet.task_id)
            .where(
                Sheet.status != "rejected",
                Sheet.period_id != WorkTask.period_id,
                WorkTask.period_id.is_not(None),
            )
        )
        line("单据与任务跨期归属", cross_period, cross_period == 0)
        to_closed = db.scalar(
            select(func.count())
            .select_from(Sheet)
            .join(WorkTask, WorkTask.id == Sheet.task_id)
            .where(
                Sheet.status != "rejected",
                WorkTask.status.in_(("closed", "cancelled")),
            )
        )
        line("单据挂在已结案/取消任务上", to_closed, to_closed == 0)
        orphan = db.scalar(
            select(func.count())
            .select_from(Sheet)
            .where(Sheet.status == "verified", Sheet.task_id.is_(None))
        )
        line("已确认但未归集单据", orphan, orphan == 0)

        print("\n[任务工艺]")
        tr_total = c(TaskRoute)
        active = db.scalar(
            select(func.count()).select_from(TaskRoute).where(TaskRoute.status == "active")
        )
        line("任务路线 / 有效", f"{tr_total} / {active}")
        with_route = db.scalar(
            select(func.count(func.distinct(TaskRoute.task_id))).where(TaskRoute.status == "active")
        )
        line("已挂路线的任务", f"{with_route}/{tasks}")

        # 引用型路线必须真的指向一条**已发布**基线；副本必须带来源，
        # 否则说不清从哪版分出来的
        bad_ref = db.scalar(
            select(func.count()).select_from(TaskRoute).where(
                TaskRoute.status == "active",
                TaskRoute.origin == "baseline",
                ~TaskRoute.route_id.in_(
                    select(ProcessRoute.id).where(
                        ProcessRoute.kind == "route", ProcessRoute.status == "released"
                    )
                ),
            )
        )
        line("引用指向未发布/非基线", bad_ref, bad_ref == 0)

        multi_released = db.execute(
            select(ProcessRoute.product, ProcessRoute.part, func.count())
            .where(ProcessRoute.kind == "route", ProcessRoute.status == "released")
            .group_by(ProcessRoute.product, ProcessRoute.part)
            .having(func.count() > 1)
        ).all()
        line("同组合多条已发布基线", len(multi_released), not multi_released)

        empty_released = 0
        mergeable = 0
        for r in db.scalars(
            select(ProcessRoute).where(
                ProcessRoute.kind == "route", ProcessRoute.status == "released"
            )
        ).all():
            if not r.steps:
                empty_released += 1
        line("已发布但没有工序", empty_released, empty_released == 0)

        # 副本与现行基线逐工序一致：说明这一单并没有特殊工艺，合并回引用即可
        for cp in db.scalars(select(ProcessRoute).where(ProcessRoute.kind == "task")).all():
            base = db.scalars(
                select(ProcessRoute).where(
                    ProcessRoute.kind == "route",
                    ProcessRoute.product == cp.product,
                    ProcessRoute.part == cp.part,
                    ProcessRoute.status == "released",
                )
            ).first()
            if base is None:
                continue
            sig = lambda r: sorted(  # noqa: E731
                (s.seq, s.process_code, s.process_name, round(s.unit_hours, 4))
                for s in r.steps
            )
            if sig(base) == sig(cp):
                mergeable += 1
        line("与基线一致的可合并副本", mergeable)
        no_snap = db.scalar(
            select(func.count()).select_from(ProcessRoute).where(
                ProcessRoute.kind == "task", ProcessRoute.snapshot_of_id.is_(None)
            )
        )
        line("副本缺来源基线", no_snap, no_snap == 0)
        stale = db.scalar(
            select(func.count()).select_from(ProcessRoute).where(
                ProcessRoute.kind == "task", ProcessRoute.task_id.is_(None)
            )
        )
        line("副本没挂任务", stale, stale == 0)
        orphan_copy = [
            r.id
            for r in db.scalars(select(ProcessRoute).where(ProcessRoute.kind == "task")).all()
            if db.query(TaskRoute).filter(TaskRoute.route_id == r.id).count() == 0
        ]
        line("没有任务引用的副本", len(orphan_copy), not orphan_copy)

        print("\n[升版提醒]")
        # 提醒本身不是问题，"提醒挂着没人处理"才是：
        # 引用指向的版本与人确认过的版本不一致，就说明这张单到底按哪版干还没定。
        alerts = task_service.version_alerts(db)
        by_reason: dict[str, int] = {}
        for a in alerts:
            by_reason[a["reason"]] = by_reason.get(a["reason"], 0) + 1
        line("待确认升版的引用", len(alerts))
        for reason, cnt in sorted(by_reason.items(), key=lambda x: -x[1]):
            line(f"  · {reason}", cnt)
        notices = db.scalar(select(func.count()).select_from(RouteVersionNotice))
        line("升版事件留痕", notices)
        orphan_notice = db.scalar(
            select(func.count())
            .select_from(RouteVersionNotice)
            .where(~RouteVersionNotice.route_id.in_(select(ProcessRoute.id)))
        )
        line("无主升版通知（目标版本已删）", orphan_notice, orphan_notice == 0)

        print("\n[任务版本]")
        # 版本号是「口径改过几次」的账。账本身错位，追溯就没意义：
        # ① 版本号缺失/非法；② 修订记录指向已不存在的任务；
        # ③ 任务的 version 与它最新一条修订记录的版本对不上（漏写或写重）。
        bad_ver = db.scalar(
            select(func.count())
            .select_from(WorkTask)
            .where((WorkTask.version.is_(None)) | (WorkTask.version < 1))
        )
        line("任务版本号缺失/非法", bad_ver, bad_ver == 0)
        orphan_rev = db.scalar(
            select(func.count())
            .select_from(TaskRevision)
            .where(~TaskRevision.task_id.in_(select(WorkTask.id)))
        )
        line("无主修订记录（任务已删）", orphan_rev, orphan_rev == 0)
        maxrev = (
            select(TaskRevision.task_id, func.max(TaskRevision.version).label("v"))
            .group_by(TaskRevision.task_id)
            .subquery()
        )
        mism = db.scalar(
            select(func.count())
            .select_from(WorkTask)
            .join(maxrev, maxrev.c.task_id == WorkTask.id)
            .where(maxrev.c.v != WorkTask.version)
        )
        line("版本号与最新修订记录不一致", mism, mism == 0)
        line(
            "改过口径的任务",
            db.scalar(
                select(func.count()).select_from(WorkTask).where(WorkTask.version > 1)
            ),
        )

        print("\n[孤立数据]")
        orphan_steps = db.scalar(
            select(func.count()).select_from(ProcessRouteStep).where(
                ~ProcessRouteStep.route_id.in_(select(ProcessRoute.id))
            )
        )
        line("孤立工序行", orphan_steps, orphan_steps == 0)
        null_period = db.scalar(select(func.count()).select_from(WorkTask).where(WorkTask.period_id.is_(None)))
        line("任务缺核算期", null_period)
        line("核算期", c(Period))

        print("\n[定额溯源]")
        traceable = db.scalar(
            select(func.count()).select_from(StandardHour).where(StandardHour.origin == "item")
        )
        line("能追到工时明细的定额", f"{traceable}/{std_total}")

        print("\n" + "=" * 52)
        if FAIL:
            print(f"有 {FAIL} 项检查未通过")
            return 1
        print("体检通过")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
