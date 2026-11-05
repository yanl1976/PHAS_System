"""字段归一：工时 / 日期 / 人名 / 工序。

设计要点：
- 手写体识别结果必须按"有限集合"收敛，人名与工序走白名单匹配，不做开放识别；
- 工时写法规格极不统一（25H / 2:30 / 2h30 / 1天 / 2.5），一律归一为小时小数；
- 日期必须能定位到核算期，越界即告警，避免跨月串账。
"""
from __future__ import annotations

import datetime as dt
import difflib
import re
import unicodedata

from ..config import HOURS_PER_DAY, UNIT_HOURS_LIMIT

_CN_NUM = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
}


def to_halfwidth(text: str) -> str:
    """全角转半角，并去掉常见干扰字符（OCR 常把 : 识别成 ，或 ·）。"""
    if not text:
        return ""
    out = unicodedata.normalize("NFKC", text)
    trans = {
        "：": ":", "，": ",", "。": ".", "、": "", "·": ".", "•": ".",
        "－": "-", "—": "-", "～": "~", "｜": "|", "（": "(", "）": ")",
        "　": " ", "\u200b": "",
    }
    for k, v in trans.items():
        out = out.replace(k, v)
    return out.strip()


def clean_text(text: str) -> str:
    """去掉空白并删除全部标点。**仅用于姓名/工序比对。**

    数值与日期解析必须用 compact()，否则 "2.5" 会被清成 "25"、"9.3" 会被清成 "93"。
    """
    text = to_halfwidth(text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[()\[\]{}<>《》\"'`~!@#$%^&*_+=|\\/;,.?:]+", "", text)
    return text


def compact(text: str) -> str:
    """只去空白，保留点号/冒号等数值与日期语义字符。用于工时、日期解析。"""
    if text is None:
        return ""
    return re.sub(r"\s+", "", to_halfwidth(str(text)))


# ------------------------------------------------------------------ 工时
# 现场《加工路线单》「定额工时/单件」栏的手写规矩：
#   2ʰ  = 2 小时（ʰ 是上标 h）
#   30' = 30 分钟（' 是上标的分符号）
#   1ʰ30' = 1 小时 30 分
# NFKC 会把上标 ʰ 归一成 h，但 OCR 经常把上标认成 u/U，或干脆吞掉，
# 于是「1ʰ30'」会变成「130」「1430」，「30'」会变成「30」。两种都要认。
_APOSTROPHE_RE = re.compile(r"['\u2018\u2019\u2032\u00b4`]")
# 上标 h 的 OCR 变体：h/H（NFKC 后）、u/U（实测「2ʰ」被认成「2u」）、
# 以及被认成度数符号 ° 的情况（「1°10'」）
_HOUR_MARK = "hH时時uU°"
# 单件工时超过一天基本不可能是「小时」，只可能是丢了分钟符号
_MAX_UNIT_HOURS = 24.0
_ONE_SHIFT_HOURS = 8.0

_HOUR_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:h|H|小时|時|时)\.?$"), "h"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:天|日|d|D|day|days)\.?$"), "day"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:分钟|分|m|min)$"), "min"),
]


def _split_hhmm(digits: str) -> float | None:
    """把「丢了上标/撇号」的连写数字还原成 时+分。

    实测「1ʰ30'」「2ʰ30'」「1ʰ10'」被识别成 1430 / 2030 / 110：
    上标ʰ变成了一位多余数字，分钟符号被吞掉。
    所以按「首位=小时、末两位=分钟」拆；若拆出来超过一个班次，
    再试「前两位=小时」——单件定额通常小于一个班次。
    """
    if len(digits) < 3:
        return None
    mi = int(digits[-2:])
    if mi >= 60:
        return None
    cands: list[float] = []
    head1 = digits[:-2]
    if head1 and int(head1) <= _MAX_UNIT_HOURS:
        cands.append(int(head1) + mi / 60)
    # 首位当小时，中间那位视为上标的误识（1430 -> 1:30）
    if len(digits) >= 4:
        cands.append(int(digits[0]) + mi / 60)
    if not cands:
        return None
    small = [c for c in cands if c <= _ONE_SHIFT_HOURS]
    return round(min(small) if small else min(cands), 4)


