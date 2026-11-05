"""原型：用「同列不能同行」约束切行，验证能否切出手写单据的 7 行。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image as PILImage

from app.db import SessionLocal
from app.models import Image
from app.ocr import layout as L
from app.ocr.normalize import compact


def x_overlap(a: dict, b: dict) -> float:
    a0, a1 = a["bbox"][0], a["bbox"][2]
    b0, b1 = b["bbox"][0], b["bbox"][2]
    inter = max(0.0, min(a1, b1) - max(a0, b0))
    return inter / min(max(1.0, a1 - a0), max(1.0, b1 - b0))


def group_by_x(lines: list[dict], tol_ratio: float = 0.9) -> list[list[dict]]:
    if not lines:
        return []
    heights = [ln["h"] for ln in lines if ln["h"] > 0]
    med_h = float(np.median(heights)) if heights else 20.0
    tol_y = med_h * tol_ratio

    buckets: list[dict] = []
    for ln in sorted(lines, key=lambda x: x["cy"]):
        placed = False
        for b in reversed(buckets[-2:]):
            if any(x_overlap(ln, other) > 0.35 for other in b["lines"]):
                continue
            if b["top"] - tol_y <= ln["cy"] <= b["bottom"] + tol_y:
                b["lines"].append(ln)
                b["top"] = min(b["top"], ln["cy"])
                b["bottom"] = max(b["bottom"], ln["cy"])
                placed = True
                break
        if not placed:
            buckets.append({"lines": [ln], "top": ln["cy"], "bottom": ln["cy"]})
    out = [b["lines"] for b in buckets]
    for r in out:
        r.sort(key=lambda x: x["cx"])
    return out


def main() -> None:
    img_id = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    lines = json.loads((Path("data/diag") / f"lines_{img_id}.json").read_text(encoding="utf-8"))["lines"]

    db = SessionLocal()
    img = db.get(Image, img_id)
    with PILImage.open(img.processed_path) as pim:
        page_w, page_h = pim.size
        gray = np.asarray(pim.convert("L"), dtype=np.uint8)
    db.close()

    old = L.group_rows(lines)
    new = group_by_x(lines)
    print(f"旧算法 {len(old)} 行 -> 新算法 {len(new)} 行\n")

    for i, row in enumerate(new):
        cy = float(np.mean([x["cy"] for x in row]))
        texts = " | ".join(f"{compact(x['text'])}@{x['cy']:.0f}" for x in row)
        print(f"[{i:>2}] cy={cy:>6.1f}  {texts}")


if __name__ == "__main__":
    main()
