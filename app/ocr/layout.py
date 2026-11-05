"""结构定位：由 OCR 行坐标还原表格网格，把每段文字归入所属单元格。

策略（三级降级，保证任何质量的照片都能出结果）：
1. 表格线检测：对二值图做行/列投影，取长直线作为行线、列线；
2. 锚点对齐：以印刷体表头关键词的 x 坐标定义列，以行线定义行；
3. 纯聚类兜底：行线缺失时按 y 聚类成行、按 x 间距切成列。
"""
from __future__ import annotations

import re

import numpy as np

from .normalize import clean_text

# 列头锚点 -> 字段键。子表头（单件/合计/合格/返修/废品）优先于跨列母表头。
COLUMN_ANCHORS: dict[str, str] = {
    "工序": "process",
    "工序等级": "process",
    "单件": "unit_hours",
    "合计": "declared_total",
    "实时工时": "real_hours",
    "实时": "real_hours",
    "工作者": "worker_raw",
    "操作者": "worker_raw",
    "签名": "worker_raw",
    "合格": "qualified_qty",
    "返修": "rework_qty",
    "废品": "scrap_qty",
    "检查员": "inspector",
    "检验员": "inspector",
    "日期": "finish_date",
    "交接人": "handover",
    "接交人": "handover",
    "备注": "remark",
}

# 「占位列」：本身不是要入库的字段，但必须占住 x 区间。
# 漏掉它们会让相邻两列的数据被并进同一格（实测：数量「2」+ 单件「4:10:00」
# 被拼成 24:10:00，合格「2」与操作者签名被拼成 22）。
COLUMN_RESERVES: dict[str, str] = {
    "数量": "_qty",
    "件数": "_qty",
    "序号": "_idx",
}

# 表头区（印刷体）键值对标签。顺序有意义：「投产台数」必须排在「台数」前面，
# 否则包含匹配会先命中短标签。
HEADER_LABELS: dict[str, str] = {
    # 标准加工路线单（现行空白表，见 app/ocr/form_spec.py）
    "计调员": "dispatcher",
    "材料员": "picker",
    "投产台数": "machine_count",
    "产品批号": "batch_no",
    "产品代号": "product_code",
    "产品名称": "product_name",
    "件号": "part_no",
    "件名": "part_name",
    "材料": "material",
    "类别": "category",
    "加工件数": "part_count",
    # 早期表格的写法，留作兼容
    "调度员": "dispatcher",
    "领料员": "picker",
    "台数": "machine_count",
}

# 跨列表头 -> 其下子列（按顺序等分）。子表头被漏识时用它回溯补列，
# 这是真实照片的高频情况：小字号子表头容易识别不到，整列随之丢失。
SPAN_CHILDREN: dict[str, list[str]] = {
    "定额工时": ["unit_hours", "declared_total"],
    "加工结果": ["qualified_qty", "rework_qty", "scrap_qty"],
    "工时": ["real_hours"],
}

# 跨列表头，出现时说明其右侧/下方才是真正的列
SPAN_LABELS = {"定额工时", "加工结果", "工时"}

# 表头区可能出现的栏目标签合集。表头标签密集时，「标签右侧最近文本」这个取值策略
# 会取到相邻格自己的标签（真实单据把「加工结果」当成材料值就是这么来的），
# 因此凡是命中这些词的候选值一律丢弃，宁可留空让人工补。
HEADER_WORDS: set[str] = (
    set(HEADER_LABELS)
    | SPAN_LABELS
    | set(COLUMN_ANCHORS)
    | set(COLUMN_RESERVES)
    | {"备注", "检验员", "日期", "合格", "返工", "废品", "件数", "序号", "数量", "操作者", "工序", "单件", "合计"}
)

KEY_LABEL = {v: k for k, v in COLUMN_ANCHORS.items()}


def binarize(gray: np.ndarray, threshold: int = 160) -> np.ndarray:
    return (gray < threshold).astype(np.uint8)


def _cluster(values: list[float], tol: float) -> list[float]:
    """一维聚类，返回各簇中心。"""
    if not values:
        return []
    values = sorted(values)
    groups: list[list[float]] = [[values[0]]]
    for v in values[1:]:
        if v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [float(np.mean(g)) for g in groups]


