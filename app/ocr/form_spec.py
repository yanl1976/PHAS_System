"""标准《加工路线单》的几何定义（毫米）—— 识别与版式渲染共用同一份数字。

为什么单独一个模块：
    列边界不能只靠「相邻列头锚点的中点」。锚点是表头**文字的中心**，而格子宽度
    差别极大（序号 7.7mm / 工序等级 65.3mm），中点会落进宽格内部靠左的位置——那里
    正是手写工序名的起笔处，结果整列工序被判给序号列，明细工序全空。真实照片如此，
    合成样本也如此，属于必现问题。

    标准表是固定的，所以最可靠的做法是：用「本表已知的相对列宽」去拟合现场照片的
    横坐标（只解缩放和偏移两个未知数），再把拟合出的列边界作为最终边界。这就是
    「按模板识别」的字面含义。

本文件与 scripts/form_template.py 渲染出的空白表必须是同一版式：改宽度、改列名、
改表头格子，两边自动一起变（后者直接 import 本模块）。
"""
from __future__ import annotations

# 表格宽度（A4 横向 297mm，左右各留 15mm）
TABLE_W = 267.0

# (列头文字, 宽度mm, 字段键)
# 字段键与 app/ocr/layout.py 的 COLUMN_ANCHORS / COLUMN_RESERVES 对应；
# 「序号」「数量」是占位列：只占 x 区间，不入库（数量仅在合格列空时作件数兜底）。
COLUMNS: list[tuple[str, float, str]] = [
    ("序号", 7.7, "_idx"),
    ("工序等级", 65.3, "process"),
    ("数量", 13.3, "_qty"),
    ("单件", 19.6, "unit_hours"),
    ("合计", 30.4, "declared_total"),
    ("操作者", 29.6, "worker_raw"),
    ("合格", 12.7, "qualified_qty"),
    ("返修", 12.4, "rework_qty"),
    ("废品", 19.5, "scrap_qty"),
    ("检查员", 27.1, "inspector"),
    ("日期", 16.1, "finish_date"),
    ("备注", 13.3, "remark"),
]

# 主表头：跨列母表头 -> 覆盖的列下标区间 [start, end)
COL_GROUPS: list[tuple[str, int, int]] = [
    ("序号", 0, 1),
    ("工序等级", 1, 2),
    ("数量", 2, 3),
    ("定额工时", 3, 5),
    ("操作者", 5, 6),
    ("加工结果", 6, 9),
    ("检查员", 9, 10),
    ("日期", 10, 11),
    ("备注", 11, 12),
]

# 竖排印刷的两字窄列
VERTICAL_LABELS = {"序号", "数量"}

# 表头键值区：4 组「标签格 + 值格」。第 2/4/6/8 条分界线与明细列边界重合
HEADER_EDGES = [0.0, 23.5, 46.5, 86.3, 136.3, 165.9, 210.5, 237.6, 267.0]

HEADER_ROWS: list[list[tuple[str, str]]] = [
    [("计调员", "dispatcher"), ("产品批号", "batch_no"), ("件号", "part_no"), ("类别", "category")],
    [("材料员", "picker"), ("产品代号", "product_code"), ("件号", "part_no"), ("类别", "category")],
    [("投产台数", "machine_count"), ("产品名称", "product_name"), ("材料", "material"), ("加工件数", "part_count")],
]


def edges() -> list[float]:
    """列边界（相对表格左沿，mm），长度 = 列数 + 1。"""
    out = [0.0]
    for _name, width, _key in COLUMNS:
        out.append(out[-1] + width)
    return out


def keys() -> list[str]:
    return [key for _name, _w, key in COLUMNS]


def rel_centers() -> dict[str, float]:
    """各列中心相对表格宽度的比例（0~1），供现场照片拟合用。"""
    out: dict[str, float] = {}
    e = edges()
    for i, (_name, _w, key) in enumerate(COLUMNS):
        out[key] = (e[i] + e[i + 1]) / 2 / TABLE_W
    return out


def rel_edges() -> list[float]:
    return [v / TABLE_W for v in edges()]