def parse_hours(text: str | float | int | None) -> float | None:
    """把各种工时写法归一为小时小数。无法解析返回 None。"""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    raw = compact(str(text))
    if not raw:
        return None
    raw = _APOSTROPHE_RE.sub("'", raw)

    # 1h30 / 1小时30分 / 2h30min / 1ʰ30' / 2°30'
    m = re.match(rf"^(\d+(?:\.\d+)?)\s*[{_HOUR_MARK}]\s*(\d{{1,2}})\s*'?$", raw)
    if m and int(m.group(2)) < 60:
        return round(float(m.group(1)) + int(m.group(2)) / 60, 4)

    # 2:30 / 2:30:00
    m = re.match(r"^(\d{1,3}):(\d{1,2})(?::(\d{1,2}))?$", raw)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2))
        s = int(m.group(3) or 0)
        total = h + mi / 60 + s / 3600
        return round(total, 4)

    # 30' / 30分 / 30分钟 —— 撇号是分钟符号，不是小数点
    m = re.fullmatch(r"(\d{1,4})'", raw)
    if m:
        digits = m.group(1)
        # 「2030'」「1430'」是「2ʰ30'」「1ʰ30'」丢了上标，不是 2030 分钟
        if len(digits) >= 3:
            split = _split_hhmm(digits)
            if split is not None:
                return split
        return round(int(digits) / 60, 4)

    # 25H / 25小时 / 1天 / 30分 / 2ʰ
    for pat, kind in _HOUR_PATTERNS:
        m = pat.search(raw)
        if m:
            val = float(m.group(1))
            if kind == "day":
                return round(val * HOURS_PER_DAY, 4)
            if kind == "min":
                return round(val / 60, 4)
            return round(val, 4)

    # 2u / 2U：上标 h 被 OCR 认成 u
    m = re.fullmatch(r"(\d+(?:\.\d+)?)[uU]", raw)
    if m:
        return round(float(m.group(1)), 4)

    # 纯数字 25 / 2.5 / 2,5
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)", raw)
    if m:
        val = float(m.group(1).replace(",", "."))
        # 超出一天的纯数字几乎必然是丢了分钟符号：「30」是 30 分钟、
        # 「1430」是 1 小时 30 分。不还原就会算出 1430 小时这种天文数字。
        if val > _MAX_UNIT_HOURS and "." not in raw and "," not in raw:
            split = _split_hhmm(raw)
            if split is not None:
                return split
            if len(raw) == 2:
                return round(val / 60, 4)
        return round(val, 4)

    # 中文数字（一 ~ 十）
    if raw in _CN_NUM:
        return float(_CN_NUM[raw])

    return None


def parse_count(text: str | float | int | None) -> float | None:
    """件数（合格/返修/废品/数量）：只取整数。

    OCR 常在数字前后带出杂点（实测合格「4」被读成「.4」），件数不可能是小数，
    这里直接把非数字字符剔掉，比走 parse_hours 再猜小数点可靠。
    """
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(int(text))
    raw = compact(str(text))
    if not raw:
        return None
    # 千分位（1,050）先合并，避免被当成两个数
    raw = re.sub(r"(?<=\d)[,.]\s*(?=\d{3}\b)", "", raw)
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    return float(int(digits))


def hours_to_text(hours: float | None) -> str:
    """小时小数 -> H:MM 文本，用于界面展示。"""
    if hours is None:
        return ""
    sign = "-" if hours < 0 else ""
    hours = abs(hours)
    h = int(hours)
    m = int(round((hours - h) * 60))
    if m == 60:
        h, m = h + 1, 0
    return f"{sign}{h}:{m:02d}"


# ------------------------------------------------------------------ 日期
_EXCEL_EPOCH = dt.date(1899, 12, 30)


def parse_date(text: str | None, ym: str | None = None) -> tuple[dt.date | None, bool]:
    """解析日期。返回 (日期, 是否仅推测到日)。

    ym 形如 '2026-09'，用于补全缺省的年月。
    """
    if not text:
        return None, False
    raw = compact(str(text))
    if not raw:
        return None, False

    year = month = None
    if ym and re.fullmatch(r"\d{4}-\d{2}", ym):
        year, month = int(ym[:4]), int(ym[5:7])

    # 完整年月日：2026.7.15 / 2026-07-15 / 2026年7月15日
    m = re.search(r"(\d{4})\s*[.\-/年]\s*(\d{1,2})\s*[.\-/月]\s*(\d{1,2})", raw)
    if m:
        try:
            return dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))), False
        except ValueError:
            return None, False

    # 月.日：7.15 / 7-15 / 7月15日
    m = re.search(r"(\d{1,2})\s*[.\-/月]\s*(\d{1,2})", raw)
    if m and year:
        try:
            return dt.date(year, int(m.group(1)), int(m.group(2))), False
        except ValueError:
            pass

    # Excel 序列号 40000~60000
    m = re.fullmatch(r"(\d{5})", raw)
    if m:
        serial = int(m.group(1))
        if 30000 <= serial <= 60000:
            return _EXCEL_EPOCH + dt.timedelta(days=serial), False

    # 仅日：15
    m = re.fullmatch(r"(\d{1,2})", raw)
    if m and year and month:
        day = int(m.group(1))
        if 1 <= day <= 31:
            try:
                return dt.date(year, month, day), True
            except ValueError:
                return None, False

    return None, False