def detect_lines(gray: np.ndarray) -> tuple[list[float], list[float]]:
    """检测表格横线与竖线坐标。"""
    ink = binarize(gray)
    h, w = ink.shape
    if h < 20 or w < 20:
        return [], []

    row_ratio = ink.sum(axis=1) / w
    col_ratio = ink.sum(axis=0) / h

    h_candidates = [float(y) for y in np.where(row_ratio > 0.45)[0]]
    v_candidates = [float(x) for x in np.where(col_ratio > 0.30)[0]]

    hlines = _cluster(h_candidates, tol=max(3.0, h * 0.006))
    vlines = _cluster(v_candidates, tol=max(3.0, w * 0.006))
    return hlines, vlines


def _x_overlap_ratio(a: dict, b: dict) -> float:
    """两块文字在 x 方向的重叠比例（相对较窄的一方）。"""
    a0, a1 = a["bbox"][0], a["bbox"][2]
    b0, b1 = b["bbox"][0], b["bbox"][2]
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    return inter / min(max(1.0, a1 - a0), max(1.0, b1 - b0))


def _row_conflict(a: dict, b: dict, med_h: float) -> bool:
    """a 与 b 是否因「落在同一列」而必须分属上下两行。"""
    # 高度几乎一致的两块文字，物理上就是同一格里的相邻书写
    # （实测序号「7」与工序「钳」写在同一格里，y 只差 4.5px，
    #  但 OCR 框横向有 38% 交叠，直接按 x 判会把它们拆到两行去）
    if abs(a["cy"] - b["cy"]) < med_h * 0.3:
        return False
    return _x_overlap_ratio(a, b) > 0.35


def group_rows_by_x(lines: list[dict], tol_ratio: float = 0.9) -> list[list[dict]]:
    """按「同列不能同行」把文字切成交明细行。

    纯 y 聚类在手写单据上必然出错：同一行的文字因为手写倾斜/大小不一，
    cy 抖动可达 80px，而行距只有 66px——行内差异比行间差异还大，
    裁任何阈值都会把相邻两行并成一行（实测两行并成一行后，「30'」和「50」
    粘成「5030」、合格「6」粘成「64」）。

    但表格有个硬约束：同一行每列最多一个值，所以同一行的两块文字
    **不可能落在同一个 x 区间**；而上下相邻行的同列文字一定重叠。
    用这个约束切行，比调聚类容差可靠得多。
    """
    if not lines:
        return []
    heights = [ln["h"] for ln in lines if ln["h"] > 0]
    med_h = float(np.median(heights)) if heights else 20.0
    tol_y = max(8.0, med_h * tol_ratio)

    buckets: list[dict] = []
    for ln in sorted(lines, key=lambda x: x["cy"]):
        placed = False
        # 只需回看最近两行：再往上的行不可能还接纳这个文字
        for b in reversed(buckets[-2:]):
            if any(_row_conflict(ln, other, med_h) for other in b["lines"]):
                continue  # 同一列已有值 -> 必是另一行
            if b["top"] - tol_y <= ln["cy"] <= b["bottom"] + tol_y:
                b["lines"].append(ln)
                b["top"] = min(b["top"], ln["cy"])
                b["bottom"] = max(b["bottom"], ln["cy"])
                placed = True
                break
        if not placed:
            buckets.append({"lines": [ln], "top": ln["cy"], "bottom": ln["cy"]})

    out = [b["lines"] for b in buckets]
    for row in out:
        row.sort(key=lambda x: x["cx"])
    return out


def refine_data_rows(rows: list[list[dict]], header_idx: int) -> list[list[dict]]:
    """表头行原样保留，数据区改用 group_rows_by_x 重切。

    只动数据区：表头里「合格返修废品检查员」这类横跨多列的印刷标签
    会与下方数据同列文字 x 冲突，混在一起就再也分不开了。
    """
    if header_idx < 0 or header_idx >= len(rows) - 1:
        return rows
    data_lines = [ln for row in rows[header_idx + 1 :] for ln in row]
    if len(data_lines) < 3:
        return rows
    return rows[: header_idx + 1] + group_rows_by_x(data_lines)


