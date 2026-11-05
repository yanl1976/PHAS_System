"""对库里所有已上传单据，用当前代码（新模板版面）重跑一遍抽取，只读不写库。

用途：换模板/改规则后，一次性看清「每张单据会识别成什么、会弹哪些提示」，
不改动任何已落库数据（含已确认单据）。

用法：
    python scripts/recheck_all.py            # 全部单据
    python scripts/recheck_all.py 6 7        # 只跑指定 sheet id
    python scripts/recheck_all.py --json     # 输出 JSON
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
from PIL import Image as PILImage  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.domain.rules import validate_sheet  # noqa: E402
from app.models import Image, Period, Sheet  # noqa: E402
from app.ocr import engine as ocr_engine  # noqa: E402
from app.ocr import preprocess  # noqa: E402
from app.ocr.extract import extract_sheet  # noqa: E402
from app.services.dicts import dict_bundle  # noqa: E402


def run_one(db, sheet: Sheet, bundle: dict) -> dict:
    img = db.get(Image, sheet.image_id)
    period = db.get(Period, sheet.period_id) if sheet.period_id else None
    ym = period.ym if period else ""

    src = img.path
    out_dir = BASE / "data" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"recheck_{sheet.id}"
    processed = out_dir / f"{stem}.png"
    preprocess.process(
        src,
        processed_path=processed,
        thumb_path=out_dir / f"{stem}_thumb.jpg",
        mask_path=out_dir / f"{stem}_red.png",
    )
    with PILImage.open(processed) as im:
        w, h = im.size
        gray = np.asarray(im.convert("L"), dtype=np.uint8)

    lines = ocr_engine.recognize(str(processed), w, h)
    got = extract_sheet(lines, w, h, ym, bundle, gray=gray)
    items, sheet_codes = validate_sheet(
        got["items"], ym, columns=got["layout"].get("columns"), std_hours=bundle.get("std_hours")
    )
    return {
        "sheet_id": sheet.id,
        "db_status": sheet.status,
        "file": Path(src).name,
        "period": ym,
        "page": [w, h],
        "columns": [
            (c.get("label"), c.get("key"), round(c.get("x0", 0)), round(c.get("x1", 0)))
            for c in got["layout"].get("columns", [])
        ],
        "header": got["header"],
        "items": items,
        "sheet_flags": sheet_codes,
    }


def main() -> None:
    as_json = "--json" in sys.argv
    ids = [int(a) for a in sys.argv[1:] if not a.startswith("--")]

    db = SessionLocal()
    try:
        bundle = dict_bundle(db)
        q = select(Sheet).order_by(Sheet.id)
        sheets = [s for s in db.scalars(q) if not ids or s.id in ids]
        results = [run_one(db, s, bundle) for s in sheets]
    finally:
        db.close()

    if as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        return

    counter: Counter[str] = Counter()
    for r in results:
        print(f"=== 单据 {r['sheet_id']}（库内状态 {r['db_status']}）{r['file']} {r['page'][0]}x{r['page'][1]}")
        hd = {k: v for k, v in (r["header"] or {}).items() if v and k not in ("dict_match",)}
        print("  表头:", json.dumps(hd, ensure_ascii=False))
        if r["sheet_flags"]:
            print("  单据级提示:", "、".join(r["sheet_flags"]))
            counter.update(r["sheet_flags"])
        for it in r["items"]:
            flags = [f["label"] for f in (it.get("flags") or [])]
            counter.update(flags)
            # 日期没解析出来时要能看到「模型到底读到了什么」（手写 9.10 常读成 9.0）
            date_text = it.get("finish_date") or f"空(读到 {it.get('finish_date_raw') or '-'})"
            print(
                f"  #{it.get('seq')} 工序={it.get('process') or '空'}"
                f"（原文 {it.get('process_raw') or '-'}）"
                f" 合格={it.get('qualified_qty')} 单件={it.get('unit_hours')}"
                f" 合计={it.get('declared_total')}"
                f" 操作者={it.get('worker_name') or it.get('worker_raw') or '空'}"
                f" 检查员={it.get('inspector') or '空'} 日期={date_text}"
                f" 提示={'、'.join(flags) if flags else '无'}"
            )
    print("\n--- 提示类型统计 ---")
    if counter:
        for name, n in counter.most_common():
            print(f"{name}: {n}")
    else:
        print("无任何提示")


if __name__ == "__main__":
    main()
