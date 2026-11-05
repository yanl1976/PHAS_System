"""扫描 doc/ 下全年个人工时表，产出结构化记录到 data/doc_scan.json。

用法：python scripts/analysis/scan_doc.py
每个 xlsx 是一个月；每个 sheet 是一个人的工时明细（或合计页、机台页）。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

import openpyxl  # noqa: E402

# 表头同义词 -> 标准键
COLMAP = {
    "序号": "seq",
    "产品零部件图号": "product",
    "产品名称/代号": "product",
    "产品名称": "product",
    "批次": "batch",
    "名称": "part_name",
    "件名": "part_name",
    "零件名称": "part_name",
    "工序": "process",
    "合格件数": "qty",
    "合格": "qty",
    "合格数量": "qty",
    "单件工时": "unit_hours",
    "合计工时": "total_hours",
    "完成时间": "finish_date",
    "检验员": "inspector",
    "姓名": "inspector",
    "备注": "remark",
    "人员": "person",
    "工时": "person_hours",
    "Ф": "diameter",
    "φ": "diameter",
}

SKIP_SHEETS = {"合计工时", "合计", "总计"}


def norm_header(v) -> str | None:
    if v is None:
        return None
    t = re.sub(r"\s+", "", str(v)).strip()
    return COLMAP.get(t) or COLMAP.get(t.lstrip("ФφΦ"))


def iter_month(fname: str) -> str:
    m = re.match(r"(\d{4})年(\d{1,2})月", fname)
    return f"{m.group(1)}-{int(m.group(2)):02d}" if m else ""


def to_hours(v) -> float | None:
    """Excel 工时既可能是 timedelta 也可能是 '4:10:00' 字符串或小数天。"""
    if v is None or v == "":
        return None
    if isinstance(v, timedelta):
        return round(v.total_seconds() / 3600, 4)
    if isinstance(v, datetime):
        return round((v - datetime(1899, 12, 30)).total_seconds() / 86400, 4) if v.year < 1900 else None
    if isinstance(v, (int, float)):
        f = float(v)
        return round(f * 24, 4) if 0 < f <= 30 else round(f, 4)
    s = str(v).strip()
    if not s:
        return None
    m = re.match(r"^(?:(\d+)\s*days?,\s*)?(\d{1,3}):(\d{2})(?::(\d{2}))?$", s)
    if m:
        d = int(m.group(1) or 0)
        return round(d * 24 + int(m.group(2)) + int(m.group(3)) / 60, 4)
    try:
        return round(float(s), 4)
    except ValueError:
        return None


def to_date(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, (int, float)) and 20000 < v < 60000:
        return (datetime(1899, 12, 30) + timedelta(days=float(v))).strftime("%Y-%m-%d")
    s = str(v).strip()
    m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.match(r"^(\d{1,2})[-/.月](\d{1,2})", s)
    if m:
        return None
    return None


def to_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(str(v).strip()))
    except ValueError:
        return None


def parse_sheet(ws, month: str, fname: str, sheet: str) -> dict:
    rows = list(ws.iter_rows(values_only=True))
    # 定位表头
    hdr_idx, cmap = None, {}
    for i, r in enumerate(rows[:12]):
        m = {j: norm_header(c) for j, c in enumerate(r)}
        vals = set(m.values())
        if "seq" in vals and ("unit_hours" in vals or "total_hours" in vals or "person_hours" in vals):
            hdr_idx, cmap = i, m
            break
    if hdr_idx is None:
        return {"person": sheet, "month": month, "rows": [], "kind": "unknown", "meta": {}}

    meta: dict[str, str] = {}
    for r in rows[:hdr_idx]:
        joined = " ".join(str(c) for c in r if c is not None)
        for key, pat in (("title", r"工时汇总表|工时表$"), ("name", r"姓名[：:]\s*(\S+)"), ("job", r"工种[：:]\s*(\S+)")):
            hit = re.search(pat, joined)
            if hit:
                meta[key] = hit.group(1) if hit.groups() else joined.strip()

    is_ledger = "person_hours" in set(cmap.values())
    recs = []
    for r in rows[hdr_idx + 1:]:
        rec = {}
        for j, key in cmap.items():
            if not key or j >= len(r):
                continue
            rec[key] = r[j]
        product = str(rec.get("product") or "").strip()
        part = str(rec.get("part_name") or "").strip()
        proc = str(rec.get("process") or "").strip()
        if is_ledger:
            person = str(rec.get("person") or "").strip()
            if person:
                recs.append({"person": person, "hours": to_hours(rec.get("person_hours"))})
            continue
        if not (product or part or proc):
            continue
        recs.append(
            {
                "month": month,
                "file": fname,
                "sheet": sheet,
                "product": product,
                "batch": str(rec.get("batch") or "").strip(),
                "part_name": part,
                "process": proc,
                "qty": to_int(rec.get("qty")),
                "unit_hours": to_hours(rec.get("unit_hours")),
                "total_hours": to_hours(rec.get("total_hours")),
                "finish_date": to_date(rec.get("finish_date")),
                "inspector": str(rec.get("inspector") or "").strip(),
                "remark": str(rec.get("remark") or "").strip(),
            }
        )
    return {
        "person": sheet,
        "month": month,
        "file": fname,
        "kind": "ledger" if is_ledger else "detail",
        "meta": meta,
        "rows": recs,
    }


def main() -> None:
    out = []
    for f in sorted((BASE / "doc").glob("*.xlsx")):
        month = iter_month(f.name)
        wb = openpyxl.load_workbook(f, read_only=False, data_only=True)
        for ws in wb.worksheets:
            if ws.title.strip() in SKIP_SHEETS:
                continue
            out.append(parse_sheet(ws, month, f.name, ws.title.strip()))
        wb.close()
    dst = BASE / "data" / "doc_scan.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    detail = [s for s in out if s["kind"] == "detail"]
    print(f"sheets={len(out)} detail={len(detail)} rows={sum(len(s['rows']) for s in detail)}")
    print("months:", sorted({s["month"] for s in out if s["month"]}))
    print("→", dst)


if __name__ == "__main__":
    main()