def group_rows(lines: list[dict], tol_ratio: float = 0.7) -> list[list[dict]]:
    """按 y 坐标把文本行聚成视觉行。

    用于表头区定位。数据区请用 group_rows_by_x——手写行的 y 抖动会超过行距，
    这个函数在那里会并掉相邻行。
    """
    if not lines:
        return []
    heights = [ln["h"] for ln in lines if ln["h"] > 0]
    tol = max(8.0, (float(np.median(heights)) if heights else 20.0) * tol_ratio)
    ordered = sorted(lines, key=lambda x: x["cy"])
    rows: list[list[dict]] = [[ordered[0]]]
    for ln in ordered[1:]:
        if ln["cy"] - np.mean([x["cy"] for x in rows[-1]]) <= tol:
            rows[-1].append(ln)
        else:
            rows.append([ln])
    for row in rows:
        row.sort(key=lambda x: x["cx"])
    return rows


def _match_anchor(text: str) -> tuple[str, str] | None:
    """匹配列头锚点。返回 (字段键, 命中的标签)。"""
    t = clean_text(text)
    if not t:
        return None
    # 精确优先
    if t in COLUMN_ANCHORS:
        return COLUMN_ANCHORS[t], t
    # 占位列（数量/序号）只认精确命中：单字标签做包含匹配会命中一大片
    if t in COLUMN_RESERVES:
        return COLUMN_RESERVES[t], t
    if t in SPAN_LABELS:
        return None  # 跨列母表头不单独成列
    # 包含匹配（如 "定额工时单件"、"合格件数"）
    for label in sorted(COLUMN_ANCHORS, key=len, reverse=True):
        if label in t:
            return COLUMN_ANCHORS[label], label
    return None


def split_anchors_in_line(line: dict) -> list[dict]:
    """一行里挤了多个锚点时（OCR 常把整行表头识别成一长串），
    按标签在文本中的字符位置插值出各自的中心 x。"""
    from .normalize import compact

    text = compact(line["text"])
    if not text:
        return []

    hits: list[tuple[int, int, str, str]] = []
    for label in sorted(COLUMN_ANCHORS, key=len, reverse=True):
        start = 0
        while True:
            idx = text.find(label, start)
            if idx < 0:
                break
            hits.append((idx, idx + len(label), label, COLUMN_ANCHORS[label]))
            start = idx + len(label)

    hits.sort()
    filtered: list[tuple[int, int, str, str]] = []
    last_end = -1
    for start, end, label, key in hits:
        if start < last_end:  # 被更长的标签覆盖
            continue
        filtered.append((start, end, label, key))
        last_end = end

    if len(filtered) < 2:
        return []

    x0, _y0, x1, _y1 = line["bbox"]
    width = x1 - x0
    n = len(text)
    return [
        {
            "cx": x0 + width * ((s + e) / 2) / n,
            "cy": line["cy"],
            "label": label,
            "key": key,
            "score": line["score"],
        }
        for s, e, label, key in filtered
    ]


def header_row_indexes(rows: list[list[dict]], header_idx: int) -> list[int]:
    """表头区的所有行索引。

    真实单据的表头常占 2~3 行：合并单元格标签行（台数/产品名称/日期/备注）、
    母表头行（定额工时/加工结果）、子表头行（单件/合计/合格）。
    只取 header_idx 与 header_idx+1 会丢掉「日期」「备注」等独占一行的列。

    扩展条件用「该行是否含表头标签」而不是「是否整行都是标签」：
    子表头行常与数据行粘连（如本行的检查员签名），整行判断会漏掉子表头。
    """
    if header_idx < 0:
        return []
    idxs = {header_idx}
    for step in (-1, 1):
        i = header_idx + step
        while 0 <= i < len(rows) and abs(i - header_idx) <= 2 and _has_header_label(rows[i]):
            idxs.add(i)
            i += step
    return sorted(idxs)


def _has_header_label(row: list[dict]) -> bool:
    """该行是否包含任一表头标签（含跨列母表头）。"""
    for ln in row:
        if _match_anchor(ln["text"]):
            return True
        if clean_text(ln["text"]) in SPAN_LABELS:
            return True
    return False


