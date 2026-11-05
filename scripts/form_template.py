"""生成空白《加工路线单》模板（PNG + PDF），供识别侧与打印侧共用。

版式实现全部在 app/render/route_form.py —— 本脚本只是命令行入口。
之所以要留这个脚本：识别前的空白表、人工对照用的样张、打印输出，三者必须是
同一份毫米坐标画出来的，任何一份单独改都会让「模板」和「实物」脱节。

用法：
    python scripts/form_template.py                     # 生成空白模板 PNG + PDF
    python scripts/form_template.py --dpi 200 --out x.png
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from app.render import route_form  # noqa: E402

OUT_DIR = BASE / "data" / "templates"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="", help="输出 PNG 路径（默认 data/templates/加工路线单_空白.png）")
    ap.add_argument("--dpi", type=int, default=route_form.DPI)
    args = ap.parse_args()

    img = route_form.render(blank=True, dpi=args.dpi)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    png = Path(args.out) if args.out else OUT_DIR / "加工路线单_空白.png"
    img.save(png)
    pdf = png.with_suffix(".pdf")
    img.save(pdf, "PDF", resolution=float(args.dpi))
    print(png)
    print(pdf)


if __name__ == "__main__":
    main()
