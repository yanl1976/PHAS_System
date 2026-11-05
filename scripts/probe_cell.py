"""排查工具：把每条明细的单价格裁出来，与整页识别对照，并打印投票明细。

走的是正式实现（app/ocr/cellprobe.recheck_hours_cell），所以「这里看到什么」就等于
「生产链路会怎么判」，不会出现调试脚本与线上两套逻辑。

用法：
    python scripts/probe_cell.py data/processed/recheck_7.png
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.ocr import preprocess  # noqa: E402
from app.ocr.cellprobe import _HOURS_VOTE_PADS, _recheck_one, shape_kept  # noqa: E402
from app.ocr.engine import recognize  # noqa: E402
from app.ocr.extract import extract_sheet  # noqa: E402
from app.services.dicts import dict_bundle  # noqa: E402


def main() -> None:
    src = Path(sys.argv[1])
    if not src.is_absolute():
        src = BASE / src

    db = SessionLocal()
    try:
        bundle = dict_bundle(db)
    finally:
        db.close()

    out_dir = BASE / "data" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 已经预处理过的图不要再处理一遍：二次缩边/纠偏会让裁格差 1~2px，
    # 而单格识别对 padding 极其敏感（pad 奇偶能翻转 5/8），结论会失真。
    if src.parent.resolve() == out_dir.resolve():
        processed = src
    else:
        processed = out_dir / f"probe_{src.stem}.png"
        preprocess.process(
            str(src),
            processed_path=processed,
            thumb_path=out_dir / f"probe_{src.stem}_thumb.jpg",
            mask_path=out_dir / f"probe_{src.stem}_red.png",
        )
    with PILImage.open(processed) as im:
        w, h = im.size
        gray = np.asarray(im.convert("L"), dtype=np.uint8)

    lines = recognize(str(processed), w, h)
    got = extract_sheet(lines, w, h, "2026-09", bundle, gray=gray)

    for idx, it in enumerate(got["items"], start=1):
        bbox = (it.get("bbox") or {}).get("unit_hours")
        hint = (it.get("conf") or {}).get("unit_hours_raw") or ""
        print(f"--- 第 {idx} 行  单件={it.get('unit_hours')}  原文={hint!r}")
        if not bbox:
            print("    （该行没有单价格坐标，跳过）")
            continue
        for drop in (True, False):
            for pad in _HOURS_VOTE_PADS:
                got_one = _recheck_one(gray, bbox, pad, drop)
                if not got_one:
                    print(f"    drop={int(drop)} pad={pad}  → 未识别")
                    continue
                text, score = got_one
                kept = "采信" if shape_kept(text, hint) else "标点残缺，丢弃"
                print(f"    drop={int(drop)} pad={pad}  → {text!r:12s} {score:.2f}  {kept}")
        rec = (it.get("conf") or {}).get("unit_hours_recheck")
        print(f"    结论：{rec if rec else '与整页一致，无需复查'}")


if __name__ == "__main__":
    main()