def merge_vertical_labels(singles: list[dict]) -> list[dict]:
    """把被 OCR 拆成两行的竖排列头拼回来（「数」+「量」→ 数量、「序」+「号」→ 序号）。

    标准表的「序号」「数量」是窄格竖排印刷：上下两个字各占一行，PaddleOCR 会输出
    两条单字行。单字既不是锚点也不是占位（占位列只认精确命中），整列就此消失——
    实测真机照片上「数量」列没了，「数量 1」和「单件 0:48:00」并进同一格，
    单件工时成了 10.8 小时。
    """
    out: list[dict] = []
    chars = [ln for ln in singles if len(clean_text(ln["text"])) == 1]
    used: set[int] = set()
    for i, a in enumerate(chars):
        if a["id"] in used:
            continue
        for b in chars[i + 1:]:
            if b["id"] in used:
                continue
            if abs(a["cx"] - b["cx"]) > max(a["h"], b["h"]) * 0.8:
                continue
            upper, lower = (a, b) if a["cy"] <= b["cy"] else (b, a)
            gap = lower["cy"] - upper["cy"]
            if not (max(a["h"], b["h"]) * 0.6 <= gap <= max(a["h"], b["h"]) * 2.0):
                continue
            text = clean_text(upper["text"]) + clean_text(lower["text"])
            hit = _match_anchor(text)
            if not hit:
                continue
            used.add(a["id"])
            used.add(b["id"])
            out.append(
                {
                    "cx": (upper["cx"] + lower["cx"]) / 2,
                    "cy": (upper["cy"] + lower["cy"]) / 2,
                    "label": hit[1],
                    "key": hit[0],
                    "score": min(upper["score"], lower["score"]),
                }
            )
            break
    return out


def collect_header_anchors(rows: list[list[dict]], header_idx: int) -> list[dict]:
    """收集表头区所有行的锚点，统一为 [{cx, cy, label, key, score}]。

    跨列母表头（定额工时/加工结果）单独标记 span，并带上自身 x 区间，
    供子表头缺失时等分回溯。
    """
    anchors: list[dict] = []
    singles: list[dict] = []
    for idx in header_row_indexes(rows, header_idx):
        for line in rows[idx]:
            multi = split_anchors_in_line(line)
            if multi:
                anchors.extend(multi)
                continue
            hit = _match_anchor(line["text"])
            if hit:
                anchors.append(
                    {"cx": line["cx"], "cy": line["cy"], "label": hit[1], "key": hit[0], "score": line["score"]}
                )
                continue
            label = clean_text(line["text"])
            if label in SPAN_LABELS:
                anchors.append(
                    {
                        "cx": line["cx"],
                        "cy": line["cy"],
                        "label": label,
                        "span": label,
                        "key": None,
                        "x0": line["bbox"][0],
                        "x1": line["bbox"][2],
                        "score": line["score"],
                    }
                )
            elif len(label) == 1:
                singles.append(line)
    anchors.extend(merge_vertical_labels(singles))
    anchors.sort(key=lambda a: a["cx"])
    return anchors


def fill_span_children(anchors: list[dict], page_w: int) -> list[dict]:
    """母表头下的子列缺失时，用母表头自身宽度等分补出。"""
    normals = [a for a in anchors if not a.get("span") and a.get("key")]
    spans = [a for a in anchors if a.get("span")]
    if not spans:
        return normals

    extra: list[dict] = []
    for span in spans:
        children = SPAN_CHILDREN.get(span["span"], [])
        if not children:
            continue
        left = float(span.get("x0", span["cx"]))
        right = float(span.get("x1", span["cx"]))
        if right - left < page_w * 0.02:
            continue
        present = {a["key"] for a in normals if left - 1 <= a["cx"] <= right + 1}
        # 全局已定位的子列同样跳过：子表头可能被识别到母表头范围之外（字宽偏差），
        # 若只按局部范围判断，会再合成一个重复窄列，把真正的数据挤到错误区间。
        located = {a["key"] for a in normals}
        if all(k in located for k in children):
            continue
        step = (right - left) / len(children)
        for i, key in enumerate(children):
            if key in located:
                continue
            extra.append(
                {
                    "cx": left + step * (i + 0.5),
                    "cy": span["cy"],
                    "label": KEY_LABEL.get(key, key),
                    "key": key,
                    "score": span["score"],
                    "synthetic": True,
                }
            )
    return sorted(normals + extra, key=lambda a: a["cx"])


