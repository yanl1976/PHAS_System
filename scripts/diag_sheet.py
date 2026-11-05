"""诊断单张图片的版面还原过程：OCR 行 -> 视觉行分组 -> 列归属。

用法：
    python scripts/diag_sheet.py 8            # 用缓存的 OCR 结果
    python scripts/diag_sheet.py 8 --ocr      # 强制重跑 OCR

只读，不改数据库。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image as PILImage

from app.db import SessionLocal
from app.models import Image
from app.ocr import engine as ocr_engine
from app.ocr import layout as L
from app.ocr.normalize import compact


def load_lines(img_id: int, force_ocr: bool) -> tuple[list[dict], int, int]:
    cache = Path("data/diag") / f"lines_{img_id}.json"
    db = SessionLocal()
    img = db.get(Image, img_id)
    path = img.processed_path
    db.close()

    with PILImage.open(path) as pim:
        w, h = pim.size

    if cache.exists() and not force_ocr:
        data = json.loads(cache.read_text(encoding="utf-8"))
        return data["lines"], w, h

    lines = ocr_engine.recognize(path, w, h)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"lines": lines}, ensure_ascii=False), encoding="utf-8"
    )
    return lines, w, h


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("image_id", type=int)
    ap.add_argument("--ocr", action="store_true")
    args = ap.parse_args()

    lines, page_w, page_h = load_lines(args.image_id, args.ocr)
    print(f"图片 {args.image_id}  尺寸 {page_w}x{page_h}  OCR 行数 {len(lines)}")

    gray = None
    db = SessionLocal()
    img = db.get(Image, args.image_id)
    with PILImage.open(img.processed_path) as pim:
        gray = np.asarray(pim.convert("L"), dtype=np.uint8)
    db.close()

    rows = L.group_rows(lines)
    print(f"\n=== 视觉行分组（共 {len(rows)} 行）===")
    for i, row in enumerate(rows):
        cy = float(np.mean([x["cy"] for x in row]))
        texts = " | ".join(f"{compact(x['text'])}(y={x['cy']:.0f},h={x['h']:.0f})" for x in row)
        print(f"[{i:>2}] cy={cy:>6.1f} n={len(row)}  {texts}")

    lay = L.build_layout(lines, page_w, page_h, gray=gray)
    print(f"\n=== 列映射（header_index={lay['header_index']}）===")
    for c in lay["columns"]:
        print(f"  {c['key']:<16} x0={c['x0']:>6.0f} x1={c['x1']:>6.0f}")

    print(f"\n=== 明细行单元格归属（共 {len(lay['cells'])} 行）===")
    for r in lay["cells"]:
        cells = {k: compact(v.get("text", "")) for k, v in r["cells"].items()}
        print(f"[{r['row_index']:>2}] {cells}")


if __name__ == "__main__":
    main()
