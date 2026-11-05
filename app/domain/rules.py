"""校验规则与告警码。

告警分两级：
- block：必须人工处理才能确认（数据不可用或明显误读）
- warn：可确认但需留痕（数据可疑，允许人工判断后放行）
"""
from __future__ import annotations

import datetime as dt

from ..config import (
    CONF_MID,
    HOURS_PER_DAY,
    REWORK_FACTOR,
    SCRAP_FACTOR,
    UNIT_HOURS_LIMIT,
    UNIT_HOURS_WARN,
)

FLAG_META: dict[str, dict] = {
    "QTY_MISSING": {"level": "warn", "label": "合格件数缺失", "hint": "已填单件工时但未填件数，合计将计为 0"},
    "UNIT_HOURS_ABNORMAL": {
        "level": "block",
        "label": "单件工时异常",
        "hint": f"单件工时超出单人单月可用工时（约 {UNIT_HOURS_LIMIT:g} 小时）",
    },
    "UNIT_HOURS_LONG": {
        "level": "warn",
        "label": "跨天工时",
        "hint": f"单件工时超过 {UNIT_HOURS_WARN:g} 小时（1 个工作日），确认无误即可通过",
    },
    "UNIT_HOURS_MISSING": {"level": "warn", "label": "单件工时缺失", "hint": "未识别到单件工时"},
    "UNIT_HOURS_DEVIATE": {
        "level": "warn",
        "label": "与标准工时偏差大",
        "hint": "单件工时与历史标准值相差过大，可能是误读或临时改价，请核对",
    },
    "UNIT_HOURS_DISAGREE": {
        "level": "warn",
        "label": "工时识别分歧",
        "hint": "整页识别与单格复查读数不同，请对照原图确认",
    },
    "UNKNOWN_PART": {
        "level": "warn",
        "label": "件名未入档",
        "hint": "件名不在件名字典内，建议到「系统管理」补充",
    },
    "TOTAL_MISMATCH": {"level": "warn", "label": "合计与手写不符", "hint": "手写合计与「件数×单件」不一致"},
    "DATE_OUT_OF_PERIOD": {"level": "warn", "label": "日期跨期", "hint": "完成时间不在本核算期内"},
    "DATE_MISSING": {"level": "warn", "label": "日期缺失", "hint": "未识别到完成时间"},
    "UNKNOWN_WORKER": {"level": "block", "label": "人员未匹配", "hint": "无法在名册中匹配工作者"},
    "UNKNOWN_PROCESS": {"level": "warn", "label": "工序未入典", "hint": "工序不在字典内，将按原文汇总"},
    "MISSING_PRODUCT": {
        "level": "warn",
        "label": "产品缺失",
        "hint": "这行没有产品，汇总里会落到「未填」产品，成本/工艺分析查不到它",
    },
    "MISSING_PART": {
        "level": "warn",
        "label": "件名缺失",
        "hint": "这行没有件名，无法归到具体零件，也无法与工艺定额对照",
    },
    "MISSING_PROCESS": {
        "level": "block",
        "label": "工序缺失",
        "hint": "这行没有工序：工时算得出来，但归不到工序，汇总会多一列「未填」，必须补填后再确认",
    },
    "UNKNOWN_PRODUCT": {
        "level": "warn",
        "label": "产品未入档",
        "hint": "产品名称/代号不在产品字典内，建议到「系统管理」补充",
    },
    "UNKNOWN_MATERIAL": {
        "level": "warn",
        "label": "材料未入档",
        "hint": "材料不在材料字典内，建议到「系统管理」补充",
    },
    "UNKNOWN_INSPECTOR": {
        "level": "warn",
        "label": "检验员未匹配",
        "hint": "检验员不在人员名册内，将按原文记录",
    },
    "UNKNOWN_HANDOVER": {
        "level": "warn",
        "label": "接交人未匹配",
        "hint": "接交人不在人员名册内，将按原文记录",
    },
    "LOW_CONF": {"level": "warn", "label": "低置信度", "hint": "存在识别置信度低于阈值的字段"},
    "DUP_SUSPECT": {"level": "warn", "label": "疑似重复", "hint": "与已有单据存在高度相似明细"},
    "NO_ITEM": {"level": "block", "label": "无明细行", "hint": "未识别出任何工时明细"},
    "ROUTE_STEP_EXTRA": {
        "level": "warn",
        "label": "工序不在路线内",
        "hint": "工艺路线中没有这道工序，可能是新工艺，也可能识别有误",
    },
    "ROUTE_HOURS_DEVIATE": {
        "level": "warn",
        "label": "工时偏离路线定额",
        "hint": "单件工时与工艺路线定额相差过大，请确认是否改制",
    },
}

