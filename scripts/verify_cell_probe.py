"""验证数量列单格补检：把某格的数字擦掉、在格内居中重写，确认补检能捞回来。

用法：.venv\\Scripts\\python.exe scripts\\verify_cell_probe.py
只读库、只画在内存图上，不改任何数据。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.db import SessionLocal  # noqa: E402
from app.ocr.extract import extract_sheet  # noqa: E402
from app.services.dicts import dict_bundle  # noqa: E402

SHEET_ID = 1


def load():
    con = sqlite3.connect("data/worktime.db")
    con.row_factory = sqlite3.Row
    row = con.execute("select image_id, ocr_raw_json from sheet where id=?", (SHEET_ID,)).fetchone()
    img = con.execute("select processed_path from image where id=?", (row["image_id"],)).fetchone()
    con.close()
    lines = json.loads(row["ocr_raw_json"])
    path = img["processed_path"]
    return path, lines


def main():
    path, lines = load()
    db = SessionLocal()
    bundle = dict_bundle(db)
    im = Image.open(path)
    gray = np.asarray(im.convert("L"), dtype=np.uint8)

    base = extract_sheet(lines, im.width, im.height, "2026-09", bundle, gray=gray)
    lay = base["layout"]
    print("原结果:", [(it["seq"], it["qualified_qty"], it["unit_hours"]) for it in base["items"]])

    col = next(c for c in lay["columns"] if c["key"] == "qualified_qty")
    row0 = next(
        r for r in lay["cells"]
        if "qualified_qty" in r["cells"] and r["cells"]["qualified_qty"].get("bbox")
    )
    boxes = [c["bbox"] for c in row0["cells"].values() if c.get("bbox")]
    y0 = min(b[1] for b in boxes)
    y1 = max(b[3] for b in boxes)
    x0, x1 = int(col["x0"]) + 4, int(col["x1"]) - 4
    print(f"目标格 x{x0}-{x1} y{y0}-{y1} 原值={row0['cells']['qualified_qty']['text']}")

    canvas = gray.copy()
    canvas[y0:y1, x0:x1] = 255
    # 字要落在补检行带（表头下沿 ~ 行底）正中，四边留白；贴边会被安全阀判为
    # 「笔画被切断」直接丢弃，这也是真实单据压线书写时的预期行为。
    fs = 1.4
    th = 2
    cx = (x0 + x1) // 2 - 12
    cy = (y0 + y1) // 2 + 14
    cv2.putText(canvas, "6", (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, fs, 0, th, cv2.LINE_AA)
    cv2.imwrite("data/thumbs/_p7_canvas.png", canvas[max(0, y0 - 40):y1 + 20, max(0, x0 - 40):x1 + 20])
    print(f"写字 fs={fs} pos=({cx},{cy}) 格 y{y0}-{y1} x{x0}-{x1}")

    # 删掉原本落在该格的 OCR 文本，模拟检测器漏检
    kept = [
        ln for ln in lines
        if not (x0 <= ln["cx"] <= x1 and y0 <= ln["cy"] <= y1)
    ]
    # 同时把该行 qualified_qty 的 cell 拿掉（否则 extract 会直接用旧值）
    print("移除 OCR 行:", len(lines) - len(kept))

    after = extract_sheet(kept, im.width, im.height, "2026-09", bundle, gray=canvas)
    print("补检后:", [(it["seq"], it["qualified_qty"], it["unit_hours"]) for it in after["items"]])
    print("probe_count:", after["layout"].get("probe_count"))
    for it in after["items"]:
        print("  conf.q:", it["conf"]["qualified_qty"], "worker:", it["worker_name"] or it["worker_raw"])
    db.close()


if __name__ == "__main__":
    main()
