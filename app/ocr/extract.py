"""字段抽取：把版面结构翻译成业务记录。"""
from __future__ import annotations

import datetime as dt
import re

from .cellprobe import inject as inject_probe
from .cellprobe import probe_digit_cells
from .layout import HEADER_LABELS, HEADER_WORDS, _match_anchor, build_layout
from .normalize import (
    clean_text,
    match_worker,
    parse_count,
    parse_date,
    parse_hours,
    resolve_entry,
    resolve_process,
)

EMPTY_DICTS: dict[str, list] = {
    "workers": [],
    "inspectors": [],
    "receivers": [],
    "processes": [],
    "products": [],
    "materials": [],
}

# 勾稽容差（小时）：手写合计与「数量 × 单件」允许的差
_TOTAL_TOL = 0.02


def _is_table_label(text: str) -> bool:
    """文本是不是表格列头（数量/操作者/合格返修…），不是可用的字段值。

    真实单据里列头与表头标签常常挨得很近，「标签右侧最近的文本」会抓到相邻列头，
    曾把「产品名称」识别成「数量」。
    """
    t = clean_text(text)
    if not t:
        return False
    if t in HEADER_WORDS:
        return True
    hit = _match_anchor(t)
    return bool(hit) and len(t) <= 8


def _same_band(a: dict, b: dict) -> bool:
    tol = max(a["h"], b["h"]) * 0.9
    return abs(a["cy"] - b["cy"]) <= tol


def _cut_after_label(text: str, label: str) -> str:
    """从「产品名称UJ-18BZ制丸机材料」这类粘连文本中切出标签后的值。"""
    from .normalize import compact

    t = compact(text)
    idx = t.find(label)
    if idx < 0:
        return ""
    rest = t[idx + len(label):]
    cuts = [rest.find(other) for other in HEADER_LABELS if other != label and other in rest]
    if cuts:
        rest = rest[: min(cuts)]
    return rest.strip()