# ------------------------------------------------------------------ 人名
def hamming_best(raw: str, candidates: list[str]) -> tuple[str, float]:
    """在候选集中找最相似的字符串，返回 (候选, 分值 0~1)。"""
    if not raw:
        return "", 0.0
    target = clean_text(raw)
    if not target:
        return "", 0.0
    if target in candidates:
        return target, 1.0

    best, best_score = "", 0.0
    for cand in candidates:
        base = difflib.SequenceMatcher(None, target, cand).ratio()
        # 包含关系加成：'符媛媛签名' 命中 '符媛媛'
        # 但要求较短一方至少 2 个字：单个字符「7」会被「27炼药机发货」这样的
        # 长别名包含，加成后误判成完全无关的条目，比认不出来危险得多。
        if cand and (cand in target or target in cand) and min(len(target), len(cand)) >= 2:
            base = max(base, 0.9)
        # 长度差过大则降权
        if abs(len(target) - len(cand)) >= 2:
            base *= 0.85
        if base > best_score:
            best, best_score = cand, base
    return best, round(best_score, 4)


def match_worker(raw: str, workers: list[tuple[int, str, str]]) -> tuple[int | None, str, float]:
    """在人员名册中匹配。workers 为 [(id, name, aliases)]。"""
    if not clean_text(raw):
        return None, "", 0.0
    lookup: dict[str, tuple[int, str]] = {}
    for wid, name, aliases in workers:
        lookup[name] = (wid, name)
        for alias in re.split(r"[/、|;,，\s]+", aliases or ""):
            if alias:
                lookup[alias] = (wid, name)
    target = clean_text(raw)
    matched, score = hamming_best(target, list(lookup.keys()))
    if not matched or score < 0.55:
        # 姓氏兜底：车间内同姓极少，若名册中该姓氏只对应一人，足以确定人员。
        # 典型场景：手写签名「马永刚」被识别成「马áA」，逐字比对仅 0.33 分，
        # 但名册里马姓只有一人，可安全判定。给 0.5 分保留「低置信度」提示，供人工复核。
        if len(target) >= 2:
            same_surname = {name for _wid, name, _al in workers if name and name.startswith(target[0])}
            if len(same_surname) == 1:
                name = same_surname.pop()
                for wid, wname, _al in workers:
                    if wname == name:
                        return wid, name, 0.5
        return None, matched, score
    wid, name = lookup[matched]
    return wid, name, score


def _build_lookup(entries: list[tuple[str, str, str]]) -> dict[str, tuple[str, str]]:
    lookup: dict[str, tuple[str, str]] = {}
    for code, name, aliases in entries:
        if not name:
            continue
        lookup[name] = (code, name)
        if code:
            lookup.setdefault(code, (code, name))
        for alias in re.split(r"[/、|;,，\s]+", aliases or ""):
            if alias:
                lookup[alias] = (code, name)
    return lookup


def match_entry(raw: str, entries: list[tuple[str, str, str]]) -> tuple[str, str, float]:
    """通用字典匹配。entries 为 [(code, name, aliases)]，返回 (code, name, 分值)。"""
    if not clean_text(raw):
        return "", "", 0.0
    lookup = _build_lookup(entries)
    target = clean_text(raw)
    if not target:
        return "", "", 0.0
    if target in lookup:
        code, name = lookup[target]
        return code, name, 1.0
    # 单字符不做模糊匹配：OCR 常把序号残字（「7」）留在工序格里，
    # 让它去跟字典做相似度计算，只会匹配到某个八竿子打不着的长别名。
    if len(target) <= 1:
        return "", target, 0.0
    matched, score = hamming_best(target, list(lookup.keys()))
    if not matched or score < 0.55:
        return "", clean_text(raw), score
    code, name = lookup[matched]
    return code, name, score


def resolve_entry(
    raw: str, entries: list[tuple[str, str, str]]
) -> tuple[str, str, float, bool]:
    """字典收敛，返回 (code, 标准名, 分值, 是否原样合法)。

    exact=True 表示单据上的写法本身就是字典里的合法写法（含别名），
    此时应保留原文，不要换成标准名——单据写代号 YUJ-18BZ、字典叫「鱼机」时，
    强行替换会让核对界面和原图对不上。只有模糊命中（错字/残字）才纠偏。
    """
    if not clean_text(raw):
        return "", "", 0.0, False
    lookup = _build_lookup(entries)
    target = clean_text(raw)
    exact = target in lookup
    code, name, score = match_entry(raw, entries)
    return code, name, score, exact


