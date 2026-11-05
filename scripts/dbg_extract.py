"""调试：对某张图片跑完整抽取，打印表头与明细。用法：python scripts/dbg_extract.py <image_id>"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Image as DbImage  # noqa: E402
from app.ocr.engine import recognize  # noqa: E402
from app.ocr.extract import extract_sheet  # noqa: E402
from app.services.pipeline import _dict_bundle  # noqa: E402


def main() -> None:
    iid = int(sys.argv[1])
    db = SessionLocal()
    img = db.get(DbImage, iid)
    dicts = _dict_bundle(db)
    db.close()
    path = img.processed_path or img.path
    with Image.open(path) as im:
        gray = np.asarray(im.convert("L"), dtype=np.uint8)
        w, h = im.size
    lines = recognize(path, w, h)
    out = extract_sheet(lines, w, h, "2026-11", dicts, gray=gray)
    print("header:", json.dumps(out["header"], ensure_ascii=False))
    for it in out["items"]:
        print(
            f"#{it['seq']} proc={it['process']!r} qty={it['qualified_qty']} unit={it['unit_hours']} "
            f"decl={it['declared_total']} date={it['finish_date']} worker={it['worker_id']}/{it['worker_raw']!r} "
            f"insp={it['inspector']!r} prod={it['product_name']!r} remark={it['remark']!r}"
        )
    print("columns:", [(c["key"], round(c["x0"]), round(c["x1"])) for c in out["layout"]["columns"]])


if __name__ == "__main__":
    main()
