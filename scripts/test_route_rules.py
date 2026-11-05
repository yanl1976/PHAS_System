"""S4 路线校验规则的单元测试 + 零回归验证。

跑法：.venv/Scripts/python.exe scripts/test_route_rules.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domain.rules import check_route, route_step_lookup, validate_sheet  # noqa: E402

FAILED: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


ROUTE = {
    ("产品A", "件名A"): [
        {"process_code": "CHE", "process_name": "车", "unit_hours": 1.5, "setup_hours": 0.0},
        {"process_code": "XI", "process_name": "铣", "unit_hours": 0.8333, "setup_hours": 0.0},
    ]
}


def item(**kw):
    base = {
        "product_name": "产品A",
        "part_name": "件名A",
        "process": "车",
        "process_std": "车",
        "process_code": "CHE",
        "unit_hours": 1.5,
        "qualified_qty": 1,
    }
    base.update(kw)
    return base


def main() -> int:
    # ---------------------------------------------------- 1. 无路线时静默
    print("\n[1] 无路线 → 静默")
    check(check_route(item(), {}) == [], "routes 为空不报任何告警")
    check(check_route(item(), None) == [], "routes 为 None 不报任何告警")
    check(
        check_route(item(), {("别的产品", "别的件名"): ROUTE[("产品A", "件名A")]}) == [],
        "该组合没路线不报告警",
    )

    # ---------------------------------------------------- 2. 工序在路线内
    print("\n[2] 工序在路线内")
    check(check_route(item(), ROUTE) == [], "在路线内的工序不报")
    check(
        check_route(item(process="铣", process_std="铣", process_code="XI", unit_hours=0.8333), ROUTE) == [],
        "第二道工序也在路线内",
    )

    # ---------------------------------------------------- 3. 工序不在路线内
    print("\n[3] 工序不在路线内")
    flags = check_route(item(process="焊", process_std="焊", process_code="HAN"), ROUTE)
    codes = [f["code"] for f in flags]
    check("ROUTE_STEP_EXTRA" in codes, f"报 ROUTE_STEP_EXTRA（实际 {codes}）")
    if flags:
        h = flags[0]["hint"]
        check("焊" in h and "车" in h, f"提示里带出实际工序与路线工序：{h}")

    # 工序不在路线内时，不再报工时偏离（没意义）
    flags2 = check_route(
        item(process="焊", process_std="焊", process_code="HAN", unit_hours=99.0), ROUTE
    )
    check(len(flags2) == 1, "工序不在路线内时只报 1 条，不再叠加工时告警")

    # ---------------------------------------------------- 4. 工时偏离定额
    print("\n[4] 工时偏离路线定额")
    flags = check_route(item(unit_hours=3.0), ROUTE)  # 定额 1.5，+100%
    codes = [f["code"] for f in flags]
    check("ROUTE_HOURS_DEVIATE" in codes, f"报 ROUTE_HOURS_DEVIATE（实际 {codes}）")
    if flags:
        h = flags[0]["hint"]
        check("1.5" in h and "3" in h, f"提示带出定额与实际值：{h}")

    # 刚好在 ±50% 内不报
    check(check_route(item(unit_hours=2.2), ROUTE) == [], "偏差 +47% 不报")
    check(check_route(item(unit_hours=2.26), ROUTE) != [], "偏差 +51% 报")
    # 偏低的反向
    check(check_route(item(unit_hours=0.8), ROUTE) == [], "偏差 -47% 不报")
    check(check_route(item(unit_hours=0.74), ROUTE) != [], "偏差 -51% 报")
    # 边界：恰好 ±50% 算超差（阈值是「超出多少才算异常」，用 <= 会漏掉边界值）
    check(check_route(item(unit_hours=2.25), ROUTE) != [], "偏差恰好 +50% 报")
    check(check_route(item(unit_hours=0.75), ROUTE) != [], "偏差恰好 -50% 报")
    check(check_route(item(unit_hours=2.24), ROUTE) == [], "偏差 +49% 不报")

    # 定额为 0 时不报（不拿 0 当分母）
    zero_route = {("产品A", "件名A"): [{"process_code": "CHE", "process_name": "车", "unit_hours": 0.0}]}
    check(check_route(item(), zero_route) == [], "定额为 0 不报（不拿 0 当分母）")

    # 单件工时为 0/空时不报
    check(check_route(item(unit_hours=0), ROUTE) == [], "单件工时为 0 不报")
    check(check_route(item(unit_hours=None), ROUTE) == [], "单件工时为空不报")

    # ---------------------------------------------------- 5. 别名匹配
    print("\n[5] 别名匹配")
    # 明细写「包板折弯」（字典别名），路线里是 code ZHEWAN / 名字「折弯」
    alias_route = {
        ("产品B", "件名B"): [{"process_code": "ZHEWAN", "process_name": "折弯", "unit_hours": 2.0}]
    }
    check(
        check_route(
            item(product_name="产品B", part_name="件名B", process="包板折弯",
                 process_std="折弯", process_code="ZHEWAN", unit_hours=2.0),
            alias_route,
        )
        == [],
        "按 code 匹配到别名工序",
    )
    check(
        check_route(
            item(product_name="产品B", part_name="件名B", process="包板折弯",
                 process_std="折弯", process_code="", unit_hours=2.0),
            alias_route,
        )
        == [],
        "按标准名匹配到别名工序",
    )
    check(
        check_route(
            item(product_name="产品B", part_name="件名B", process="折弯",
                 process_std="", process_code="", unit_hours=2.0),
            alias_route,
        )
        == [],
        "按显示名匹配到别名工序",
    )

    # ---------------------------------------------------- 6. 零回归
    print("\n[6] 零回归：不传 routes 时行为不变")
    it = item(finish_date="2026-09-10")
    a, ca = validate_sheet([dict(it)], "2026-09")
    b, cb = validate_sheet([dict(it)], "2026-09", routes=None)
    check(ca == cb, f"告警码集合一致（{ca} vs {cb}）")
    check(
        [f["code"] for f in a[0]["flags"]] == [f["code"] for f in b[0]["flags"]],
        "明细告警一致",
    )
    # 传空 routes 也必须一致
    c, cc = validate_sheet([dict(it)], "2026-09", routes={})
    check(ca == cc, "传空 routes 告警码不变")

    # ---------------------------------------------------- 7. lookup 工具
    print("\n[7] route_step_lookup")
    check(route_step_lookup(item(), ROUTE[("产品A", "件名A")]) is not None, "找到工序行")
    check(
        route_step_lookup(item(process_code="ZZZ", process_std="ZZZ", process="ZZZ"),
                          ROUTE[("产品A", "件名A")]) is None,
        "找不到时返回 None",
    )
    check(route_step_lookup(item(), []) is None, "空路线返回 None")

    print("\n" + "=" * 56)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
