"""数量列单格补检：整页检测器漏掉的细笔画数字，靠「砍格 + 只跑识别」捞回来。

背景（实测，勿凭直觉调参）：
表格里「合格 / 返修 / 废品」这类数量格经常只写一个 `1`。它是一个孤立、细长、
紧贴格线的竖笔画，DB 文本检测器会把这类形状判成表格线：同一张单据在
text_det_limit_side_len 取 960 / 1536 / 1920、并把 det_thresh / box_thresh 放宽到
0.15 / 0.3 之后，四组参数仍然 **完全没有检出** 该笔画。所以靠「加大边长、降阈值」
是死路，必须绕开检测网络。

做法：既然版面里已经有列边界与行高，就把目标单元格直接裁下来，去掉格线后送
识别网络（rec-only）。实测同一个格子：含表头字时误识成 `9`，按行带精确裁剪并去线后
稳定输出 `1`（score 0.57）。

防误报：
- 只处理整页 OCR 在该格**完全没有文本**的格子，已有文本的一律不动；
- 只接受纯数字结果，且置信度不低于阈值（默认 0.5）；
- 裁格上界由 layout 的 header_bottom 兜住，避免把「合格」这种印刷表头当数据；
- 墨量太少的空格子直接跳过，不进识别，省时间也避免噪点被读成数字。
"""
from __future__ import annotations

import logging
import os
import re

import numpy as np

from .engine import cell_probe_enabled, recognize_cell
from .normalize import clean_text

log = logging.getLogger("worktime.cellprobe")

DIGIT_KEYS = ("qualified_qty", "rework_qty", "scrap_qty")

# 该行本来就得是一条明细，才值得补检。
# 页面底部的「计划调度：张喜军」签名区也会被归进某些列，若不加这道门槛，
# 补检会把它变成一条合格件数=1 的假明细。
ROW_ANCHOR_KEYS = {"unit_hours", "declared_total", "real_hours", "worker_raw", "process", "finish_date"}

# 手写数量就是 1~4 位整数，偶尔带 .5；超出这个形状的一律不采信
_DIGIT_RE = re.compile(r"^\d{1,4}(?:\.\d{1,2})?$")

# 单张单据最多补检多少个格子，防止极端版面把识别时间拖长
_MAX_PROBES = int(os.getenv("WT_CELL_PROBE_MAX", "40"))

# 单元格内缩，避开格线本身
_PAD_X = 4
_PAD_Y = 3

# 墨量占比低于此值视为空格子（一个手写数字通常占 3%~10%）
_MIN_INK = 0.006

# 去线后裁剪框边缘允许残留的墨量；超过即认为笔画被框切断，结果不可信
_MAX_EDGE_INK = 0.02

# ---- 形状兜底（处理「笔画压在表头里、常规裁剪必然切断」的情况）----
# 手写 1 就是一根细长竖线，即便压在印刷表头「合格」上，也能靠连通域形状把它
# 从表头字里挑出来，再把这一小块单独送识别。
_SHAPE_MIN_AREA = 25          # 连通域最小像素面积，滤掉噪点
_SHAPE_MIN_H = 20             # 最小字高（像素）
_SHAPE_ASPECT = 2.2           # 高 / 宽，竖笔画的门槛；表头字的横竖部件都过不了
_SHAPE_MAX_W_RATIO = 0.6      # 宽 / 格宽上限
_SHAPE_CORE_OVERLAP = 0.55    # 该连通域纵向落在数据行核心区内的比例
_SHAPE_MIN_CONF = 0.45        # 形状兜底本来就是低置信场景，门槛略低于常规
_SHAPE_ONLY = {"1"}           # 只认「一根竖笔画」；其它数字带横画，常规路径本来就看得见


def _min_conf() -> float:
    try:
        return float(os.getenv("WT_CELL_PROBE_CONF", "0.5"))
    except ValueError:
        return 0.5


def _drop_lines(img: np.ndarray) -> np.ndarray:
    """去掉贯穿整块的长横线/长竖线（即格线），保留笔画。

    没有 opencv 时退化为原图——识别网络对干净的手工裁剪仍有一定容忍度，
    只是准确率略低，不影响主流程。
    """
    try:
        import cv2  # type: ignore
    except Exception:  # pragma: no cover
        return img
    return 255 - _bin_drop_lines(img)