# 字段中文名：告警要能指到「去改哪一格」
FIELD_CN: dict[str, str] = {
    "unit_hours": "单件工时",
    "qualified_qty": "合格件数",
    "process": "工序",
    "worker_raw": "工作者",
    "worker": "工作者",
    "finish_date": "完成日期",
    "declared_total": "手写合计",
    "product_name": "产品名称",
    "batch_no": "批次",
    "part_name": "件名",
    "inspector": "检验员",
    "handover": "接交人",
    "material": "材料",
}


def flag(code: str, field: str = "", hint: str | None = None) -> dict:
    """构造一条告警。hint 传入时覆盖默认文案，用于带上识别到的实际值。"""
    meta = FLAG_META.get(code, {"level": "warn", "label": code, "hint": ""})
    return {
        "code": code,
        "field": field,
        "level": meta["level"],
        "label": meta["label"],
        "hint": hint or meta["hint"],
    }


def compute_total_hours(
    qualified_qty: float | None,
    unit_hours: float | None,
    rework_qty: float | None = None,
    scrap_qty: float | None = None,
) -> float:
    """合计工时 = 合格件数 x 单件工时 (+ 返修/废品折算)。

    返修、废品系数默认 0（不计工时），可通过环境变量调整。
    """
    if not unit_hours or unit_hours <= 0:
        return 0.0
    total = (qualified_qty or 0) * unit_hours
    total += (rework_qty or 0) * unit_hours * REWORK_FACTOR
    total += (scrap_qty or 0) * unit_hours * SCRAP_FACTOR
    return round(total, 4)


