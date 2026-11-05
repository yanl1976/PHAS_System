"""实验：PaddleOCR 3.x 的表格结构识别（SLANet）在加工路线单上的表现。

目的不是接入，而是回答一个问题：把「自己算列边界、按坐标归格」换成通用表格结构
识别，结构准确率会不会更高。

输出：表格 HTML（结构）+ 每个单元格的 bbox，便于与现有 layout 结果对照。

用法：
    set PADDLE_PDX_MODEL_SOURCE=bos
    python scripts/probe_table.py data/processed/recheck_7.png
    python scripts/probe_table.py data/samples/sample_02.jpg --crop 0.28,1.0
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "bos")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PADDLE_PDX_MODEL_DIR", str(BASE / "models"))
os.environ.setdefault("FLAGS_use_mkldnn", "0")


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    src = Path(args[0])
    if not src.is_absolute():
        src = BASE / src

    crop = None
    for a in sys.argv[1:]:
        if a.startswith("--crop="):
            y0, y1 = a.split("=", 1)[1].split(",")
            crop = (float(y0), float(y1))

    from PIL import Image

    from paddleocr import TableStructureRecognition

    img = Image.open(src).convert("RGB")
    if crop:
        w, h = img.size
        img = img.crop((0, int(h * crop[0]), w, int(h * crop[1])))
        out = BASE / "data" / f"tmp_probe_crop_{src.stem}.png"
        img.save(out)
        target = str(out)
    else:
        target = str(src)

    model = TableStructureRecognition()
    results = model.predict(input=target)
    for res in results:
        data = res.json if hasattr(res, "json") else {}
        # 只保留结构相关字段，避免刷屏
        keep = {}
        for k in ("res", "html", "structure", "cell_bboxes", "input_path"):
            if isinstance(data, dict) and k in data:
                keep[k] = data[k]
        if not keep:
            keep = {"raw": str(data)[:2000]}
        print(json.dumps(keep, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