def extract_header(lines: list[dict], header_band_y: float) -> tuple[dict, dict[str, float]]:
    """抽取表头键值对。

    表单是「标签格 + 右侧值格」的结构，所以判定归属时看**值左边最近的那个标签**，
    而不是「标签右边最近的文本」——后者在标签密集时会串行（实测「投产台数」和
    「产品名称」抢同一个值，「材料」拿到别人家的值）。

    OCR 常把「产品名称」与相邻格粘成一行，所以先处理同行粘连，再处理独立值行。
    """
    header: dict[str, str] = {}
    conf: dict[str, float] = {}

    # 1) 找出所有标签行
    label_hits: list[dict] = []
    for ln in lines:
        t = clean_text(ln["text"])
        if not t:
            continue
        for label, key in HEADER_LABELS.items():
            if t == label or (label in t and len(t) <= len(label) + 2):
                label_hits.append(
                    {
                        "id": ln["id"],
                        "key": key,
                        "label": label,
                        "right": ln["bbox"][2],
                        "cy": ln["cy"],
                        "h": ln["h"],
                    }
                )
                break

    # 标准表把「件号」印了两遍（表头第 1、2 行各一格），第二格现场习惯写件名。
    # 同一个标签命中两次时，第二个改归件名，否则件名永远是空的。
    dup_fallback = {"part_no": "part_name", "category": "category"}
    seen_keys: set[str] = set()
    for hit in sorted(label_hits, key=lambda x: (x["cy"], x["right"])):
        if hit["key"] in seen_keys:
            alt = dup_fallback.get(hit["key"])
            if alt and alt not in seen_keys and alt != hit["key"]:
                hit["key"] = alt
        seen_keys.add(hit["key"])

    def _store(key: str, value: str, score: float) -> None:
        if key in header:
            return
        v = (value or "").strip()
        if not v or _is_table_label(v):
            return
        if key in ("part_no", "part_name", "material", "category"):
            v = clean_text(v)
            if not v:
                return
        header[key] = v
        conf[key] = score

    by_id = {ln["id"]: ln for ln in lines}

    # 2) 值与标签粘在同一行：从标签右侧切出来
    for hit in label_hits:
        ln = by_id.get(hit["id"])
        if not ln:
            continue
        cut = _cut_after_label(ln["text"], hit["label"])
        # 切出单字多半是标签自身的残字（「材料员」切出「员」），不算值
        if len(clean_text(cut)) >= 2 and not any(other in cut for other in HEADER_LABELS):
            _store(hit["key"], cut, ln["score"])

    # 3) 独立的值行：与左侧标签做「全局最近优先」配对。
    #    逐个值行顺序配对会让上方的噪声（如单据标题）抢走下方的真值，
    #    所以先算出所有可行的（值行, 标签）组合，按距离从近到远依次成立。
    label_ids = {h["id"] for h in label_hits}
    page_w_est = max((ln["bbox"][2] for ln in lines), default=1) or 1
    max_dx = page_w_est * 0.35
    pairs: list[tuple[float, float, dict, dict]] = []
    for ln in lines:
        if ln["id"] in label_ids:
            continue
        text = ln["text"].strip()
        if not clean_text(text) or _is_table_label(text):
            continue
        # 值里混着别的标签（「推产会，件号」）说明这一行本身没被切干净，不能当值
        if any(other in text for other in HEADER_LABELS):
            continue
        # 单据标题/公司名不是字段值（实测「加工路线单」被塞进产品批号）
        if "路线单" in text or "公司" in text:
            continue
        for h in label_hits:
            dx = ln["cx"] - h["right"]
            if dx > max_dx:
                continue
            # 纵向容差按**标签自身的行高**算，不能取两者最大值：
            # 值格里的字一旦被 OCR 框成大字（单据 5 把标题右上的红笔名框成 h=102），
            # 容差被撑到 92px，隔了一整行的红笔名就被当成「类别」的值配进去了。
            if (
                h["right"] <= ln["cx"] + ln["w"] * 0.2
                and abs(ln["cy"] - h["cy"]) <= h["h"] * 0.9
            ):
                pairs.append((dx, abs(ln["cy"] - h["cy"]), ln, h))
    # 排序不能只看横向距离：表头标签左右挨得很近，跨行的标签横向距离可能比同行还小，
    # 于是「产品批号」抢走了下一行「产品代号」的值（实测 batch_no 变成了产品代号 YUJ-18BZ）。
    # 纵向错行远比横向远一点严重，所以按 dy 加权的综合距离排序。
    pairs.sort(key=lambda p: p[1] * 5 + p[0])
    taken_lines: set[int] = set()
    for _dx, _dy, ln, h in pairs:
        if h["key"] in header or ln["id"] in taken_lines:
            continue
        _store(h["key"], ln["text"].strip(), ln["score"])
        if h["key"] in header:
            taken_lines.add(ln["id"])
    return header, conf


def _row_is_meaningful(row: dict) -> bool:
    """判断某个视觉行是否值得成为一条明细。

    空行、表尾合计行、以及被切碎的杂字都会聚成一行，全部放过会让单据凭空多出几条
    空明细（实测一张只有 1 行数据的单据被拆成 4 行）。
    """
    cells = row["cells"]
    texts = {k: clean_text(str(c.get("text", ""))) for k, c in cells.items()}
    texts = {k: v for k, v in texts.items() if v}
    if not texts:
        return False
    useful = {"unit_hours", "qualified_qty", "worker_raw", "process", "part_name", "real_hours"}
    if not (set(texts) & useful):
        return False
    # 只剩「合计」一个数：多半是表尾的合计行（或杂字被归到合计列），不是明细
    if set(texts) == {"declared_total"}:
        return False
    has_number = any(
        parse_hours(texts[k]) is not None
        for k in ("unit_hours", "qualified_qty", "declared_total", "rework_qty", "scrap_qty", "_qty")
        if k in texts
    )
    has_process = len(texts.get("process", "")) >= 2
    has_worker = len(texts.get("worker_raw", "")) >= 2
    return bool(has_number or has_process or has_worker)