# 数据行的关键字段：缺这些字段的行多为被切碎的残片
KEY_FIELDS = {"unit_hours", "qualified_qty", "worker_raw", "process", "real_hours", "declared_total"}


def _row_cy(rows: list[list[dict]], idx: int) -> float:
    if not (0 <= idx < len(rows)) or not rows[idx]:
        return 0.0
    return float(np.mean([ln["cy"] for ln in rows[idx]]))


def _is_label_text(text: str) -> bool:
    """文本是否为表头标签（或其与杂字的拼接）。

    同一单元格内表头字与杂字会连成一串（实测「级」+「单件」→「级单件」），
    此时按整串比对锚点会失配，需要识别出「含标签且剩余部分无数字」的情况。
    """
    if not text:
        return False
    if text in COLUMN_ANCHORS or text in SPAN_LABELS or text in COLUMN_RESERVES:
        return True
    for label in list(COLUMN_ANCHORS) + list(SPAN_LABELS) + list(COLUMN_RESERVES):
        if label and label in text:
            rest = text.replace(label, "")
            if not rest or not re.search(r"\d", rest):
                return True
    return False


def drop_label_cells(cell_row: dict) -> dict:
    """剔除误归入数据行的表头残留。

    子表头（单件/合计/工时）与首行数据在 y 上粘连时会被聚成同一行，
    这些文字会抢占 unit_hours 等列，导致真正的手写值被挤掉。
    """
    from .normalize import compact

    cells: dict[str, dict] = {}
    for key, cell in cell_row["cells"].items():
        if _is_label_text(compact(cell.get("text", ""))):
            continue
        cells[key] = cell
    return {"row_index": cell_row["row_index"], "cells": cells}


def merge_fragmented_rows(cells: list[dict], rows: list[list[dict]], page_h: float) -> list[dict]:
    """合并被 y 聚类切碎的同一表格行。

    手写签名、日期常有倾斜，同一表格行内文字的基线不齐，单纯按 y 聚类会拆成多行
    （实测：单件工时「25H」与签名「马永刚」被拆成两行，各自成为一条残缺明细）。

    合并条件从严，避免把两行真实数据揉在一起：
    1. 两行单元格的列互不重叠；
    2. 其中至少一行缺少关键字段（即残片）；
    3. 纵向间距不超过约 2 个行高。
    真正相邻的两行数据都带单件工时/件数，列必然重叠，不会被误合。
    """
    merged: list[dict] = []
    # 「只有工序、没有任何数字」的行一定是被切碎的残片：一个真实的明细行不可能只有工序。
    # 把工序也当关键字段的话，这类残片永远不会被合并，结果是明细里多出一条只有工序的空行，
    # 而相邻那条有数量工时却没有工序。
    num_fields = KEY_FIELDS - {"process"}
    for cur in cells:
        if merged:
            prev = merged[-1]
            pkeys, ckeys = set(prev["cells"]), set(cur["cells"])
            prev_has, cur_has = bool(pkeys & num_fields), bool(ckeys & num_fields)
            gap = _row_cy(rows, cur["row_index"]) - _row_cy(rows, prev["row_index"])
            if (
                pkeys and ckeys
                and not (pkeys & ckeys)
                and (not prev_has or not cur_has)
                and gap <= max(60.0, page_h * 0.06)
            ):
                prev["cells"].update(cur["cells"])
                continue
        merged.append({"row_index": cur["row_index"], "cells": dict(cur["cells"])})
    return merged


def is_header_row(row: dict) -> bool:
    """判断某行是否仍是表头（全部单元格都是表头标签），用于跳过第二行子表头。"""
    from .normalize import compact

    texts = [compact(c.get("text", "")) for c in row.get("cells", {}).values()]
    texts = [t for t in texts if t]
    if not texts:
        return False
    labelish = sum(1 for t in texts if _match_anchor(t) or t in SPAN_LABELS or t in COLUMN_ANCHORS)
    return labelish == len(texts)


