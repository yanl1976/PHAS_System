"""单件工时复查的三种分支自测（不走 OCR，用打桩的识别结果）。

为什么要单独测：真实单据里几乎不会同时满足「整页读错 + 候选读对 + 合计能勾稽上」，
自动采用分支在样本上永远不触发，也就永远发现不了它写错了。

用法：python scripts/test_recheck.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402

from app.ocr import cellprobe  # noqa: E402
from app.ocr.cellprobe import shape_kept  # noqa: E402
from app.ocr.extract import _recheck_unit_hours  # noqa: E402


def run(items: list[dict], rets: list[str]) -> list[dict]:
    """把 recheck_hours_cell 打桩成按顺序返回 rets 里的文本。"""
    seq = {"i": 0}

    def fake(gray, bbox, hint=""):  # noqa: ANN001, ARG001
        if seq["i"] >= len(rets):
            return None
        text = rets[seq["i"]]
        seq["i"] += 1
        return (text, 0.9)

    original = cellprobe.recheck_hours_cell
    cellprobe.recheck_hours_cell = fake
    try:
        _recheck_unit_hours(np.zeros((10, 10), dtype=np.uint8), items)
    finally:
        cellprobe.recheck_hours_cell = original
    return items


def item(unit, declared=None, qty=None, raw="", bbox=(0, 0, 50, 20)) -> dict:  # noqa: ANN001
    return {
        "unit_hours": unit,
        "declared_total": declared,
        "qualified_qty": qty,
        "conf": {"unit_hours_raw": raw},
        "bbox": {"unit_hours": list(bbox)},
    }


CASES = [
    # 名称, items, 桩返回, 断言函数
    ("一致 → 不记录",
     [item(0.75, 6.0, 8, "0:45:00")], ["0:45:00"],
     lambda it: "unit_hours_recheck" not in it[0]["conf"]),
    ("整页读错 + 合计能勾稽 → 自动采用候选",
     [item(0.8, 6.0, 8, "0:48:00")], ["0:45:00"],
     lambda it: abs(it[0]["unit_hours"] - 0.75) < 1e-6
     and it[0]["conf"]["unit_hours_recheck"]["used"] is True),
    ("整页读错但合计对不上 → 只记分歧，不改值",
     [item(0.8, 2.0, 1, "0:48:00")], ["0:45:00"],
     lambda it: abs(it[0]["unit_hours"] - 0.8) < 1e-6
     and it[0]["conf"]["unit_hours_recheck"]["used"] is False),
    ("整页没读到单件工时 → 补检采用",
     [item(None, None, 1, "")], ["0:45:00"],
     lambda it: abs(it[0]["unit_hours"] - 0.75) < 1e-6
     and it[0]["conf"]["unit_hours_recheck"]["used"] is True),
    ("候选解析不出工时 → 原样保留",
     [item(0.8, 2.0, 1, "0:48:00")], ["定生"],
     lambda it: "unit_hours_recheck" not in it[0]["conf"]),
]


def main() -> int:
    failed = 0
    if not cellprobe.recheck_enabled():
        print("WT_CELL_RECHECK 已关闭，跳过仲裁分支用例（形态校验仍可测）\n")
    else:
        for name, items, rets, check in CASES:
            out = run(items, rets)
            ok = check(out)
            print(f"{'PASS' if ok else 'FAIL'}  {name}  → {out[0]['unit_hours']} / {out[0]['conf'].get('unit_hours_recheck')}")
            if not ok:
                failed += 1

    # 形态校验：去线会把「2.5」的点和「0:45:00」的冒号抹掉，这类残缺候选必须挡掉
    shape_cases = [
        ("2.5", "25", False),
        ("2.5", "2.5", True),
        ("0:45:00", "04500", False),
        ("0:45:00", "0:45:00", True),
        ("25H", "25H", True),
    ]
    for hint, cand, expect in shape_cases:
        got = shape_kept(cand, hint)
        ok = got == expect
        print(f"{'PASS' if ok else 'FAIL'}  形态校验 hint={hint!r} cand={cand!r} → {got}")
        if not ok:
            failed += 1

    print(f"\n{'全部通过' if not failed else f'{failed} 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
