"""给 51 道工序排定「工艺默认序」（process_dict.default_seq）。

工艺默认序 = 同一道工序在工艺路线里通常出现在第几段。它有两个用途：

1. 「从历史工时生成路线」时按此排序——否则只能按样本数排，会把
   「车」排在「焊」前面这种明显不合工艺的顺序交给人去改；
2. 覆盖率看板按价值排待建清单时，作为同一零件内各工序的默认展开次序。

**这不是硬规则**：现场可以按零件族有自己的先后（有的厂先焊后车），
所以系统管理里可以随时改，改完立刻影响后续生成。

排序依据是机加工的通用流程，本厂实际如有出入直接改这张表即可：
    备料 → 切削加工 → 钳工 → 钣金 → 焊接 → 装配 → 电气 → 调试 → 表面处理 → 包装发运

用法：
    python scripts/seed_process_seq.py --dry    # 只看会改成什么
    python scripts/seed_process_seq.py          # 只填「还没定序」的
    python scripts/seed_process_seq.py --force  # 全部覆盖（改了顺序后用）
"""
from __future__ import annotations

import argparse

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import AuditLog, ProcessDict  # noqa: E402

USER = "system:seed_process_seq"

# 工序名 → 工艺默认序。段内留 10 的间隔，方便以后往中间插一道工序。
SEQUENCE: dict[str, int] = {
    # ---- 备料 ----
    "下料": 10,
    "喷砂": 11,
    "领料排盘": 12,
    # ---- 切削加工 ----
    "车": 20,
    "铣": 21,
    "镗": 22,
    "立加": 23,
    "加工中心": 24,
    "刨": 25,
    "磨": 26,
    "精磨": 27,
    "插": 28,
    "线切割": 29,
    # ---- 钳工 ----
    "划线": 40,
    "钳": 41,
    "钻孔": 42,
    "攻丝": 43,
    "倒角": 44,
    "校直": 45,
    "抛光": 46,
    "去刺": 47,
    "酸洗": 48,
    "拉丝": 49,
    # ---- 钣金 ----
    "板钳": 50,
    "制件": 51,
    "折弯": 52,
    "卷圆": 53,
    "开角": 54,
    "包板": 55,
    # ---- 焊接 ----
    "焊": 60,
    # ---- 装配 ----
    "装配": 70,
    "总装": 71,
    "下线装配": 72,
    # ---- 电气 ----
    "配盘": 80,
    "接线": 81,
    "电工": 82,
    # ---- 调试 ----
    "试机": 90,
    "考核": 91,
    # ---- 表面处理与收尾 ----
    "打磨": 100,
    "刷漆": 101,
    "打批号": 102,
    "铭牌": 103,
    "清洗": 104,
    "整理": 105,
    # ---- 包装发运 ----
    "打包": 110,
    "发货": 111,
    "送货": 112,
    "卸车": 113,
    # ---- 特殊 / 非工艺流程 ----
    "电镀": 120,
    "表处": 121,
    "维修": 122,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只看会改成什么，不写库")
    ap.add_argument("--force", action="store_true", help="全部覆盖（含已定序的）")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    rows = db.query(ProcessDict).order_by(ProcessDict.id).all()

    changes: list[tuple[ProcessDict, int, int]] = []
    unmatched: list[str] = []
    for p in rows:
        target = SEQUENCE.get(p.name)
        if target is None:
            unmatched.append(p.name)
            continue
        if p.default_seq == target:
            continue
        if p.default_seq != 999 and not args.force:
            continue  # 已经人工定过序，不覆盖
        changes.append((p, p.default_seq, target))

    print(f"工序总数 {len(rows)}｜本次将定序 {len(changes)} 条｜未在序表内 {len(unmatched)} 条")
    for p, old, new in changes:
        print(f"  {p.name:<8} {old if old != 999 else '未定序'} → {new}")
    if unmatched:
        print("未在序表内（保持原样，建议补进 SEQUENCE）：", "、".join(unmatched))

    if args.dry:
        return 0
    if not changes:
        print("没有需要写入的改动")
        return 0

    for p, old, new in changes:
        p.default_seq = new
        db.add(
            AuditLog(
                entity="process_dict",
                entity_id=p.id,
                field="process_dict:default_seq",
                old_value=str(old),
                new_value=str(new),
                user=USER,
            )
        )
    db.commit()
    print("已写入并留痕")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
