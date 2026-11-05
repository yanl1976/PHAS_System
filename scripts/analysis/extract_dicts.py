"""从全年工时汇总（data/doc_scan.json）提取「基本数据」，产出到 data/extracted/*.json。

提取六类：
1. workers      人员名册（姓名 / 车间 / 工种 / 是否检验员）
2. processes    工序字典（标准名 + 工种 + 该工序在单据上的全部写法作为别名）
3. products     产品字典（设备类名 + 型号，同规格的不同写法互为别名）
4. parts        件名字典（零部件名称，识别「件名」栏的兜底名册）
5. std_hours    单件工时标准（产品 + 件名 + 工序 → 单件工时）
6. batches      批次/客户（只统计，不入库）

用法：python scripts/analysis/extract_dicts.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from scripts.analysis.merge import merge_process, norm, split_product  # noqa: E402
from scripts.analysis.lexicon import PROCESS_LEXICON  # noqa: E402

SCAN = BASE / "data" / "doc_scan.json"
OUT = BASE / "data" / "extracted"

# 不是人的 sheet：机台/产品/空表
NON_PERSON = {"尾料机", "2粒", "0", ""}
# 工种 → 角色提示
INSPECTOR_HINT = {"检验", "质检", "检查"}


def load_rows() -> list[dict]:
    data = json.loads(SCAN.read_text(encoding="utf-8"))
    return [r for s in data if s["kind"] == "detail" for r in s["rows"]]


def load_sheets() -> list[dict]:
    data = json.loads(SCAN.read_text(encoding="utf-8"))
    return [s for s in data if s["kind"] == "detail"]


def extract_workers(sheets: list[dict], rows: list[dict]) -> list[dict]:
    """人员：sheet 名是姓名（最可靠），表头「姓名/工种」补工种，文件名的车间决定 team。"""
    found: dict[str, dict] = {}
    for s in sheets:
        name = s["person"].strip()
        if name in NON_PERSON:
            continue
        w = found.setdefault(name, {"name": name, "team": "", "job": "", "rows": 0,
                                    "months": set(), "sheet_names": set(), "team_rows": Counter()})
        # 同一个人可能两个车间的账页都出现（马永刚：2车间 266 行 / 1-3车间 12 行），
        # 按行数多的那个定车间
        w["team_rows"]["2车间" if "2车间" in s["file"] else "1-3车间"] += len(s["rows"])
        w["rows"] += len(s["rows"])
        if s["month"]:
            w["months"].add(s["month"])
        job = (s["meta"].get("job") or "").strip()
        if job:
            w["job"] = job
        nm = (s["meta"].get("name") or "").strip()
        if nm:
            w["sheet_names"].add(nm)

    # 检验员：检验员列出现次数够多的，视为检验员
    ins = Counter()
    for r in rows:
        v = re.sub(r"\s+", "", r.get("inspector") or "")
        if v:
            ins[v] += 1
    # 检验员列里有的人本身不填工时表（专职检验），也要进名册：
    # 分开记，入库时统一按「人员 + 检验员角色」建
    extra_inspectors = []
    for name, n in ins.items():
        if name in found:
            found[name]["inspector_rows"] = n
        elif 2 <= len(name) <= 4 and n >= 3:
            extra_inspectors.append({"name": name, "inspector_rows": n})

    out = []
    for w in found.values():
        w["team"] = w["team_rows"].most_common(1)[0][0] if w["team_rows"] else ""
        w["team_rows"] = dict(w["team_rows"])
        w["months"] = sorted(w["months"])
        w["sheet_names"] = sorted(w["sheet_names"])
        out.append(w)
    out.sort(key=lambda x: (-x["rows"], x["name"]))
    return out, extra_inspectors


def extract_processes(rows: list[dict]) -> list[dict]:
    """工序：每条数据写法 → 标准工序，写法本身进别名表。"""
    variants: dict[str, Counter] = defaultdict(Counter)
    unhit: Counter = Counter()
    for r in rows:
        raw = re.sub(r"\s+", "", r.get("process") or "")
        if not raw:
            continue
        m = merge_process(raw)
        if not m:
            unhit[raw] += 1
            continue
        code, name, _count, _stem = m
        variants[code][raw] += 1

    meta = {c: (n, cat, cnt) for c, n, cat, cnt, _stems in PROCESS_LEXICON}
    out = []
    ranked = sorted(variants.items(), key=lambda kv: -sum(kv[1].values()))
    for code, cnt in ranked:
        name, cat, count_hours = meta[code]
        # 别名只收「短且常用」的写法，长的复合描述塞进去会把匹配搅乱
        alias = [v for v, c in cnt.most_common(24) if len(v) <= 10 and c >= 2]
        out.append({
            "code": code,
            "name": name,
            "category": cat,
            "count_hours": count_hours,
            "rows": sum(cnt.values()),
            "variants": dict(cnt.most_common(40)),
            "aliases": alias,
        })
    return out, unhit


def extract_products(rows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for r in rows:
        raw = re.sub(r"\s+", "", r.get("product") or "")
        if not raw:
            continue
        groups[split_product(raw)][raw] += 1

    out = []
    for (fam, model), cnt in groups.items():
        # 标准名取该组最高频写法：与手写单据最贴近，精确命中率最高
        top = cnt.most_common()
        name = top[0][0]
        out.append({
            "name": name,
            "family": fam,
            "model": model,
            "rows": sum(cnt.values()),
            "variants": dict(top),
            "aliases": [v for v, c in top[1:] if c >= 1],
        })
    out.sort(key=lambda x: -x["rows"])
    return out


def extract_parts(rows: list[dict], min_rows: int = 2) -> list[dict]:
    cnt: Counter = Counter()
    by_process: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        raw = re.sub(r"\s+", "", r.get("part_name") or "")
        if not raw or len(raw) < 2:
            continue
        cnt[raw] += 1
    out = []
    for name, n in cnt.most_common():
        if n < min_rows:
            continue
        out.append({"name": name, "rows": n})
    return out


def extract_std_hours(rows: list[dict], min_samples: int = 2) -> list[dict]:
    """单件工时标准：键是 (产品, 件名, 工序)。

    只收样本一致度够高的组合——同一个零件同一道工序的单件工时在全年账里应当稳定，
    抖得厉害的组合（改名、改制、临时加价）不入库，免得拿它当校验基准反而误报。
    """
    buckets: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for r in rows:
        uh = r.get("unit_hours")
        if uh is None or uh <= 0 or uh > 100:
            continue
        part = re.sub(r"\s+", "", r.get("part_name") or "")
        proc_raw = re.sub(r"\s+", "", r.get("process") or "")
        prod_raw = re.sub(r"\s+", "", r.get("product") or "")
        if not part:
            continue
        m = merge_process(proc_raw)
        proc = m[1] if m else proc_raw
        if not proc:
            continue
        fam, model = split_product(prod_raw) if prod_raw else ("", "")
        prod = f"{model}{fam}" if (fam or model) else ""
        buckets[(prod, part, proc)].append(round(uh, 4))

    out = []
    for key, vals in buckets.items():
        if len(vals) < min_samples:
            continue
        c = Counter(vals)
        value, hits = c.most_common(1)[0]
        agreement = hits / len(vals)
        if agreement < 0.6:
            continue
        out.append({
            "product": key[0], "part": key[1], "process": key[2],
            "unit_hours": value, "samples": len(vals), "agreement": round(agreement, 2),
        })
    out.sort(key=lambda x: (-x["samples"], x["product"]))
    return out


def extract_batches(rows: list[dict], min_rows: int = 3) -> list[dict]:
    c: Counter = Counter()
    for r in rows:
        raw = re.sub(r"\s+", "", r.get("batch") or "")
        if raw:
            c[raw] += 1
    return [{"name": k, "rows": v} for k, v in c.most_common() if v >= min_rows]


def main() -> None:
    rows = load_rows()
    sheets = load_sheets()
    OUT.mkdir(parents=True, exist_ok=True)

    workers, extra_inspectors = extract_workers(sheets, rows)
    processes, unhit = extract_processes(rows)
    products = extract_products(rows)
    parts = extract_parts(rows)
    std_hours = extract_std_hours(rows)
    batches = extract_batches(rows)

    def dump(name: str, obj) -> None:
        (OUT / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")

    dump("workers", workers)
    dump("inspectors", extra_inspectors)
    dump("processes", processes)
    dump("products", products)
    dump("parts", parts)
    dump("std_hours", std_hours)
    dump("batches", batches)
    dump("unhit_process", [{"name": k, "rows": v} for k, v in unhit.most_common(300)])

    print(f"人员 {len(workers)}（其中兼检验员：{sum(1 for w in workers if w.get('inspector_rows'))}），另补专职检验员 {len(extra_inspectors)} 人")
    print(f"工序 {len(processes)} 类，覆盖 {sum(p['rows'] for p in processes)} 行；未归并 {sum(unhit.values())} 行")
    print(f"产品 {len(products)} 条，覆盖 {sum(p['rows'] for p in products)} 行")
    print(f"件名 {len(parts)} 条（≥2 次）")
    print(f"单件工时标准 {len(std_hours)} 条")
    print(f"批次 {len(batches)} 条")
    print("→", OUT)


if __name__ == "__main__":
    main()