def find_header_band(rows: list[list[dict]], page_h: float | None = None) -> int:
    """定位表头所在行号（取锚点命中最多的一行，且限定在页面上半部）。

    这里不能按「行数的前 60%」来限范围：行数是 OCR 认出来的文字行数，空白多的单子
    只有十几行，表头（第 8 行）刚好被切在界外，header_idx 直接 -1，整张单子的列定义
    全空、明细为 0。实测单据 6（13 行、表头在 idx 7）就是这么丢的。
    改为按 y 坐标限在页面上半部。
    """
    best_idx, best_hits = -1, 0
    limit = len(rows)
    if page_h:
        upper = page_h * 0.6
        limit = 0
        for row in rows:
            if not row:
                continue
            if min(ln["cy"] for ln in row) > upper:
                break
            limit += 1
    limit = max(3, limit)
    for idx, row in enumerate(rows[:limit]):
        hits = 0
        for ln in row:
            multi = split_anchors_in_line(ln)
            hits += len(multi) if multi else (1 if _match_anchor(ln["text"]) else 0)
        if hits > best_hits:
            best_idx, best_hits = idx, hits
    return best_idx if best_hits >= 2 else -1


def build_columns(rows: list[list[dict]], header_idx: int, page_w: int) -> list[dict]:
    """由表头锚点构造列定义：[{key, label, x0, x1}]。"""
    if header_idx < 0:
        return []

    anchors = collect_header_anchors(rows, header_idx)
    if not anchors:
        return []

    anchors = fill_span_children(anchors, page_w)

    # 同一 x 位置有母表头与子表头时，保留子表头（y 更大者）
    deduped: list[dict] = []
    for a in anchors:
        if deduped and abs(a["cx"] - deduped[-1]["cx"]) < page_w * 0.02:
            if a["cy"] > deduped[-1]["cy"]:
                deduped[-1] = a
            continue
        deduped.append(a)

    # 同一字段键只保留最左一个，避免重复列
    seen_keys: set[str] = set()
    unique: list[dict] = []
    for a in deduped:
        if a["key"] in seen_keys:
            continue
        seen_keys.add(a["key"])
        unique.append(a)

    columns: list[dict] = []
    for i, a in enumerate(unique):
        cx = a["cx"]
        left = 0.0 if i == 0 else (unique[i - 1]["cx"] + cx) / 2
        right = float(page_w) if i == len(unique) - 1 else (cx + unique[i + 1]["cx"]) / 2
        columns.append({"key": a["key"], "label": a["label"], "x0": left, "x1": right, "cx": cx})
    return columns


def snap_columns_to_vlines(columns: list[dict], vlines: list[float], page_w: int) -> list[dict]:
    """用检测到的竖线校正列边界。

    列边界原本取两个列头锚点的中点，但锚点是「表头文字的中心」，格子宽度差别很大时
    中点会严重偏离真实边线：标准加工路线单的「序号」只有 8mm、「工序等级」65mm，
    两者中点落在 工序等级 格内部的左侧，那里正是手写工序名的起笔位置——工序整列被
    判给了序号列，明细的工序全空。

    改法：边界必须落在左右两个锚点之间，取该区间内**唯一/最近**的那条竖线。
    区间约束保证不会跳到别的列，竖线保证落在印刷格线上，比中点可靠得多。
    """
    if not vlines or not columns:
        return columns
    snapped = [dict(c) for c in columns]
    vs = sorted(vlines)

    def _mid(col: dict, edge: str) -> float:
        return float(col[edge])

    # 列头文字中心（锚点）。首/末列的原边界是 0 和页宽，不能用边界推算中心
    centers = [float(c.get("cx", (c["x0"] + c["x1"]) / 2)) for c in snapped]

    for i, col in enumerate(snapped):
        for edge, j in (("x0", i - 1), ("x1", i + 1)):
            if 0 <= j < len(centers):
                lo, hi = sorted((centers[i], centers[j]))
                inside = [v for v in vs if lo + 1 < v < hi - 1]
            elif edge == "x0":
                inside = [v for v in vs if v < centers[i] - 1]
            else:
                inside = [v for v in vs if v > centers[i] + 1]
            if not inside:
                continue
            target = _mid(col, edge)
            if 0 <= j < len(centers):
                col[edge] = min(inside, key=lambda v: abs(v - target))
            else:
                # 首列左边界取最左竖线、末列右边界取最右竖线（表格外框）
                col[edge] = inside[0] if edge == "x0" else inside[-1]

    # 边界必须严格递增，否则说明吸附把某列压没了，退回未吸附版本
    for a, b in zip(snapped, snapped[1:]):
        if b["x0"] < a["x0"]:
            return columns
    for col in snapped:
        if col["x1"] <= col["x0"]:
            return columns
    return snapped