def extract_items(
    layout: dict,
    dicts: dict[str, list],
    ym: str,
    defaults: dict | None = None,
) -> list[dict]:
    """把版面行翻译成明细。

    dicts 为各份识别名册（workers/inspectors/receivers/processes/products/materials），
    所有文本字段先走字典收敛，匹配不上才保留原文，交由规则层告警。
    """
    dicts = {**EMPTY_DICTS, **(dicts or {})}
    defaults = defaults or {}
    items: list[dict] = []
    order = 0
    for row in layout.get("cells", []):
        if not _row_is_meaningful(row):
            continue
        order += 1
        cells = row["cells"]
        get = lambda k: cells.get(k, {}).get("text", "")  # noqa: E731

        unit_hours = parse_hours(get("unit_hours")) or parse_hours(get("declared_total"))
        # 「合格」列空着但「数量」列有数（有的单子只填数量）时用数量兜底。
        # 件数只取整数：OCR 常在数字前带出杂点（实测合格「4」读成「.4」）
        qty_raw = get("qualified_qty") or get("_qty")
        qty = parse_count(qty_raw) if qty_raw else None

        # 日期格原文：必须保留标点（清成「910」就再也判断不出是 9.10 还是 9/10）
        date_raw = str(cells.get("finish_date", {}).get("text", "") or "").strip()
        finish_date, date_guessed = parse_date(date_raw, ym)
        worker_raw = clean_text(get("worker_raw"))
        wid, wname, wscore = match_worker(worker_raw, dicts["workers"])
        # 签名认不出来时经常只剩一个笔画残字（实测「操作者」格里是「2」）：
        # 与其显示成「工作者 2」，不如留空，老老实实提示人工选择
        if not wid and len(worker_raw) <= 1:
            worker_raw = ""
        # 工序格左侧常和「序号」粘连（实测「1包板折弯」），去掉行首序号再匹配
        proc_raw = re.sub(r"^\d{1,2}(?=[\u4e00-\u9fa5])", "", clean_text(get("process")))
        # 「包板折弯」是「折弯」的字典别名：code 归到折弯，但显示必须照单据写
        pcode, pstd, pscore, pdisp, pexact = resolve_process(proc_raw, dicts["processes"])

        # 检验员 / 接交人：同样是封闭集，识别残字（如「杰」之于「杜文杰」）靠姓氏兜底
        ins_raw = clean_text(get("inspector"))
        _iid, ins_name, ins_score = match_worker(ins_raw, dicts["inspectors"])
        ho_raw = clean_text(get("handover"))
        _hid, ho_name, ho_score = match_worker(ho_raw, dicts["receivers"])

        # 产品 / 材料：行内优先，其次用表头默认值。
        # 精确命中（含别名）保留单据原写法，只在错字时才纠偏为标准名。
        prod_raw = clean_text(get("product_name")) or defaults.get("product_name", "")
        pdc, prod_std, prod_score, prod_exact = resolve_entry(prod_raw, dicts["products"])
        prod_name = prod_raw if (prod_exact or not pdc) else (prod_std or prod_raw)
        mat_raw = clean_text(get("material")) or defaults.get("material", "")
        mdc, mat_std, mat_score, mat_exact = resolve_entry(mat_raw, dicts["materials"])
        mat_name = mat_raw if (mat_exact or not mdc) else (mat_std or mat_raw)

        # 件名与工序是两回事：件名是零件（机架/刀轮/进料斗），工序是干法。
        # 有件名字典时按它收敛错字；没有就原样保留——新零件总在不断出现。
        part_raw = clean_text(get("part_name")) or defaults.get("part_name", "")
        _ptc, part_std, part_score, part_exact = resolve_entry(part_raw, dicts.get("parts") or [])
        part_name = part_raw if (part_exact or not part_std) else part_std

        # 名册为空 = 该字段未启用，不算未匹配，避免未维护字典时满屏告警
        prod_matched = bool(pdc) or not dicts["products"]
        mat_matched = bool(mdc) or not dicts["materials"]
        part_matched = bool(part_std) or not (dicts.get("parts") or [])
        ins_matched = bool(ins_name) or not dicts["inspectors"]
        ho_matched = bool(ho_name) or not dicts["receivers"]

        conf = {
            "unit_hours": cells.get("unit_hours", {}).get("score", 0.0),
            # 原文必须保留标点（clean_text 会把「2.5」洗成「25」），
            # 复查时用它判断「候选的冒号/小数点还在不在」
            "unit_hours_raw": str(cells.get("unit_hours", {}).get("text", "") or "").strip(),
            "qualified_qty": cells.get("qualified_qty", {}).get("score", 0.0),
            "process": cells.get("process", {}).get("score", 0.0),
            "worker": round(wscore, 4),
            "finish_date": cells.get("finish_date", {}).get("score", 0.0),
            # 检验员报的是**人名匹配的把握**，不是签名那几笔的 OCR 置信度。
            # 草书签名 OCR 永远只有 0.3 上下；一旦按别名认出「定生=杜文杰」，
            # 人名就是确定的，不该再挂「低置信度」让人重看一遍。
            # 没认出来时给 0（LOW_CONF 只处理 >0 的），由「检验员未匹配」单独提示，避免两条重复告警。
            "inspector": round(ins_score, 4) if ins_name else 0.0,
            "product_name": round(prod_score, 4),
            "material": round(mat_score, 4),
        }

        bbox = {}
        for key in ("unit_hours", "qualified_qty", "process", "worker_raw", "finish_date", "inspector"):
            if key in cells:
                bbox[key] = cells[key]["bbox"]

        items.append(
            {
                "seq": order,
                "product_name": prod_name or prod_raw or defaults.get("product_name", ""),
                "product_code": pdc,
                "product_matched": prod_matched,
                "batch_no": clean_text(get("batch_no")) or defaults.get("batch_no", ""),
                "part_name": part_name,
                "part_matched": part_matched,
                "material": mat_name or mat_raw or defaults.get("material", ""),
                "material_matched": mat_matched,
                "process": pdisp or proc_raw,
                # 复合工序会被词干归并（包板折弯 → 折弯），原文必须留着，
                # 否则核对时对着原图找不到「折弯」两个字
                "process_raw": proc_raw,
                # 字典标准名：显示层跟着单据走，汇总和标准工时还得认同一道工序
                "process_std": pstd,
                "process_code": pcode,
                "qualified_qty": qty,
                # 返修/废品同样是件数，走整数解析
                "rework_qty": parse_count(get("rework_qty")),
                "scrap_qty": parse_count(get("scrap_qty")),
                "unit_hours": unit_hours,
                "declared_total": parse_hours(get("declared_total")),
                "real_hours": parse_hours(get("real_hours")),
                "finish_date": finish_date.isoformat() if finish_date else None,
                "date_guessed": date_guessed,
                # 解析失败时把原文带出去：提示要能说「识别到 9.0」，人才知道该改哪一笔
                "finish_date_raw": date_raw,
                "inspector": ins_name or ins_raw,
                # 签名格原文单独存：人工改正后要拿它建别名（见 app/services/learn.py）
                "inspector_raw": ins_raw,
                "inspector_matched": ins_matched,
                "handover": ho_name or ho_raw,
                "handover_matched": ho_matched,
                "worker_id": wid,
                "worker_name": wname,
                "worker_raw": worker_raw,
                "remark": clean_text(get("remark")),
                "conf": conf,
                "bbox": bbox,
                "row_state": "auto",
            }
        )
    return items