def validate_item(item: dict, period_ym: str, has_date_col: bool = True) -> list[dict]:
    flags: list[dict] = []

    unit = item.get("unit_hours")
    qty = item.get("qualified_qty")

    if unit is None:
        flags.append(flag("UNIT_HOURS_MISSING", "unit_hours"))
    elif unit <= 0:
        flags.append(
            flag("UNIT_HOURS_ABNORMAL", "unit_hours", f"单件工时识别为 {unit:g}，不是有效数值")
        )
    elif unit > UNIT_HOURS_LIMIT:
        flags.append(
            flag(
                "UNIT_HOURS_ABNORMAL",
                "unit_hours",
                f"单件工时 {unit:g} 小时，超出单人单月可用工时（约 {UNIT_HOURS_LIMIT:g} 小时），"
                f"疑似小数点漏识或多认一位数字",
            )
        )
    elif unit > UNIT_HOURS_WARN:
        flags.append(
            flag(
                "UNIT_HOURS_LONG",
                "unit_hours",
                f"单件 {unit:g} 小时 ≈ {unit / HOURS_PER_DAY:.1f} 个工作日，属跨天件，确认无误可直接通过",
            )
        )

    # 件数缺失独立判断：不能因为单件工时异常就漏掉这一项
    if unit and unit > 0 and (qty is None or qty == 0) and (item.get("rework_qty") or 0) == 0:
        flags.append(
            flag(
                "QTY_MISSING",
                "qualified_qty",
                f"单件工时 {unit:g} 已填，但合格件数为空，本行合计按 0 计",
            )
        )

    declared = item.get("declared_total")
    if declared is not None and unit is not None and qty is not None:
        calc = compute_total_hours(qty, unit)
        if abs(calc - declared) > 0.01:
            flags.append(
                flag(
                    "TOTAL_MISMATCH",
                    "declared_total",
                    f"手写合计 {declared:g}，按 件数×单件 应为 {calc:g}",
                )
            )

    # 单格复查读到了另一个值，但勾稽证据不足以判定谁对 → 交给人工看，不擅改。
    # 人工若采纳了复查值（把单件工时改成它），两者相等就不再报——否则这条提示永远消不掉。
    rec = (item.get("conf") or {}).get("unit_hours_recheck") or {}
    if (
        rec
        and not rec.get("used")
        and rec.get("value") is not None
        and unit is not None
        and abs(float(unit) - float(rec["value"])) > 0.005
    ):
        flags.append(
            flag(
                "UNIT_HOURS_DISAGREE",
                "unit_hours",
                f"单件工时有两种读法：整页 {unit:g} 小时 / 单格复查 {rec.get('value'):g} 小时"
                f"（单格原文「{rec.get('text', '')}」），请对照原图确认",
            )
        )

    finish = item.get("finish_date")
    if not finish:
        # 单据本身没有「日期」列时不必每行都报一次（实测有的加工路线单只有
        # 单件/合计两列工时，日期统一由核算期决定），只在表单确实有日期列时告警
        if has_date_col:
            # 读到了但读不成日期，必须把原文摆出来：手写 9.10 里那个「1」只有 2px 高，
            # 识别网络一律看不见，整页结果是「9.0」。不说原文的话，人只看到一格空日期，
            # 不知道模型到底看见了什么，也没法判断该不该信。
            raw = str(item.get("finish_date_raw") or "").strip()
            if raw:
                flags.append(
                    flag(
                        "DATE_MISSING",
                        "finish_date",
                        f"日期格识别到「{raw}」，不是有效日期，请对照原图手工填写",
                    )
                )
            else:
                flags.append(flag("DATE_MISSING", "finish_date"))
    else:
        try:
            if str(finish)[:7] != period_ym:
                flags.append(
                    flag(
                        "DATE_OUT_OF_PERIOD",
                        "finish_date",
                        f"完成日期 {str(finish)[:10]} 不属于本期 {period_ym}",
                    )
                )
        except Exception:
            flags.append(flag("DATE_MISSING", "finish_date"))

    if not item.get("worker_id"):
        raw = item.get("worker_raw") or ""
        flags.append(
            flag(
                "UNKNOWN_WORKER",
                "worker_raw",
                f"「{raw}」无法匹配名册中的人员，请手工选择" if raw else "未识别到工作者，请手工选择",
            )
        )

    if not item.get("process_code"):
        proc = item.get("process") or ""
        flags.append(
            flag(
                "UNKNOWN_PROCESS",
                "process",
                f"「{proc}」不在工序字典内，将按原文汇总" if proc else "未识别到工序",
            )
        )

    if not (item.get("process") or "").strip():
        # 工序为空和「工序不在字典内」是两件事：后者按原文汇总仍然可查，
        # 前者汇总里只能落进「未填」，工序工时分析从这一刻起就是断的。
        # 工资算得出来不等于账是对的，所以这里按阻断处理（可显式强制确认）。
        flags.append(
            flag(
                "MISSING_PROCESS",
                "process",
                "未填工序：工时归不到工序，汇总会出现「未填」，请补填后再确认",
            )
        )

    # 归集维度缺失必须报出来：产品/件名是成本与工艺分析的挂载点，
    # 空着也能算工资，但月底汇总里会多出一列「未填」，
    # 而人是不会主动去查「为什么这个月有个未填产品」的。
    if not (item.get("product_name") or "").strip():
        flags.append(flag("MISSING_PRODUCT", "product_name"))
    if not (item.get("part_name") or "").strip():
        flags.append(flag("MISSING_PART", "part_name"))

    # 字典兜底：只有「单据上写了值但不在字典里」才提示，空值不打扰
    if item.get("product_name") and not item.get("product_matched", True):
        flags.append(
            flag(
                "UNKNOWN_PRODUCT",
                "product_name",
                f"产品「{item['product_name']}」不在产品字典内，将按原文记录",
            )
        )

    if item.get("material") and not item.get("material_matched", True):
        flags.append(
            flag(
                "UNKNOWN_MATERIAL",
                "material",
                f"材料「{item['material']}」不在材料字典内，将按原文记录",
            )
        )

    if item.get("inspector") and not item.get("inspector_matched", True):
        flags.append(
            flag(
                "UNKNOWN_INSPECTOR",
                "inspector",
                f"检验员「{item['inspector']}」不在人员名册内，建议在「系统管理」补充",
            )
        )

    if item.get("handover") and not item.get("handover_matched", True):
        flags.append(
            flag(
                "UNKNOWN_HANDOVER",
                "handover",
                f"接交人「{item['handover']}」不在人员名册内，建议在「系统管理」补充",
            )
        )

    conf = item.get("conf") or {}
    low = [k for k, v in conf.items() if isinstance(v, (int, float)) and 0 < v < CONF_MID]
    if low:
        names = "、".join(FIELD_CN.get(k, k) for k in low)
        flags.append(
            flag("LOW_CONF", ",".join(low), f"这些字段识别把握偏低，请重点核对：{names}")
        )

    item["total_hours"] = compute_total_hours(
        qty, unit, item.get("rework_qty"), item.get("scrap_qty")
    )
    return flags