def assign_cells(rows: list[list[dict]], columns: list[dict], data_start: int) -> list[dict]:
    """把表头以下的行按列归属，返回 [{row_index, cells:{key: line}}]。"""
    result: list[dict] = []
    # 占位列（数量/序号）除了占住 x 区间，也把值留下来：`_qty` 在「合格」列为空
    # 时可作为件数兜底，`_idx` 只用于调试，不参与入库
    for idx in range(data_start, len(rows)):
        row = rows[idx]
        cells: dict[str, dict] = {}
        for ln in row:
            for col in columns:
                if col["x0"] <= ln["cx"] < col["x1"]:
                    # 同一格多段文字时拼接
                    if col["key"] in cells:
                        cells[col["key"]] = {
                            "text": cells[col["key"]]["text"] + ln["text"],
                            "score": min(cells[col["key"]]["score"], ln["score"]),
                            "bbox": cells[col["key"]]["bbox"],
                            "poly": cells[col["key"]]["poly"],
                        }
                    else:
                        cells[col["key"]] = {
                            "text": ln["text"],
                            "score": ln["score"],
                            "bbox": ln["bbox"],
                            "poly": ln["poly"],
                        }
                    break
        if cells:
            result.append({"row_index": idx, "cells": cells})
    return result


def fit_columns_to_template(
    columns: list[dict], page_w: int, vlines: list[float] | None = None
) -> list[dict]:
    """按标准《加工路线单》的相对列宽，拟合现场照片的列边界。

    只解两个未知数：表格左沿 b 与每毫米对应的像素数 s。
    参与拟合的是「本表列头的相对中心」与「现场识别到的锚点中心」，样本即全部列头，
    十二个点求两个参数，稳得很。

    只在真的就是标准表时才动手（列键顺序必须完全一致，且拟合残差要小），
    换了一种表单就原样返回，退回到锚点中点/竖线吸附的老路子。
    """
    if len(columns) < 6:
        return columns
    from . import form_spec

    std_keys = form_spec.keys()
    present = [c["key"] for c in columns]
    # 必须是标准表的子序列：顺序一致、没有多出来的列。
    # 允许缺列——现场漏识一列（例如窄格竖排的「数量」）很常见，
    # 缺列时按标准位置仍能算准其余列的边界，比整张表退回中点方案强得多。
    if not set(present) <= set(std_keys):
        return columns
    if present != [k for k in std_keys if k in set(present)]:
        return columns

    rel = form_spec.rel_centers()
    xs = [float(c.get("cx", (c["x0"] + c["x1"]) / 2)) for c in columns]
    rs = [rel[c["key"]] for c in columns]

    n = len(rs)
    mean_r = sum(rs) / n
    mean_x = sum(xs) / n
    denom = sum((r - mean_r) ** 2 for r in rs)
    if denom <= 0:
        return columns
    scale = sum((r - mean_r) * (x - mean_x) for r, x in zip(rs, xs)) / denom
    offset = mean_x - scale * mean_r
    if scale <= 0:
        return columns

    resid = [abs(offset + scale * r - x) for r, x in zip(rs, xs)]
    rmse = (sum(v * v for v in resid) / n) ** 0.5
    # 残差过大 = 透视变形严重或根本不是这张表，宁可用旧边界
    if rmse > page_w * 0.012:
        return columns

    edges_rel = form_spec.rel_edges()
    idx_of = {k: i for i, k in enumerate(std_keys)}
    out: list[dict] = []
    for col in columns:
        i0 = idx_of[col["key"]]
        x0 = offset + scale * edges_rel[i0]
        x1 = offset + scale * edges_rel[i0 + 1]
        # 有竖线时把边界吸附过去，抵消纸张缩放与少量透视
        if vlines:
            for which in ("x0", "x1"):
                target = x0 if which == "x0" else x1
                nearest = min(vlines, key=lambda v: abs(v - target))
                if abs(nearest - target) <= page_w * 0.010:
                    if which == "x0":
                        x0 = float(nearest)
                    else:
                        x1 = float(nearest)
        out.append({**col, "x0": max(0.0, x0), "x1": min(float(page_w), x1)})

    for a, b in zip(out, out[1:]):
        if b["x0"] < a["x0"]:
            return columns
    for col in out:
        if col["x1"] <= col["x0"]:
            return columns
    return out


