"""调试：对任意图片文件跑完整抽取（不写库），用于验证新版式/新模板。

用法：python scripts/dbg_file.py data/samples/sample_02.jpg [--json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.ocr import preprocess  # noqa: E402
from app.ocr.engine import recognize  # noqa: E402
from app.ocr.extract import extract_sheet  # noqa: E402
from app.services.pipeline import _dict_bundle  # noqa: E402


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    as_json = "--json" in sys.argv
    path = Path(args[0]).resolve()
    ym = "2026-09"
    db = SessionLocal()
    try:
        dicts = _dict_bundle(db)
    finally:
        db.close()

    # 与生产链路一致：先做缩边/纠偏/红笔抑制，再识别（跳过预处理会因照片倾斜丢掉表格线）
    out_dir = BASE / "data" / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"dbg_{path.stem}"
    processed = out_dir / f"{stem}.png"
    meta = preprocess.process(
        str(path),
        processed_path=processed,
        thumb_path=out_dir / f"{stem}_thumb.jpg",
        mask_path=out_dir / f"{stem}_red.png",
    )
    with Image.open(processed) as im:
        w, h = im.size
        gray = np.asarray(im.convert("L"), dtype=np.uint8)
    lines = recognize(str(processed), w, h)
    out = extract_sheet(lines, w, h, ym, dicts, gray=gray)
    if as_json:
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return
    print(f"skew={meta.get('skew_angle')} page={w}x{h}")
    print("header:", json.dumps(out["header"], ensure_ascii=False))
    for it in out["items"]:
        print(
            f"#{it['seq']} proc={it['process']!r}({it['process_raw']!r}) qty={it['qualified_qty']} "
            f"unit={it['unit_hours']} decl={it['declared_total']} date={it['finish_date']} "
            f"worker={it['worker_id']}/{it['worker_raw']!r} insp={it['inspector']!r} "
            f"prod={it['product_name']!r} part={it['part_name']!r} mat={it['material']!r}"
        )
    print("columns:", [(c["key"], round(c["x0"]), round(c["x1"])) for c in out["layout"]["columns"]])


if __name__ == "__main__":
    main()
