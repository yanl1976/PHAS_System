"""把工时表的自由写法归并到标准工序 / 标准产品。"""
from __future__ import annotations

import re

from .lexicon import PROCESS_LEXICON

_STEMS: list[tuple[str, str, str, bool, int]] = []  # (stem, code, name, count_hours, prio)
for code, name, cat, count, stems in PROCESS_LEXICON:
    for s in stems:
        _STEMS.append((s, code, name, count, len(s)))
_STEMS.sort(key=lambda x: -x[4])

_NORM = re.compile(r"[\s\-－—_/、,，.。()（）\[\]]+")


def norm(text: str) -> str:
    return _NORM.sub("", (text or "")).upper()


def merge_process(raw: str) -> tuple[str, str, bool, str] | None:
    """归并工序写法 → (code, 标准名, 计工时, 命中的词干)。无法归并返回 None。"""
    t = norm(raw)
    if not t:
        return None
    hits = [(pos, stem, code, name, count) for stem, code, name, count, ln in _STEMS
            for pos in [t.rfind(stem)] if pos >= 0]
    if not hits:
        return None
    # 最长优先；同长取最靠后（「打磨酸洗」→ 酸洗）
    hits.sort(key=lambda h: (-len(h[1]), -h[0]))
    _, stem, code, name, count = hits[0]
    return code, name, count, stem


# ---------------------------------------------------------------- 产品
from .product_lexicon import PRODUCT_FAMILY  # noqa: E402

_FAMILIES: list[tuple[str, str]] = sorted(
    ((alias, fam) for fam, aliases in PRODUCT_FAMILY.items() for alias in aliases),
    key=lambda x: -len(x[0]),
)
_MODEL = re.compile(r"[A-Z]{1,5}[-]?\d+[A-Z]{0,4}(?:[-/]\d+[A-Z]?)*|[A-Z]{2,}[A-Z0-9]*|\d+[A-Z]{1,3}(?:-\d+)?|-?\d+(?:\.\d+)?")
_NOISE = re.compile(r"(发货|试制|改制|返修|加急|配件|新做|改造|维修|组装|调试|安装|打包|全部|试机|约旦|智利)$")


def split_product(raw: str) -> tuple[str, str]:
    """产品写法 → (设备类名, 型号)。类名不在词表里时退回原文，型号为空。"""
    t = _NOISE.sub("", norm(raw))
    fam, alias = "", ""
    for a, f in _FAMILIES:
        if a in t:
            fam, alias = f, a
            break
    if not fam:
        return t, ""
    # 型号 = 去掉类名写法后剩下的字母数字（顺序无所谓：600滚筒筛 / 滚筒筛600 都得到 600）
    rest = t.replace(alias, "", 1)
    rest = re.sub(r"[^A-Z0-9./-]", "", rest)
    models = [m for m in _MODEL.findall(rest) if m not in ("", "-")]
    model = models[0] if models else ""
    return fam, model