def _recheck_unit_hours(gray, items: list[dict]) -> None:
    """单件工时格复查：整页读错时，用裁格重识别纠一次。

    背景：印刷体「0:45:00」在整页尺度下被读成「0:48:00」（格线穿过 5 的肚子），
    裁格去线后能稳定读对。但裁格结果不一定更可信（错值置信度反而更高），
    所以不按置信度取舍，只按**业务勾稽**仲裁：

    1. 整页完全没读到单件工时、裁格读到了 → 直接采用（这就是补检）；
    2. 有手写合计时，看「数量 × 单件 = 合计」——整页值对不上而裁格值对得上 → 采用裁格；
    3. 两边都对不上（或没有合计数可对）→ 保留整页值，只把分歧记进 conf，
       由规则层报「工时识别分歧」让人工看一眼，绝不擅改。
    """
    if gray is None or not items:
        return
    try:
        from .cellprobe import recheck_budget_left, recheck_enabled, recheck_hours_cell
    except Exception:  # noqa: BLE001
        return
    if not recheck_enabled():
        return

    budget = recheck_budget_left()
    for it in items:
        bbox = (it.get("bbox") or {}).get("unit_hours")
        if not bbox or budget <= 0:
            continue
        budget -= 1
        hint = (it.get("conf") or {}).get("unit_hours_raw") or ""
        got = recheck_hours_cell(gray, bbox, hint)
        if not got:
            continue
        text, score = got
        alt = parse_hours(text)
        if alt is None or alt <= 0 or alt > 500:
            continue

        unit = it.get("unit_hours")
        if unit is not None and abs(alt - float(unit)) <= 0.005:
            continue

        qty = it.get("qualified_qty") or 0
        declared = it.get("declared_total")
        used = False
        if unit is None:
            used = True
        elif declared is not None and qty:
            bad_current = abs(qty * float(unit) - float(declared)) > _TOTAL_TOL
            ok_alt = abs(qty * alt - float(declared)) <= _TOTAL_TOL
            used = bad_current and ok_alt

        conf = it.setdefault("conf", {})
        if used:
            it["unit_hours"] = round(alt, 4)
            conf["unit_hours"] = round(score, 4)
        conf["unit_hours_recheck"] = {
            "text": text,
            "value": round(alt, 4),
            "score": round(score, 4),
            "used": used,
            "before": unit,
        }


