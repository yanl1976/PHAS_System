"""验证：印在纸面右上角的单号，OCR 到底读不读得出来。

M1 的整个前提是这句话 ——「单号是印刷体黑体，比手写稳」。如果没有实测过，
`qr.find_form_nos()` 就只是「按设想写的正则」，真到现场可能一个都读不到，
而识别侧照样把单标成 recognized 入账，重复计工时那个洞依然敞着。

做法：不出库、不查库，直接
    版式渲染 → 贴码（qr_stamp） → 存 PNG → 真跑 PaddleOCR → find_form_nos
所以它只回答一个问题：**这张纸读不读得出来**。

用法：
    .venv/Scripts/python.exe scripts/check_form_no_ocr.py
    .venv/Scripts/python.exe scripts/check_form_no_ocr.py RG2611-00042   # 指定单号
    .venv/Scripts/python.exe scripts/check_form_no_ocr.py --dpi 200      # 低分辨率也测一遍
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

from app.domain import qr  # noqa: E402
from app.ocr import engine as ocr_engine  # noqa: E402
from app.render import qr_stamp  # noqa: E402
from app.render import route_form as form  # noqa: E402

CARD = {
    "product": "验证产品",
    "part": "整机装配",
    "code": "01.02.003",
    "material": "304",
    "qty_per_unit": 1,
    "print_qty": 5,
    "steps": [
        {"process_name": "下料", "unit_hours": 0.5, "seq": 10},
        {"process_name": "车削", "unit_hours": 2.0, "seq": 20},
        {"process_name": "钳工修配", "unit_hours": 1.5, "seq": 30},
    ],
}
TASK = {"qty": 5, "batch_no": "B-VERIFY", "dispatcher": "验证"}


def run(form_no: str, dpi: int) -> bool:
    from app.services import card_print as CP

    values = CP.card_values(CARD, task=TASK)
    pages = form.render_pages(values, dpi=dpi)
    page = pages[0]
    qr_stamp.stamp(page, form_no, "0a0b0c0d", dpi)

    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / f"sheet_{dpi}.png"
        page.save(png)
        with PILImage.open(png) as im:
            w, h = im.size
            gray = np.asarray(im.convert("L"), dtype=np.uint8)
        lines = ocr_engine.recognize(str(png), w, h)
        found = qr.find_form_nos(lines)

        # 只打印右上角区域（码在 266mm/3mm 附近）读到了什么，便于判断是「没认出」
        # 还是「认出成别的字」
        right_top = [
            ln["text"] for ln in lines
            if (ln.get("cx") or 0) > w * 0.72 and (ln.get("cy") or 0) < h * 0.16
        ]
        print(f"  dpi={dpi:>3}  行数={len(lines):>3}  单号识别={found}")
        print(f"        右上角读数：{right_top}")
        _ = gray  # 保留：将来若要按灰度定位码区会用到
        return form_no in found


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dpi_arg = next((a for a in sys.argv[1:] if a.startswith("--dpi")), "")
    dpi = int(dpi_arg.split("=", 1)[1]) if "=" in dpi_arg else 300
    form_no = args[0] if args else "RG2611-00042"

    print(f"验证印刷体单号 OCR 可读性 · 单号 {form_no}")
    ok = True
    for d in sorted({dpi, 200}):
        print(f"\n[{d} dpi]")
        try:
            ok = run(form_no, d) and ok
        except Exception as exc:  # pragma: no cover - 环境问题要有明确报错
            print(f"  [FAIL] {d} dpi 跑失败：{exc}")
            ok = False

    print("\n" + "=" * 60)
    if ok:
        print("结论：纸面单号可被 OCR 读出，识别侧互认这条链成立。")
        print("注意：这是版式渲染图（干净、正射）。现场照片会歪、会暗，")
        print("      所以识别侧的绑定仍然只做「命中才认」，读不到就保持原状。")
        return 0
    print("结论：**读不出来**，识别侧绑定实际不会触发。")
    print("下一步：把一张真实现场的预印单照片放进来重跑；若仍读不到，")
    print("      需要调大单号字号或改用「码区定位 + 小图二次识别」。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
