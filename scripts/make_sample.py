"""生成合成工时单照片，用于端到端自测（无真实照片时的临时替代）。

版式完全来自 scripts/form_template.py（＝车间现行空白加工路线单），
所以「自测能过」就等于「真表能过」，不会再出现模板与真表两套版式的情况。

用法：python scripts/make_sample.py [数量]
输出：data/samples/sample_*.jpg（约 1600px 宽，模拟手机拍照）
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

from PIL import Image, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))

import form_template as ft  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
OUT = BASE / "data" / "samples"
OUT.mkdir(parents=True, exist_ok=True)

# 每张样本：表头 + 明细。字段键与 form_template.COLUMNS / HEADER_ROWS 一一对应。
SAMPLES: list[dict] = [
    {
        "header": {
            "dispatcher": "张喜军",
            "batch_no": "河北凯诺",
            "product_code": "GHL-2-18.5",
            "product_name": "双层炼药机",
        },
        "rows": [
            {
                "process": "切刀焊",
                "qty": "8",
                "unit_hours": "0:45:00",
                "declared_total": "6:00:00",
                "worker_raw": "赵永刚",
                "qualified_qty": "8",
                "inspector": "杜文杰",
                "finish_date": "9.10",
            }
        ],
        "footer": {"plan": "张喜军", "date": "2026.9"},
    },
    {
        "header": {
            "dispatcher": "张喜军",
            "product_code": "YUJ-18BZ",
            "product_name": "YUJ-18BZ制丸机",
            "part_no@2": "料包",
            "material": "304",
            "part_count": "4",
        },
        "rows": [
            {
                "process": "镗",
                "qty": "40",
                "unit_hours": "3",
                "declared_total": "120",
                "worker_raw": "陈永安",
                "qualified_qty": "40",
                "inspector": "杜文杰",
                "finish_date": "9.3",
            },
            {
                "process": "镗",
                "qty": "4",
                "unit_hours": "2.5",
                "declared_total": "10",
                "worker_raw": "陈永安",
                "qualified_qty": "4",
                "inspector": "杜文杰",
                "finish_date": "9.5",
            },
        ],
        "footer": {"plan": "张喜军", "date": "2026.9"},
    },
    {
        "header": {
            "dispatcher": "张喜军",
            "batch_no": "26-1",
            "product_code": "YUJ-18BZ",
            "product_name": "YUJ-18BZ制丸机",
            "material": "Q235",
        },
        "rows": [
            {
                "process": "铣",
                "qty": "12",
                "unit_hours": "1.5",
                "declared_total": "18",
                "worker_raw": "王国良",
                "qualified_qty": "12",
                "inspector": "杜文杰",
                "finish_date": "9.8",
            },
            {
                "process": "刨",
                "qty": "2",
                "unit_hours": "4",
                "declared_total": "8",
                "worker_raw": "王国良",
                "qualified_qty": "2",
                "rework_qty": "1",
                "inspector": "符媛媛",
                "finish_date": "9.10",
            },
        ],
        "footer": {"plan": "张喜军", "date": "2026.9"},
    },
    {
        "header": {
            "dispatcher": "张喜军",
            "product_code": "YUJ-18BZ",
            "product_name": "YUJ-18BZ制丸机",
            "part_no@2": "机架",
            "material": "45#",
            "part_count": "1",
        },
        "rows": [
            {
                "process": "电工",
                "qty": "1",
                "unit_hours": "6",
                "declared_total": "6",
                "worker_raw": "马永刚",
                "qualified_qty": "1",
                "inspector": "杜文杰",
                "finish_date": "9.18",
            }
        ],
        "footer": {"plan": "张喜军", "date": "2026.9"},
    },
]

PHOTO_W = 1600  # 模拟手机拍照后的宽度


def _photo_effect(img: Image.Image, seed: int) -> Image.Image:
    """轻微倾斜 + 纸张底色 + 噪点 + 柔焦，贴近手机拍照的成像。"""
    rnd = random.Random(seed)
    paper = Image.new("RGB", img.size, (247, 244, 238))
    img = Image.blend(img, paper, 0.12)
    img = img.rotate(rnd.uniform(-1.1, 1.1), resample=Image.BICUBIC, fillcolor=(243, 240, 234), expand=False)
    w, h = img.size
    k = PHOTO_W / w
    img = img.resize((PHOTO_W, int(h * k)), Image.LANCZOS)
    img = img.filter(ImageFilter.GaussianBlur(0.45))
    return img


def draw_sample(idx: int, spec: dict, seed: int) -> Path:
    img = ft.render(spec, dpi=300)
    img = _photo_effect(img, seed)
    path = OUT / f"sample_{idx:02d}.jpg"
    img.save(path, quality=90)
    return path


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else len(SAMPLES)
    for i in range(n):
        print(draw_sample(i + 1, SAMPLES[i % len(SAMPLES)], seed=100 + i))


if __name__ == "__main__":
    main()