def _guess_worker_from_top(
    lines: list[dict], page_w: float, page_h: float, workers: list[tuple[int, str, str]]
) -> str:
    """右上角红笔圈出的手写姓名：仅在右上区域、且匹配分数足够高时才采信。

    红笔字迹在预处理中被抑制后笔画偏淡，识别易出错，因此只作提示、不作依据。
    """
    zone = [
        ln
        for ln in lines
        if ln["cy"] < page_h * 0.35 and ln["cx"] > page_w * 0.55 and len(clean_text(ln["text"])) <= 4
    ]
    best = ("", 0.0)
    for ln in zone:
        _wid, name, score = match_worker(ln["text"], workers)
        if score > best[1]:
            best = (name, score)
    return best[0] if best[1] >= 0.7 else ""


def extract_sheet(
    lines: list[dict],
    page_w: int,
    page_h: int,
    ym: str,
    dicts: dict[str, list],
    gray=None,
) -> dict:
    dicts = {**EMPTY_DICTS, **(dicts or {})}
    layout = build_layout(lines, page_w, page_h, gray)
    # 数量列补检：整页检测器对「一格只写一个 1」的细笔画是无解的，砍格单独识别
    if gray is not None:
        try:
            injected = inject_probe(layout, probe_digit_cells(gray, layout))
            if injected:
                layout["probe_count"] = injected
        except Exception:  # noqa: BLE001 - 补检属增强，失败不影响主链路
            pass
    header_idx = layout["header_index"]
    header_band_y = 0.0
    if header_idx >= 0:
        rows = [ln for ln in lines]
        # 表头行平均 y，用于限定表头区
        matched = [ln for ln in rows if clean_text(ln["text"]) in HEADER_LABELS]
        if matched:
            header_band_y = min(ln["cy"] for ln in matched)

    header, header_conf = extract_header(lines, header_band_y)

    # 表头同样走字典收敛，产品代号/材料写法不统一（YUJ-18BZ / YUJ18BZ）需归一
    prod_raw = header.get("product_name") or header.get("product_code") or ""
    _pdc, prod_std, prod_score, prod_exact = resolve_entry(prod_raw, dicts["products"])
    if prod_std and not prod_exact:
        header["product_raw"] = prod_raw
        header["product_name"] = prod_std
    mat_raw = header.get("material") or ""
    _mdc, mat_std, mat_score, mat_exact = resolve_entry(mat_raw, dicts["materials"])
    if mat_std and not mat_exact:
        header["material_raw"] = mat_raw
        header["material"] = mat_std
    header["dict_match"] = {
        "product": round(prod_score, 4),
        "material": round(mat_score, 4),
    }

    # 表头写「产品名称」还是「产品代号」两种都有，明细默认值要一并兜住
    defaults = {
        "product_name": header.get("product_name") or header.get("product_code") or "",
        "batch_no": header.get("batch_no", ""),
        "part_name": header.get("part_name", ""),
        "material": header.get("material", ""),
    }
    items = extract_items(layout, dicts, ym, defaults)
    # 单件工时格复查：整页读错时用裁格重识别纠一次，有勾稽证据才自动改
    _recheck_unit_hours(gray, items)

    header["worker_hint"] = _guess_worker_from_top(lines, page_w, page_h, dicts["workers"])
    header["form_title"] = next(
        (ln["text"] for ln in lines if "路线单" in ln["text"] or "工时" in ln["text"]), ""
    )
    header["company"] = next(
        (ln["text"] for ln in lines if "公司" in ln["text"] and len(ln["text"]) > 8), ""
    )

    return {"header": header, "header_conf": header_conf, "items": items, "layout": layout}


def sheet_month_of(header: dict, items: list[dict], default_ym: str) -> str:
    """从物料日期推断单据所属月份（用于核对期归属校验）。"""
    for it in items:
        if it.get("finish_date"):
            return str(it["finish_date"])[:7]
    return default_ym