def _is_total_row(cell_row: dict) -> bool:
    """是否为表尾「小计：」行。

    标准加工路线单在小计行之后还有「车间主任：  计划调度：  日期：」表尾，
    它们会被当成数据行——实测「计划调度」曾被写进明细的操作者列。
    """
    from .normalize import compact

    for key in ("_idx", "process", "_qty"):
        cell = cell_row["cells"].get(key)
        if not cell:
            continue
        t = compact(cell.get("text", ""))
        if t.startswith("小计") or t.startswith("合计"):
            return True
    return False


def cut_at_total_row(cell_rows: list[dict]) -> list[dict]:
    """截断到小计行（不含），把表尾文字挡在明细之外。"""
    for i, row in enumerate(cell_rows):
        if _is_total_row(row):
            return cell_rows[:i]
    return cell_rows


def build_layout(lines: list[dict], page_w: int, page_h: int, gray: np.ndarray | None = None) -> dict:
    """生成整页版面结构。"""
    rows = group_rows(lines)
    header_idx = find_header_band(rows, page_h)
    # 表头定位完再把数据区重切：y 聚类会把相邻两行并成一行，
    # 一合并就会把「30'」和「50」粘成「5030」、合格「6」粘成「64」
    rows = refine_data_rows(rows, header_idx)
    columns = build_columns(rows, header_idx, page_w)

    hlines: list[float] = []
    vlines: list[float] = []
    if gray is not None:
        hlines, vlines = detect_lines(gray)
        columns = snap_columns_to_vlines(columns, vlines, page_w)
    columns = fit_columns_to_template(columns, page_w, vlines)

    data_start = header_idx + 1 if header_idx >= 0 else 0
    raw_cells = assign_cells(rows, columns, data_start) if columns else []
    raw_cells = cut_at_total_row(raw_cells)
    # 先剔除粘连进数据行的子表头文字，再合并被 y 聚类切碎的残片
    cleaned = [drop_label_cells(c) for c in raw_cells]
    raw_cells = merge_fragmented_rows(cleaned, rows, page_h)
    # 跳过第二行子表头（单件/合计/合格/返修/废品），否则会被误当成一条空明细
    cells = [c for c in raw_cells if not is_header_row(c)]

    # 表头区下沿：单格补检时用它挡住「合格」这类印刷体表头，避免把表头字当数据。
    # 只统计**表头标签本身**的 bbox，不能统计表头区整行的下沿——首行数据常与表头
    # 在 y 上粘连（实测「杰」的签名框下沿 1021 会把数据行整个盖掉）。
    header_bottom = 0.0
    if header_idx >= 0:
        ys = [
            ln["bbox"][3]
            for i in header_row_indexes(rows, header_idx)
            for ln in rows[i]
            if ln.get("bbox") and (clean_text(ln["text"]) in SPAN_LABELS or _match_anchor(ln["text"]))
        ]
        header_bottom = float(max(ys)) if ys else 0.0

    return {
        "rows_count": len(rows),
        "header_index": header_idx,
        "header_bottom": header_bottom,
        "columns": columns,
        "cells": cells,
        "hlines": hlines,
        "vlines": vlines,
        "page_w": page_w,
        "page_h": page_h,
    }