def std_hour_deviation(item: dict, std_hours: dict) -> tuple[float, float, int] | None:
    """查这条明细在历史账里的标准单件工时。返回 (标准值, 明细值, 样本数)。

    键是 (产品, 件名, 工序) 三元组——少一个都定不准：同一个产品同一道工序，
    不同零件的单件工时能差好几倍。

    工序先按 code 查、再按字典标准名、最后按显示值：明细上的工序可能写着
    「包板折弯」（字典登记的别名），标准工时表里记的是「折弯」，不回溯就查不到。
    """
    if not std_hours:
        return None
    unit = item.get("unit_hours")
    if not unit or unit <= 0:
        return None
    product = item.get("product_name") or ""
    part = item.get("part_name") or ""
    hit = None
    for proc in (item.get("process_code"), item.get("process_std"), item.get("process")):
        if not proc:
            continue
        hit = std_hours.get((product, part, proc))
        if hit:
            break
    if not hit:
        return None
    value, samples = hit
    return float(value), float(unit), int(samples or 0)


def check_std_hours(item: dict, std_hours: dict, tolerance: float = 2.0) -> list[dict]:
    """按历史标准工时核对单件工时。

    只做「提醒」不做「拦截」：改制、临时加价、试制都是真实存在的，
    标准工时只是复核的参照物，最终仍由人判断。
    """
    hit = std_hour_deviation(item, std_hours)
    if not hit:
        return []
    std, actual, samples = hit
    if std <= 0:
        return []
    ratio = actual / std
    if 1 / tolerance <= ratio <= tolerance:
        return []
    # 用「偏差百分比」而不是「几倍」：0.5 对 12 说成「0.0 倍」等于没说，
    # 说成「-96%」人一眼就知道是少报了一个数量级。
    pct = (actual - std) / std * 100
    return [
        flag(
            "UNIT_HOURS_DEVIATE",
            "unit_hours",
            f"单件工时 {actual:g} 小时，历史标准 {std:g} 小时（{samples} 条样本），"
            f"偏差 {pct:+.0f}%",
        )
    ]


def route_step_lookup(item: dict, route_steps: list[dict]) -> dict | None:
    """在路线里找这道工序。返回那一行，找不到返回 None。

    工序在明细上可能是别名（「包板折弯」），在路线里是标准名（「折弯」），
    所以三种写法都要试：code → 标准名 → 显示名。
    """
    if not route_steps:
        return None
    for proc in (item.get("process_code"), item.get("process_std"), item.get("process")):
        if not proc:
            continue
        for s in route_steps:
            if s.get("process_code") == proc or s.get("process_name") == proc:
                return s
    return None


