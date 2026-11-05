"""端到端识别自测：预处理 -> OCR -> 结构定位 -> 字段抽取 -> 规则校验。

用法：python scripts/test_ocr.py [图片路径...]
不指定路径时使用 data/samples 下的全部样张。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.db import SessionLocal, init_db  # noqa: E402
from app.ocr import engine, preprocess  # noqa: E402
from app.ocr.extract import extract_sheet  # noqa: E402
from app.domain.rules import summarize_flags, validate_sheet  # noqa: E402
from app.services.dicts import dict_bundle  # noqa: E402


def load_dicts():
    """加载识别名册（与线上流水线同一来源，含产品/材料/检验员/接交人）。"""
    db = SessionLocal()
    try:
        return dict_bundle(db)
    finally:
        db.close()


def run(path: Path, dicts) -> dict:
    t0 = time.time()
    stem = path.stem
    processed = BASE / "data" / "processed" / f"{stem}.png"
    meta = preprocess.process(path, processed_path=processed, deskew=True)
    with Image.open(processed) as im:
        w, h = im.size
        gray = np.asarray(im.convert("L"), dtype=np.uint8)

    t1 = time.time()
    lines = engine.recognize(str(processed), w, h)
    t2 = time.time()

    extracted = extract_sheet(lines, w, h, "2026-09", dicts, gray=gray)
    items, _codes = validate_sheet(extracted["items"], "2026-09")

    return {
        "file": path.name,
        "page": f"{w}x{h}",
        "skew": meta["skew_angle"],
        "red_ratio": meta["red_ratio"],
        "ocr_lines": len(lines),
        "prep_ms": int((t1 - t0) * 1000),
        "ocr_ms": int((t2 - t1) * 1000),
        "header": extracted["header"],
        "columns": [(c["label"], c["key"]) for c in extracted["layout"]["columns"]],
        "items": items,
        "flags": summarize_flags(items),
    }


def main() -> None:
    init_db()
    dicts = load_dicts()

    args = sys.argv[1:]
    paths = [Path(p) for p in args] if args else sorted((BASE / "data" / "samples").glob("sample_*.jpg"))
    if not paths:
        print("没有可测试的图片，请先运行 scripts/make_sample.py")
        return

    total_ok = 0
    total_items = 0
    for path in paths:
        result = run(path, dicts)
        total_items += len(result["items"])
        print("=" * 78)
        print(f"文件：{result['file']}  尺寸：{result['page']}  倾斜：{result['skew']}  红占比：{result['red_ratio']}")
        print(f"OCR 行数：{result['ocr_lines']}  预处理 {result['prep_ms']}ms  OCR {result['ocr_ms']}ms")
        print(f"表头：{json.dumps(result['header'], ensure_ascii=False)}")
        print(f"列映射：{result['columns']}")
        for it in result["items"]:
            flags = ",".join(f["code"] for f in it.get("flags", []))
            print(
                f"  行{it['seq']}: 工序={it['process']}({it['process_code']}) 单件={it['unit_hours']} "
                f"合格={it['qualified_qty']} 合计={it['total_hours']} 人={it['worker_name'] or it['worker_raw']} "
                f"日期={it['finish_date']} 检验={it['inspector']} "
                f"产品={it['product_name']} 材料={it['material']} "
                f"{'FLAGS:' + flags if flags else 'OK'}"
            )
            if it["worker_name"]:
                total_ok += 1
        print(f"告警汇总：{result['flags']}")

    print("=" * 78)
    print(f"图片 {len(paths)} 张  明细 {total_items} 行  人员匹配成功 {total_ok} 行")


if __name__ == "__main__":
    main()