def _bin_drop_lines(img: np.ndarray) -> np.ndarray:
    """同上，但返回二值图（墨=255），供连通域分析使用。"""
    import cv2  # type: ignore

    bw = cv2.adaptiveThreshold(
        img, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 31, 15
    )
    h, w = img.shape[:2]
    hk = cv2.getStructuringElement(cv2.MORPH_RECT, (max(12, w * 3 // 4), 1))
    vk = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(12, h * 3 // 4)))
    lines = cv2.morphologyEx(bw, cv2.MORPH_OPEN, hk) | cv2.morphologyEx(bw, cv2.MORPH_OPEN, vk)
    lines = cv2.dilate(lines, np.ones((3, 3), np.uint8), iterations=1)
    return cv2.subtract(bw, lines)


def _upscale(img: np.ndarray, factor: int = 3) -> np.ndarray:
    try:
        import cv2  # type: ignore
    except Exception:  # pragma: no cover
        return img
    h, w = img.shape[:2]
    # rec 网络内部会把高缩到 48，放大到约 96~144 高即可，再大只增加耗时
    f = max(1, min(factor, int(160 / max(1, h))))
    if f <= 1:
        return img
    return cv2.resize(img, None, fx=f, fy=f, interpolation=cv2.INTER_CUBIC)


def _edge_ink(img: np.ndarray, band: int = 2) -> float:
    """裁剪框四条边上残留墨水的最大占比（只看去线后的图）。

    表头下沿与首行手写常常重叠（实测「合格」印刷字 y 到 970，而 7.15 的签名框
    从 928 就开始了），此时裁出来的框会把笔画切掉一截。被切掉一截的笔画既可能
    识别成别的数字（`1` 下半截被读成 `7`，score 0.93），也可能整片误判，
    因此只要边缘还有墨，就认为笔画不完整，一律不采信——宁可留空让人工补录。
    """
    dark = img < 160
    if dark.size == 0:
        return 1.0
    return float(
        max(
            dark[:band].mean(),
            dark[-band:].mean(),
            dark[:, :band].mean(),
            dark[:, -band:].mean(),
        )
    )


def _row_band(cell_row: dict, header_bottom: float, min_h: float) -> tuple[float, float, float] | None:
    """该数据行的纵向范围，返回 (行带原始上沿, 下沿, 避开表头后的核心上沿)。

    原始上沿会伸进表头区——首行手写常常压在印刷表头上，形状兜底需要这个范围；
    核心上沿则是常规裁剪用的，必须躲开表头字。
    """
    boxes = [c["bbox"] for c in cell_row.get("cells", {}).values() if c.get("bbox")]
    if not boxes:
        return None
    raw_y0 = min(float(b[1]) for b in boxes)
    raw_y1 = max(float(b[3]) for b in boxes)
    y0 = max(raw_y0, header_bottom + 2)
    y1 = raw_y1
    if y1 - y0 < min_h:
        cy = (y0 + y1) / 2
        y0, y1 = cy - min_h / 2, cy + min_h / 2
    if y1 - y0 < 12:
        return None
    return (raw_y0, y1, y0)


def _grow(blocks: list[list[int]], cell_w: int) -> list[list[int]]:
    """把同一根笔画被切断/写斜造成的碎片并回一块。

    手写 1 常常是斜的，被格线拦腰一抹后，上下两段在 x 上还错开十几像素——光靠
    形态学闭运算接不上。这里按「纵向间隙 ≤6px、横向错开 ≤1/4 格宽」判同一笔，
    并在每一次合并后重新校验形状（高宽比、宽度上限），不让表头字或格线残片
    顺着合并链混进来。
    """
    out = [list(b) for b in blocks]
    max_w = _SHAPE_MAX_W_RATIO * cell_w
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                a, b = out[i], out[j]
                gap = max(a[1], b[1]) - min(a[1] + a[3], b[1] + b[3])
                if gap > 6:
                    continue
                dx = max(a[0], b[0]) - min(a[0] + a[2], b[0] + b[2])
                if dx > max(12, 0.25 * cell_w):
                    continue
                nx0, ny0 = min(a[0], b[0]), min(a[1], b[1])
                nx1 = max(a[0] + a[2], b[0] + b[2])
                ny1 = max(a[1] + a[3], b[1] + b[3])
                nw, nh = nx1 - nx0, ny1 - ny0
                if nw > max_w or nh < _SHAPE_MIN_H or nh / max(1, nw) < _SHAPE_ASPECT:
                    continue
                out[i] = [nx0, ny0, nw, nh]
                del out[j]
                changed = True
                break
            if changed:
                break
    return out


def _num(text: object) -> float | None:
    try:
        return float(str(text).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _expected_qty(cell_row: dict) -> float | None:
    """用合计（或实时工时）÷ 单件工时反推数量，作为形状判定的独立旁证。"""
    cells = cell_row.get("cells") or {}
    unit = _num(cells.get("unit_hours", {}).get("text"))
    total = _num(cells.get("declared_total", {}).get("text"))
    if total is None:
        total = _num(cells.get("real_hours", {}).get("text"))
    if not unit or unit <= 0 or not total or total <= 0:
        return None
    return total / unit


def _probe_by_shape(
    gray: np.ndarray,
    ix0: int,
    ix1: int,
    raw_y0: float,
    raw_y1: float,
    header_bottom: float,
    cell_row: dict,
) -> dict | None:
    """连通域形状兜底：把压在表头里的那一根竖笔画认成 1。

    针对的场景（真实单据 e1d102461d0ed843）：首行手写的 `1` 纵向压在印刷表头
    「合格」下沿上，常规裁剪为了躲表头必然把笔画切断，edge 检查于是主动放弃；
    但被切掉的只是"坐标系"，笔画本身完整且形状唯一——细长、绝大部分落在数据行里。

    判据（缺一不可）：
    1. 去线后恰有 **一个** 高宽比 ≥2.2、宽度不超过格宽一半、纵向 ≥55% 落在数据行
       核心区的连通域（表头字的竖笔都停在表头内，过不了这一条）；
    2. 把这一小块单独送识别，结果必须是 `1`，置信度 ≥0.45；
    3. 若该行有合计/实时工时，反推出的数量不应与 1 明显矛盾（矛盾只降分、不否决，
       因为单件工时本身也是识别值，拿它硬否一个视觉上明确的 1 不划算）。

    只接受 `1` 是刻意的：其它数字都带横向笔画，整页检测器本来就能看见，兜底
    不需要也不应该替它们做主；把范围放宽到 7 会跟表格竖线残片混淆，误报代价远大于漏检。
    """
    try:
        import cv2  # type: ignore
    except Exception:  # pragma: no cover
        return None

    band_h = max(1.0, raw_y1 - raw_y0)
    # 上沿卡在表头区下沿的下方一点：既能收进压线的笔画上半截，又不会把印刷表头字
    # 一起卷进来（卷进来后闭运算会把表头竖笔和手写笔画粘成一块，判据全废）。
    ty0 = max(0, int(header_bottom - 0.15 * band_h))
    # 下沿只取到行带下沿：再往下就会把下一条横线/下一行的笔迹卷进来，
    # 实测多扩 5px 就会在格内凭空多出一个细长连通域，把唯一性判据打掉。
    ty1 = min(int(gray.shape[0]), int(raw_y1))
    if ty1 - ty0 < 16:
        return None
    crop = gray[ty0:ty1, ix0:ix1]
    if crop.size == 0:
        return None
    ch, cw = crop.shape[:2]
    clean = _bin_drop_lines(crop)
    if not clean.any():
        return None
    # 去线时横线会把竖笔画拦腰抹掉 1~3px，一根 1 因此裂成上下两段；先纵向闭运算接回去，
    # 否则拿到的是半截笔画，rec 会读成 X / 7 之类（实测 score 0.50 读成 'X'）。
    clean = cv2.morphologyEx(clean, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5)))

    core0 = max(float(ty0), header_bottom + 2)
    core1 = float(ty1)
    blocks: list[list[int]] = []
    n, _, stats, _ = cv2.connectedComponentsWithStats(clean, 8)
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < _SHAPE_MIN_AREA or h < _SHAPE_MIN_H:
            continue
        blocks.append([x, y, w, h])
    raw = [tuple(b) for b in blocks]
    blocks = _grow(blocks, cw)

    cands: list[tuple[int, int, int, int]] = []
    for x, y, w, h in blocks:
        if h / max(1, w) < _SHAPE_ASPECT:
            continue
        if w > _SHAPE_MAX_W_RATIO * cw:
            continue
        ay0, ay1 = ty0 + y, ty0 + y + h
        # 必须真的压在表头边界上：顶端伸进表头区，主体落在数据行里。
        # 这一条同时排掉了格线残片、下一行笔迹等「整根都在数据行内」的干扰项，
        # 而没压表头的数字本来常规路径就能识别，不需要兜底。
        if ay0 >= header_bottom:
            continue
        overlap = max(0.0, min(ay1, core1) - max(ay0, core0))
        if overlap / h < _SHAPE_CORE_OVERLAP:
            continue
        cands.append((x, y, w, h))

    if len(cands) != 1:
        # 0 个＝格子里没有竖笔画；多个＝有歧义（可能是别的字被线切碎），一律不猜
        log.debug(
            "形状兜底窗口 y%d-%d %dx%d core=%d cands=%s raw=%s merged=%s",
            ty0, ty1, cw, ch, int(core0), cands, raw, [tuple(b) for b in blocks],
        )
        return None

    x, y, w, h = cands[0]
    sub = clean[max(0, y - 2) : y + h + 2, max(0, x - 2) : x + w + 2]
    if sub.size == 0:
        return None
    # 四周留白：rec 网络对贴边的图很敏感，留白后更稳
    pad = np.zeros((sub.shape[0] + 10, sub.shape[1] + 10), dtype=np.uint8)
    pad[5 : 5 + sub.shape[0], 5 : 5 + sub.shape[1]] = sub
    factor = max(1, int(120 / max(1, pad.shape[0])))
    big = cv2.resize(255 - pad, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)

    try:
        text, score = recognize_cell(big)
    except Exception as exc:  # noqa: BLE001
        log.warning("形状兜底识别失败 row=%s: %s", cell_row.get("row_index"), exc)
        return None

    text = clean_text(text or "").replace(" ", "")
    if text not in _SHAPE_ONLY or score < _SHAPE_MIN_CONF:
        log.info(
            "形状兜底不采信 row=%s text=%r score=%.2f（形状像竖笔画但识别不符）",
            cell_row.get("row_index"), text, score,
        )
        return None

    expect = _expected_qty(cell_row)
    if expect is not None:
        if abs(expect - 1.0) <= 0.25:
            score = min(0.95, score + 0.2)  # 形状与算术互相印证，提分
        else:
            log.info(
                "形状兜底与合计反推不符 row=%s 反推=%.2f，仍采信但降分", cell_row.get("row_index"), expect
            )
            score = _SHAPE_MIN_CONF

    log.info(
        "形状兜底命中 row=%s text=%s score=%.2f bbox=(%d,%d,%dx%d)",
        cell_row.get("row_index"), text, score, ix0 + x, ty0 + y, w, h,
    )
    return {
        "text": text,
        "score": round(float(score), 4),
        "bbox": [ix0 + x, ty0 + y, ix0 + x + w, ty0 + y + h],
        "probe": True,
        "shape": True,
    }


def probe_digit_cells(gray: np.ndarray | None, layout: dict) -> dict[int, dict[str, dict]]:
    """返回 {row_index: {key: cell}}，cell 形如 {"text","score","bbox","probe":True}。"""
    if gray is None or not cell_probe_enabled():
        return {}
    gray = np.asarray(gray)
    if gray.ndim == 3:
        gray = gray[..., 0]

    cols = {
        c["key"]: (float(c["x0"]), float(c["x1"]))
        for c in layout.get("columns") or []
        if c.get("key") in DIGIT_KEYS
    }
    if not cols:
        return {}

    page_h = float(layout.get("page_h") or gray.shape[0])
    header_bottom = float(layout.get("header_bottom") or 0.0)
    min_h = max(40.0, page_h * 0.028)
    conf_floor = _min_conf()

    out: dict[int, dict[str, dict]] = {}
    probes = 0
    for cell_row in layout.get("cells") or []:
        have = set(cell_row.get("cells", {}))
        if not (have & ROW_ANCHOR_KEYS):
            continue
        todo = [(k, v) for k, v in cols.items() if k not in have]
        if not todo:
            continue
        band = _row_band(cell_row, header_bottom, min_h)
        if not band:
            continue
        raw_y0, raw_y1, core_y0 = band
        y0, y1 = int(core_y0) + _PAD_Y, int(raw_y1) - _PAD_Y
        for key, (x0, x1) in todo:
            if probes >= _MAX_PROBES:
                break
            ix0, ix1 = int(x0) + _PAD_X, int(x1) - _PAD_X
            if ix1 - ix0 < 10 or y1 - y0 < 16:
                continue
            crop = gray[y0:y1, ix0:ix1]
            if crop.size == 0:
                continue
            # 墨量：先看有无笔画，空格子不进识别
            if float((crop < 160).mean()) < _MIN_INK:
                continue
            probes += 1
            hit: dict | None = None
            try:
                prepared = _upscale(_drop_lines(crop))
            except Exception as exc:  # noqa: BLE001
                log.warning("单格补检预处理失败 key=%s row=%s: %s", key, cell_row.get("row_index"), exc)
                prepared = None
            edge = _edge_ink(prepared) if prepared is not None else 1.0
            if prepared is not None and edge <= _MAX_EDGE_INK:
                try:
                    text, score = recognize_cell(prepared)
                except Exception as exc:  # noqa: BLE001
                    log.warning("单格补检失败 key=%s row=%s: %s", key, cell_row.get("row_index"), exc)
                    text, score = "", 0.0
                text = clean_text(text or "").replace(" ", "")
                if _DIGIT_RE.match(text) and score >= conf_floor:
                    hit = {
                        "text": text,
                        "score": round(float(score), 4),
                        "bbox": [ix0, y0, ix1, y1],
                        "probe": True,
                    }
                elif _DIGIT_RE.match(text):
                    log.info("单格补检置信度不足 %s row=%s text=%r score=%.2f",
                             key, cell_row.get("row_index"), text, score)
            else:
                log.info(
                    "单格补检裁剪被切断 key=%s row=%s edge=%.3f，转形状兜底",
                    key, cell_row.get("row_index"), edge,
                )
            # 常规路径没拿到（笔画被表头切断 / 识别成非数字 / 置信度不足）时，
            # 用连通域形状再判一次：这就是「明明写着 1 却识别不出来」的收口。
            if hit is None:
                hit = _probe_by_shape(gray, ix0, ix1, raw_y0, raw_y1, header_bottom, cell_row)
            if hit is None:
                continue
            out.setdefault(int(cell_row["row_index"]), {})[key] = hit
    if out:
        log.info("单格补检命中 %d 行 / %d 格", len(out), sum(len(v) for v in out.values()))
    return out


def inject(layout: dict, probed: dict[int, dict[str, dict]]) -> int:
    """把补检结果写回 layout 的 cells，返回注入格数。"""
    if not probed:
        return 0
    n = 0
    for cell_row in layout.get("cells") or []:
        hit = probed.get(int(cell_row.get("row_index", -1)))
        if not hit:
            continue
        for key, cell in hit.items():
            if key not in cell_row["cells"]:
                cell_row["cells"][key] = cell
                n += 1
    return n


# ---------------------------------------------------------------- 单件工时复查
# 与上面的「空格补检」不同：这里格子里**本来就有文本**，只是整页识别读错了。
# 实测单据 7 印刷体「0:45:00」被读成「0:48:00」——格线斜着穿过 5 的肚子，
# 整页尺度下像一个 8（置信度还高达 0.97，比正确答案更自信）。
# 把这一格裁下来、去线、放大后单独送识别网络，能读成 0:45:00（score 0.93）。
#
# 两个实测出来的坑（别凭直觉改）：
# 1) **padding 奇偶会翻转结果**：同一格 pad=0/2/4 得 0:45:00，pad=1/3/5 得 0:48:00。
#    裁剪框差 1px，斜线残留量就不同。所以不能押单个 pad，必须多 pad 投票。
# 2) **去线会把标点一起抹掉**：`2.5` 去掉格线后变成 `25`、`0:45:00` 变成 `04500`。
#    冒号和小数点紧贴格线，被线掩膜膨胀吃掉。所以候选的标点形态必须与整页原文一致，
#    否则一律不采信（`2.5 → 25` 这类假候选就是这么挡掉的）。
#
# 姓名格（草书「杜文杰」读成「定生」「机生」）与日期格（9.10 的「1」只有 2px 高）
# 在这个尺度下救不回来：试遍 pad 0~12 × 放大 2~6 × 去线与否，外加竖向/横向加粗、
# 腐蚀 1~2 像素、字符级切分后把那一笔单独送识别（读成 't' 0.29），共 100+ 组合，
# **没有一组读出 9.10**——整页结果始终是「9.0」，解析不出日期。所以只复查工时。
# 日期格的补救放在别处：原文存进 finish_date_raw，核对页显示「识别到 9.0」并给一格
# 可点开放大的裁图（crop?field=finish_date），人眼一秒就认出来了。
_HOURS_VOTE_PADS = (0, 2, 4)
_MAX_RECHECK = int(os.getenv("WT_CELL_RECHECK_MAX", "8"))


def _recheck_min_conf() -> float:
    try:
        return float(os.getenv("WT_CELL_RECHECK_CONF", "0.6"))
    except ValueError:
        return 0.6


def shape_kept(text: str, hint: str) -> bool:
    """候选的标点形态是否与整页原文一致。

    原文有冒号/小数点，候选也得有——否则说明去线把标点抹了，是残缺读数。
    """
    for mark in (":", "."):
        if mark in (hint or "") and mark not in (text or ""):
            return False
    return True


def _recheck_one(
    gray: np.ndarray, bbox: list[int], pad: int, drop: bool
) -> tuple[str, float] | None:
    from .engine import recognize_cell

    h, w = gray.shape[:2]
    x0, y0, x1, y1 = (int(v) for v in bbox)
    x0, y0 = max(0, x0 - pad), max(0, y0 - pad)
    x1, y1 = min(w, x1 + pad), min(h, y1 + pad)
    if x1 - x0 < 8 or y1 - y0 < 6:
        return None
    crop = gray[y0:y1, x0:x1]
    img = _drop_lines(crop) if drop else crop
    if img is None:
        return None
    try:
        text, score = recognize_cell(_upscale(img, 3))
    except Exception:  # noqa: BLE001 - 复查属增强，异常一律放过
        return None
    text = str(text or "").strip()
    if not text or score < _recheck_min_conf():
        return None
    return text, float(score)


def recheck_hours_cell(
    gray: np.ndarray | None, bbox: list[int], hint: str = ""
) -> tuple[str, float] | None:
    """把单件工时格裁出来重识别，返回 (文本, 置信度)；不可信时返回 None。

    多 pad × 去线/不去线 共 6 次识别投票，平票时偏向去线版本（格线是主要干扰源）。
    调用方负责用 parse_hours 解析，本函数只管「拿到一个更干净的字符串」。
    """
    if gray is None or not bbox:
        return None

    votes: dict[str, list[float]] = {}
    for drop in (True, False):
        for pad in _HOURS_VOTE_PADS:
            got = _recheck_one(gray, bbox, pad, drop)
            if not got:
                continue
            text, score = got
            if not shape_kept(text, hint):
                continue
            votes.setdefault(text, []).append(score)
    if not votes:
        return None

    # 票数优先；平票时保持插入顺序（去线版本先入，即偏向去线）
    best_text, best_scores = max(votes.items(), key=lambda kv: len(kv[1]))
    return best_text, round(sum(best_scores) / len(best_scores), 4)


def recheck_budget_left() -> int:
    """单张单据允许复查的行数（每行 6 次识别，防止极端版面拖长识别时间）。"""
    return max(0, _MAX_RECHECK)


def recheck_enabled() -> bool:
    """总开关：WT_CELL_RECHECK=0 可整块关掉工时复查（出问题时的回退开关）。"""
    return os.getenv("WT_CELL_RECHECK", "1").strip().lower() not in ("0", "false", "off", "no")