def check_route(item: dict, routes: dict | None, tolerance: float = 0.5) -> list[dict]:
    """按已发布的工艺路线核对明细行。

    只做「提醒」不做「拦截」，且**只报两类真信号**：

    1. ROUTE_STEP_EXTRA：单据上这道工序不在路线内。
    2. ROUTE_HOURS_DEVIATE：单件工时与路线定额相差超过 ±50%。

    刻意**不报「缺工序」**：路线是片段制的（completeness=partial），
    允许只登记已知工序。按现有数据，801 个组合里 601 个只有 1 道工序，
    报缺工序等于天天误报，用户三周内就会把全部告警忽略。

    没有路线时静默返回——这是「该零件还没建路线」，不是错误。
    """
    if not routes:
        return []
    product = item.get("product_name") or ""
    part = item.get("part_name") or ""
    route_steps = routes.get((product, part))
    if not route_steps:
        return []

    flags: list[dict] = []

    # ---- 1) 工序不在路线内
    if not route_step_lookup(item, route_steps):
        names = "、".join(s.get("process_name") or s.get("process_code") or "?" for s in route_steps)
        shown = item.get("process") or item.get("process_raw") or "空"
        flags.append(
            flag(
                "ROUTE_STEP_EXTRA",
                "process",
                f"工艺路线里没有「{shown}」这道工序（路线现有：{names}），"
                f"可能是新工艺，也可能识别有误",
            )
        )
        return flags  # 工序都不在，工时比对没有意义

    # ---- 2) 工时偏离路线定额
    unit = item.get("unit_hours")
    if not unit or unit <= 0:
        return flags
    step = route_step_lookup(item, route_steps)
    quota = float(step.get("unit_hours") or 0.0)
    if quota <= 0:
        return flags
    # 相对偏差 = (实际 - 定额) / 定额，直接算偏差率。
    # 不用 ratio 倒数比较：ratio=0.533 时倒数的界是 0.667，
    # 会把「-47%」判成超差（0.533 < 0.667），方向算反了。
    delta = (float(unit) - quota) / quota
    # 用 < 而不是 <=：阈值是「偏差达到多少就算超差」，
    # 恰好 ±50% 要报出来（用 <= 会把边界值整条漏掉）。
    if abs(delta) < tolerance:
        return flags
    flags.append(
        flag(
            "ROUTE_HOURS_DEVIATE",
            "unit_hours",
            f"路线定额 {quota:g} 小时，本单 {float(unit):g} 小时（{delta * 100:+.0f}%），请确认是否改制",
        )
    )
    return flags


def validate_sheet(
    items: list[dict],
    period_ym: str,
    columns: list[dict] | None = None,
    std_hours: dict | None = None,
    routes: dict | None = None,
) -> tuple[list[dict], list[str]]:
    """对整张单据校验。返回 (带 flags 的明细, 单据级告警码)。

    columns 是版面识别出来的列定义：没有某列（如「日期」）就不要按该列告警。
    std_hours 是历史单件工时标准表，传了才做「与标准工时偏差大」的提醒。
    routes 是已发布工艺路线索引，传了才做「工序不在路线内」「工时偏离定额」的提醒。
    不传 routes 时行为与引入该功能之前完全一致。
    """
    if not items:
        return [], ["NO_ITEM"]

    present = {str(c.get("key")) for c in (columns or [])}
    has_date_col = ("finish_date" in present) if columns else True

    codes: set[str] = set()
    for item in items:
        flags = validate_item(item, period_ym, has_date_col=has_date_col)
        if std_hours:
            flags.extend(check_std_hours(item, std_hours))
        if routes:
            flags.extend(check_route(item, routes))
        item["flags"] = flags
        for f in flags:
            codes.add(f["code"])
    return items, sorted(codes)


def has_blocking(flags: list[dict]) -> bool:
    return any(f.get("level") == "block" for f in flags)


def summarize_flags(items: list[dict]) -> dict:
    counts: dict[str, int] = {}
    block = 0
    warn = 0
    for item in items:
        for f in item.get("flags", []):
            counts[f["code"]] = counts.get(f["code"], 0) + 1
            if f.get("level") == "block":
                block += 1
            else:
                warn += 1
    return {"total": block + warn, "block": block, "warn": warn, "by_code": counts}


def period_bounds(ym: str) -> tuple[dt.date, dt.date]:
    year, month = int(ym[:4]), int(ym[5:7])
    start = dt.date(year, month, 1)
    end = dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1)
    return start, end


def hours_per_day() -> float:
    return HOURS_PER_DAY
