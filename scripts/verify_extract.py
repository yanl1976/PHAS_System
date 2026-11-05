"""用缓存的 OCR 行重跑抽取，验证不落库的识别结果。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PIL import Image as PILImage

from app.db import SessionLocal
from app.models import Batch, Image, Period
from app.ocr.extract import extract_sheet
from app.services.pipeline import _dict_bundle


def main() -> None:
    img_id = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    lines = json.loads((Path("data/diag") / f"lines_{img_id}.json").read_text(encoding="utf-8"))["lines"]

    db = SessionLocal()
    img = db.get(Image, img_id)
    with PILImage.open(img.processed_path) as pim:
        page_w, page_h = pim.size
        gray = np.asarray(pim.convert("L"), dtype=np.uint8)
    batch = db.get(Batch, img.batch_id)
    period = db.get(Period, batch.period_id)
    bundle = _dict_bundle(db)
    db.close()

    out = extract_sheet(lines, page_w, page_h, period.ym, bundle, gray=gray)
    print(f"{'序':<3}{'工序':<8}{'单件':>9}{'合格':>6}{'工作者':<8}{'日期':<12}")
    total = 0.0
    for it in out["items"]:
        uh = it.get("unit_hours")
        qty = it.get("qualified_qty")
        hours = (uh or 0) * (qty or 0)
        total += hours
        print(
            f"{it.get('seq', '-'):<3}{(it.get('process') or '')[:6]:<8}"
            f"{(uh if uh is not None else '-'):>9}{(qty if qty is not None else '-'):>6}"
            f"{(it.get('worker_name') or it.get('worker_raw') or '')[:6]:<8}"
            f"{str(it.get('finish_date') or it.get('finish_date_raw') or ''):<12}"
        )
    print(f"\n合计工时 {total:.2f} h  明细 {len(out['items'])} 行")


if __name__ == "__main__":
    main()
