"""闭环体检：按业务流程核对「口径是否自洽、状态是否自相矛盾、账内账外是否混算」。

与 `check_data.py` 的分工：
- `check_data.py` 管**结构**：孤儿行、悬空外键、唯一约束、物化字段有没有脏。
- 本脚本管**流程语义**：数字口径对不对、状态与字段是否互相矛盾、
  未入账的数据有没有污染入账口径、留痕能不能追到人。

只读，不写库。用法：`.venv/Scripts/python.exe scripts/audit_closed_loop.py [YM]`
退出码：0 = 无红灯；1 = 有红灯（阻断级）。
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog,
    Period,
    ProcessRoute,
    RouteVersionNotice,
    Sheet,
    TaskRoute,
    WorkItem,
    WorkTask,
)
from app.domain.aggregation import reconcile  # noqa: E402
from app.domain.rules import period_bounds  # noqa: E402

RED = "RED"
AMBER = "AMBER"
OK = "OK"

rows: list[tuple[str, str, str, str]] = []   # (level, 分组, 检查项, 说明)


def add(level: str, group: str, label: str, detail: str = "") -> None:
    rows.append((level, group, label, detail))


def main() -> int:
    ym = sys.argv[1] if len(sys.argv) > 1 else None
    db = SessionLocal()
    try:
        period = db.scalar(select(Period).where(Period.ym == ym)) if ym else db.scalars(select(Period)).first()
        if period is None:
            print("没有核算期，无法体检")
            return 1
        ym = period.ym
        start, end = period_bounds(ym)
        print(f"核算期 {ym}（{period.status}）　区间 {start} ~ {end}")
        print("=" * 74)

        sheets = db.scalars(select(Sheet)).all()
        items = db.scalars(select(WorkItem)).all()
        by_sheet: dict[int, list[WorkItem]] = defaultdict(list)
        for it in items:
            by_sheet[it.sheet_id].append(it)

        # ---------------------------------------------------------- 一、单据口径
        bad_total = []
        bad_calc = []
        for s in sheets:
            s_items = by_sheet.get(s.id, [])
            if not s_items:
                continue
            expect = round(sum(i.total_hours or 0 for i in s_items), 4)
            if abs(expect - (s.total_hours or 0)) > 0.01:
                bad_total.append(f"#{s.id} 单头 {s.total_hours} vs 明细 {expect}")
            if s.status != "verified":
                continue
            for i in s_items:
                calc = round((i.qualified_qty or 0) * (i.unit_hours or 0), 4)
                if abs(calc - (i.total_hours or 0)) > 0.01:
                    bad_calc.append(f"#{s.id}/{i.seq} 件数×单件 {calc} vs 合计 {i.total_hours}")
        add(RED if bad_total else OK, "单据口径", "单头合计 = 明细合计", "；".join(bad_total[:5]))
        add(RED if bad_calc else OK, "单据口径", "明细合计 = 合格件数 × 单件工时", "；".join(bad_calc[:5]))

        # -------------------------------------------------- 二、状态与字段自洽性
        no_stamp = [f"#{s.id}" for s in sheets if s.status == "verified" and not (s.verify_at and s.verify_user)]
        stray_stamp = [f"#{s.id}" for s in sheets if s.status != "verified" and s.verify_at]
        add(RED if no_stamp else OK, "状态自洽", "已确认单据必须有确认人与确认时间", "；".join(no_stamp[:5]))
        add(AMBER if stray_stamp else OK, "状态自洽", "未确认单据不应残留确认戳", "；".join(stray_stamp[:5]))

        verified_items = [(i, s) for s in sheets if s.status == "verified" for i in by_sheet.get(s.id, [])]
        no_worker = [f"#{s.id}/{i.seq}（{i.total_hours} h）" for i, s in verified_items if not i.worker_id]
        add(RED if no_worker else OK, "状态自洽", "已确认明细都能落到具体人（否则汇总出现「未匹配」）", "；".join(no_worker[:5]))

        no_dims = [
            f"#{s.id}/{i.seq}"
            for i, s in verified_items
            if not (i.product_name and i.part_name and (i.process_std or i.process))
        ]
        add(RED if no_dims else OK, "状态自洽", "已确认明细维度齐全（产品/件名/工序）",
            ("；".join(no_dims[:5]) + "　原件该栏空白，需车间在核对页「补录维度」补齐") if no_dims else "")

        no_date = [f"#{s.id}/{i.seq}" for i, s in verified_items if not i.finish_date]
        add(AMBER if no_date else OK, "状态自洽", "已确认明细都有完工日期", "；".join(no_date[:5]))

        # -------------------------------------------- 三、账内 / 账外（最易出错）
        unverified_items = [(i, s) for s in sheets if s.status != "verified" for i in by_sheet.get(s.id, [])]
        offline_hours = round(sum(i.total_hours or 0 for i, _ in unverified_items), 4)
        add(OK if not unverified_items else AMBER, "账内账外",
            f"未入账明细 {len(unverified_items)} 行 / {offline_hours} h",
            "已单列，不参与汇总" if unverified_items else "")

        # 关键：账外数据是否在勾稽里被当成「入账口径」判错
        rep = reconcile(db, ym)
        red_codes = {i["code"] for i in rep["issues"] if i["level"] == "error"}
        unassigned_ids = {x["item_id"] for x in rep["checks"]["unassigned"]}
        unassigned_in_verified = bool(unassigned_ids & {i.id for i, _ in verified_items})
        if "UNASSIGNED" in red_codes and not unassigned_in_verified:
            add(RED, "账内账外", "勾稽把「未入账单据」的问题算成了阻断项",
                "UNASSIGNED 全部来自未确认/驳回单据，但 level=error，导出被永久拒绝")
        else:
            add(OK, "账内账外", "勾稽的阻断项只针对已入账数据", f"error 项：{sorted(red_codes) or '无'}")
        add(OK if rep["balanced"] else RED, "账内账外", f"本月可导出（balanced={rep['balanced']}）",
            "；".join(f"{i['code']}:{i['message']}" for i in rep["issues"] if i["level"] == "error"))

        # ---------------------------------------------------------- 四、任务闭环
        tasks = db.scalars(select(WorkTask)).all()
        drift_cnt, drift_hours, drift_pending = [], [], []
        offline_by_task: dict[int, int] = defaultdict(int)
        for t in tasks:
            # 口径按服务层定义：实报 = 已确认单据；待核对单独物化，两者都不能少算
            owned = [s for s in sheets if s.task_id == t.id and s.status == "verified"]
            waiting = [s for s in sheets if s.task_id == t.id and s.status not in ("verified", "rejected")]
            if (t.sheet_count or 0) != len(owned):
                drift_cnt.append(f"{t.task_no} 记 {t.sheet_count} 实 {len(owned)}")
            real = round(sum(s.total_hours or 0 for s in owned), 4)
            if abs(real - (t.actual_hours or 0)) > 0.01:
                drift_hours.append(f"{t.task_no} 记 {t.actual_hours} 实 {real}")
            pend = round(sum(s.total_hours or 0 for s in waiting), 4)
            if (t.pending_count or 0) != len(waiting) or abs(pend - (t.pending_hours or 0)) > 0.01:
                drift_pending.append(
                    f"{t.task_no} 记 {t.pending_count} 张/{t.pending_hours}h 实 {len(waiting)} 张/{pend}h"
                )
            offline_by_task[t.id] = len(waiting)
        add(RED if drift_cnt else OK, "任务闭环", "任务物化「已确认单据数」与实际一致", "；".join(drift_cnt[:5]))
        add(RED if drift_hours else OK, "任务闭环", "任务物化「实报工时」与实际一致", "；".join(drift_hours[:5]))
        add(RED if drift_pending else OK, "任务闭环", "任务物化「待核对」与实际一致", "；".join(drift_pending[:5]))

        no_period = [t.task_no for t in tasks if not t.period_id]
        add(AMBER if no_period else OK, "任务闭环", "任务都归到核算期", "；".join(no_period[:5]))

        # 任务实报含待核对单据，月度汇总只含已确认 —— 两个数天然不同，
        # 但不能让它「悄悄不同」：谁对着任务卡和汇总表核对，必须知道差在哪。
        gap = [
            f"{t.task_no} 有 {offline_by_task[t.id]} 张待核对单"
            for t in tasks
            if offline_by_task.get(t.id)
        ]
        add(OK, "任务闭环", "实报与待核对已分列（不再混算）", "；".join(gap[:5]) if gap else "无待核对单据")

        # ---------------------------------------------------------- 五、工艺发布
        # 组合未发布 = 工艺不可引用：核对时判断不了「这道工序是不是路线里的」，
        # 任务也没法挂线。这不是数据错误，但必须是**看得见**的状态，
        # 否则会有人以为系统在替他挡，实际是没人有基准可挡。
        from app.models import WorkCombo as _WC

        combos = db.scalars(select(_WC)).all()
        pub = [c for c in combos if c.status == "released"]
        draft_ready = [
            c for c in combos if c.status == "draft" and c.demoted_from == "released"
        ]
        add(
            OK if pub else AMBER,
            "工艺发布",
            f"已发布工艺 {len(pub)} / {len(combos)} 条组合",
            (
                "有工艺在用"
                if pub
                else (
                    f"当前是 0 条：核对时不会判断「工序是否在路线内」，任务也挂不上工艺。"
                    f"其中 {len(draft_ready)} 条是迁移降级的，到「工时组合」页批量确认发布即可恢复"
                )
            ),
        )

        # ---------------------------------------------------------- 六、工艺卡
        # 工艺卡是「发布」这一步的下游产物：发布 0 条就一定印不出卡。
        # 单列一条，是为了让「为什么车间拿不到卡」有确切答案，
        # 而不是让人跑去 /cards 页面看一个空列表猜原因。
        from app.services import process_card as _PC

        cards = _PC.products(db)
        printable = sum(1 for x in cards if x["released_with_route"])
        card_total = sum(x["released_with_route"] for x in cards)
        add(
            OK if card_total else AMBER,
            "工艺卡",
            f"可打印 {card_total} 张卡 / {printable} 个产品（版式=加工路线单）",
            (
                "已发布的组合都能出卡，卡上的版式就是车间实印的加工路线单"
                if card_total
                else "还没有已发布的组合，车间没有可下发的工艺卡；发布后自动出现在「工艺卡」页"
            ),
        )

        # 挂了件名、工艺却没发布的任务：这单在车间**印不出卡**。
        # 比「一条路线没建」更容易被漏掉 —— 任务页上看着正常，去打印才发现少几张。
        gaps = [t for t in _PC.tasks(db) if t["draft"]]
        add(
            AMBER if gaps else OK,
            "任务工艺缺口",
            f"{len(gaps)} 个任务的件名还没有已发布工艺",
            (
                "；".join(
                    f"{t['task_no']}（{t['draft']}/{t['routes']} 件）" for t in gaps[:4]
                )
                + " —— 这些件名印不出工艺卡，工人在车间只能凭经验做"
                if gaps
                else "每个任务挂的件名都有已发布工艺，都能印出卡"
            ),
        )

        # ---------------------------------------------------------- 七、路线引用
        trs = db.scalars(select(TaskRoute).where(TaskRoute.status == "active")).all()
        dup: dict[tuple[int, int], int] = defaultdict(int)
        for tr in trs:
            dup[(tr.task_id, tr.route_id)] += 1
        dup_bad = [f"任务{tid}/路线{rid}×{n}" for (tid, rid), n in dup.items() if n > 1]
        add(RED if dup_bad else OK, "路线引用", "同一任务不重复挂同一条路线", "；".join(dup_bad[:5]))

        routes = {r.id: r for r in db.scalars(select(ProcessRoute)).all()}
        mismatch = []
        for tr in trs:
            r = routes.get(tr.route_id)
            if r is None:
                mismatch.append(f"引用{tr.route_id} 路线不存在")
                continue
            if tr.product and r.product and tr.product != r.product:
                mismatch.append(f"任务{tr.task_id} 产品 {tr.product} vs 路线 {r.product}")
            if tr.part and r.part and tr.part != r.part:
                mismatch.append(f"任务{tr.task_id} 件名 {tr.part} vs 路线 {r.part}")
        add(RED if mismatch else OK, "路线引用", "引用登记的产品/件名与路线一致", "；".join(mismatch[:5]))

        stale_copy = []
        for tr in trs:
            r = routes.get(tr.route_id)
            if r is None or r.kind != "task" or not r.snapshot_of_id:
                continue
            base = routes.get(r.snapshot_of_id)
            if base is not None and (base.version or 1) > (r.snapshot_version or 1):
                stale_copy.append(f"任务{r.task_id} 副本基于 v{r.snapshot_version}，基线已 v{base.version}")
        add(AMBER if stale_copy else OK, "路线引用", "任务副本没有落后于基线", "；".join(stale_copy[:5]))

        notices = db.scalars(select(RouteVersionNotice)).all()
        acked = {tr.id for tr in trs if tr.seen_route_id}
        add(OK, "路线引用", f"升版事件 {len(notices)} 条，引用已确认到具体版本 {len(acked)} 条",
            "有升版必须有确认动作" if notices else "本月无升版")

        # ---------------------------------------------------------- 八、留痕可追
        audits = db.scalars(select(AuditLog)).all()
        audited = {(a.entity, a.entity_id, a.field) for a in audits}
        no_trace = [f"#{s.id}" for s in sheets if s.status == "verified" and ("sheet", s.id, "status") not in audited]
        add(AMBER if no_trace else OK, "留痕", "已确认单据都有状态变更留痕", "；".join(no_trace[:5]))

        # 真实风险：留痕里查不到操作人（说明还被请求体或空串糊弄着）
        move_audits = [a for a in audits if a.entity == "sheet" and a.field == "task_id"]
        no_user_move = [f"#{a.entity_id}" for a in move_audits if not (a.user or "").strip()]
        add(RED if no_user_move else OK, "留痕", "归属变更都记了操作人", "；".join(no_user_move[:5]))

        # 归集本身有两种：确认时自动归集（有 status 留痕可追人）、手工改挂（有 task_id 留痕）。
        # 两者都查不到才是真断链。
        untraced = []
        for s in sheets:
            if s.task_id is None:
                continue
            if not (("sheet", s.id, "task_id") in audited or ("sheet", s.id, "status") in audited):
                untraced.append(f"#{s.id}")
        add(AMBER if untraced else OK, "留痕", "单据归属可追到人（改挂留痕或确认留痕）",
            "；".join(untraced[:5]))

        # ---------------------------------------------------------- 九、汇总口径
        from app.domain.aggregation import monthly_summary  # 局部导入，避免循环

        ms = monthly_summary(db, ym)
        total = ms["totals"]["hours"]
        add(OK if abs(total - rep["levels"]["detail_sum"]) <= 0.01 else RED, "汇总口径",
            f"汇总 {total} h = 勾稽明细合计 {rep['levels']['detail_sum']} h")
        preview = monthly_summary(db, ym, include_unverified=True)
        add(OK, "汇总口径", f"含未确认预览 {preview['totals']['hours']} h（差 {round(preview['totals']['hours'] - total, 4)} h）")

        cross = [f"#{i.sheet_id}/{i.seq} {i.finish_date}" for i, _ in verified_items
                 if i.finish_date and not (start <= i.finish_date <= end)]
        add(AMBER if cross else OK, "汇总口径", "已确认明细的完工日期都在本期", "；".join(cross[:5]))

        if period.status == "locked":
            add(AMBER if [s for s in sheets if s.status != "verified"] else OK, "汇总口径",
                "锁定期间内仍存在未确认单据",
                f"{len([s for s in sheets if s.status != 'verified'])} 张")
    finally:
        db.close()

    # ---------------------------------------------------------------- 输出
    reds = [r for r in rows if r[0] == RED]
    ambers = [r for r in rows if r[0] == AMBER]
    group = ""
    for level, g, label, detail in rows:
        if g != group:
            print(f"\n[{g}]")
            group = g
        mark = {RED: "红灯", AMBER: "提示", OK: "通过"}[level]
        print(f"  {mark}  {label}" + (f"　—— {detail}" if detail else ""))
    print("\n" + "=" * 74)
    print(f"红灯 {len(reds)} 项，提示 {len(ambers)} 项，共查 {len(rows)} 项")
    return 1 if reds else 0


if __name__ == "__main__":
    raise SystemExit(main())