def match_process(raw: str, processes: list[tuple[str, str, str]]) -> tuple[str, str, float]:
    """匹配工序字典。processes 为 [(code, name, aliases)]。"""
    if not clean_text(raw):
        return "", "", 0.0
    # 工序常带尾号：铣1 / 立加2 -> 先按前缀匹配
    target = clean_text(raw)
    stripped = re.sub(r"\d+$", "", target)
    if stripped and stripped != target:
        code, name, score = match_entry(stripped, processes)
        if score >= 0.9:
            return code, name, 0.9
    code, name, score = match_entry(target, processes)
    # 字典名只是原文的一小段（「包板折弯」→「折弯」）：不能就此认成「折弯」，
    # 但也不该整句丢掉——交给主工序词干归并（详见 match_process_by_stem）
    composite = bool(name and name != target and name in target and len(target) - len(name) >= 2)
    if code and not composite:
        return code, name, score
    scode, sname, sscore = match_process_by_stem(target, processes)
    if scode:
        return scode, sname, sscore
    if composite:
        # 归并不出来（多半是把零件名填进了工序栏）：保留原文，交给「工序未入典」告警
        return "", target, score
    return code, name, score


def resolve_process(
    raw: str, processes: list[tuple[str, str, str]]
) -> tuple[str, str, float, str, bool]:
    """工序字典收敛，返回 (code, 标准名, 分值, 显示名, 是否原样合法)。

    与 resolve_entry 同一口径：字典里登记过的写法（名称、代号或别名，
    比如「包板折弯」挂在「折弯」下）就原样保留，只有认不准的错字才纠成标准名。
    显示名和 code 分开：界面要跟原图对得上，汇总/标准工时仍按 code 归到同一道工序。
    """
    target = clean_text(raw)
    if not target:
        return "", "", 0.0, "", False
    lookup = _build_lookup(processes)
    if target in lookup:
        code, name = lookup[target]
        return code, name, 1.0, target, True
    code, name, score = match_process(target, processes)
    # 归并/纠偏出来的都不是单据原话，显示层用标准名，原文另存 process_raw
    return code, name, score, name or target, False


def _stem_table(processes: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """从字典现算主工序词干表：工序名本身 + 短的别名（≤4 字）。

    不硬编码词表——用户在系统管理里新加的工序要立刻能参与归并。
    """
    stems: list[tuple[str, str, str]] = []
    for code, name, aliases in processes:
        for s in [name, *re.split(r"[/、|;,，\s]+", aliases or "")]:
            s = clean_text(s)
            if s and len(s) <= 4:
                stems.append((s, code, name))
    stems.sort(key=lambda x: -len(x[0]))
    return stems


def match_process_by_stem(
    raw: str, processes: list[tuple[str, str, str]]
) -> tuple[str, str, float]:
    """复合写法按主工序词干归并（「腔体上下端焊」→ 焊、「包板折弯」→ 折弯）。

    全年工时账里 2600 多种工序写法，纯靠别名表永远补不完；其中大半是
    「零件/部位 + 主工序」的复合描述，主工序就那么几十个。

    规则：取最长的词干命中；长度相同取最靠后的（中文是「修饰语 + 动作」，
    「打磨酸洗」以酸洗收尾）。分值固定 0.72，明显低于精确命中，
    上层据此区分「认出来的」和「归出来的」，不会被当成高置信匹配。
    """
    target = clean_text(raw)
    if not target:
        return "", "", 0.0
    best: tuple[tuple[int, int], str, str] | None = None
    for stem, code, name in _stem_table(processes):
        pos = target.rfind(stem)
        if pos < 0:
            continue
        key = (len(stem), pos)
        if best is None or key > best[0]:
            best = (key, code, name)
    if best:
        return best[1], best[2], 0.72
    return "", "", 0.0


def match_product(raw: str, products: list[tuple[str, str, str]]) -> tuple[str, str, float]:
    """匹配产品字典。产品代号与名称混写常见，别名表需覆盖代号各种写法。"""
    return match_entry(raw, products)


def match_material(raw: str, materials: list[tuple[str, str, str]]) -> tuple[str, str, float]:
    """匹配材料字典。"""
    return match_entry(raw, materials)


def is_unit_hours_abnormal(hours: float | None) -> bool:
    return hours is not None and (hours <= 0 or hours > UNIT_HOURS_LIMIT)
