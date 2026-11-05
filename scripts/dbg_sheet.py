"""调试：打印某张单据原图的 OCR 行、列定义与归行结果。用法：python scripts/dbg_sheet.py <image_id>"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Image as DbImage  # noqa: E402
from app.ocr.engine import recognize  # noqa: E402
from app.ocr.layout import build_layout, group_rows  # noqa: E402


def main() -> None:
    iid = int(sys.argv[1])
    db = SessionLocal()
    img = db.get(DbImage, iid)
    db.close()
    path = img.processed_path or img.path
    with Image.open(path) as im:
        gray = np.array(im.convert("L"))
        w, h = im.size
    lines = recognize(path, w, h)
    print(f"lines={len(lines)} page={w}x{h}")
    for ln in lines:
        print(f"  y={ln['bbox'][1]:4d}-{ln['bbox'][3]:4d} x={ln['bbox'][0]:4d}-{ln['bbox'][2]:4d} "
              f"cx={ln['cx']:6.1f} cy={ln['cy']:6.1f} s={ln['score']:.2f} {ln['text']!r}")
    lay = build_layout(lines, w, h, gray)
    print("\nheader_index=", lay["header_index"], "header_bottom=", round(lay["header_bottom"], 1))
    print("columns:")
    for c in lay["columns"]:
        print("  ", c)
    print("\ncells:")
    for row in lay["cells"]:
        idx = row["row_index"]
        parts = {k: v["text"] for k, v in row["cells"].items()}
        cy = np.mean([ln["cy"] for ln in group_rows(lines)[idx]]) if idx < len(group_rows(lines)) else 0
        print(f"  row#{idx} cy={cy:6.1f} {parts}")


if __name__ == "__main__":
    main()
