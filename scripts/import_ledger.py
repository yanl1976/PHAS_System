"""从既有工时核算台账提取「产品名称」与「工序」，导入系统字典。

台账（doc/9月2车间工时核算.xlsx）每人一个 sheet，第 3 行是表头，列「产品名称/代号」
和「工序」。这两列是自由填写的，写法与字典口径不一致，因此本脚本做三件事：

1. 提取：按表头定位列（袁红华表的产品列名是空单元格，用列位置兜底）
2. 归类：工序拆成「工艺工序 / 件名类写法 / 噪声」三类；产品做变体合并、剔除杂项
3. 导入：复用 app/api/admin.py 的 _import_rows，与前端「批量导入」同一套解析

用法：
    python scripts/import_ledger.py --dry-run     # 只看报告，不写库
    python scripts/import_ledger.py               # 实际导入
    python scripts/import_ledger.py --include-unclassified
                                                  # 件名类写法也录入工序字典

归类规则为什么这么定：
- 台账工序列是混合列，既有「车/铣/磨」这类工艺工序，也有「法兰/料仓/540内框」
  这类**加工内容**。前者才是工序，后者如果也进字典，月度汇总会碎成几十个只有
  一两条的"工序"，工时占比失去意义。
- 数字后缀（车1/铣2）是同一工序的不同机台，归并到标准工序并进别名，识别时
  仍能匹配原文。
- 「合计」「工种：龙门刨」是汇总行/批注串进数据区，直接丢弃。
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from openpyxl import load_workbook  # noqa: E402

DEFAULT_LEDGER = BASE / "doc" / "9月2车间工时核算.xlsx"
SKIP_SHEETS = {"总工时"}

# ---------------------------------------------------------------- 工序归类
# code, 标准名, 工种, 台账里出现过的写法（含 seed 原有别名，替换模式导入不会丢）
PROCESS_STANDARD: list[tuple[str, str, str, str]] = [
    ("TANG", "镗", "镗工", "镗,镗工,镗床"),
    ("XI", "铣", "铣工", "铣,铣工,铣1,铣2,铣3,铣4,铣床"),
    ("LJIA", "立加", "立加", "立加,立式加工中心,立加1,立加2"),
    ("JGZX", "加工中心", "加工中心", "加工中心,cnc,CNC,加工中芯"),
    ("CHE", "车", "车工", "车,车工,车床,数车,数控车,车1,车2,车3,车4,车5"),
    ("QIAN", "钳", "钳工", "钳,钳工,钳1,钳2,钳装,气缸钳装,退回翻新钳拆"),
    ("MO", "磨", "磨工", "磨,磨工,磨床,磨1,磨2,翻板磨,主轴磨"),
    ("JINGMO", "精磨", "磨工", "精磨"),
    ("XIAN", "线切割", "线切割", "线切割,线切,线"),
    ("ZHEWAN", "折弯", "折弯", "折弯,折,辅助折弯"),
    ("BAO", "刨", "刨工", "刨,刨床,机架刨,导向轴座刨,龙门刨"),
    ("DIANGONG", "电工", "电工", "电工,电气,拆线,拆线打包,火焰切割接线"),
    ("HAN", "焊", "焊工", "焊,焊接,组焊"),
    ("ZHUANGPEI", "装配", "装配", "装配,组装,沾输送带"),
    ("SHIJI", "试机", "装配", "试机,调试,试程序,试自动程序,空载试机"),
    ("PEIPAN", "配盘", "电工", "配盘,配电"),
    ("XIEHUO", "卸车", "辅助", "卸车"),
    ("SONGHUO", "送货", "辅助", "开车送货,送货"),
    # 以下为台账新增、seed 未覆盖的工序
    ("PENSHA", "喷砂", "辅助", "喷砂,打砂"),
    ("XIALIAO", "下料", "下料", "下料,激光下料,火焰下料"),
    ("HUAXIAN", "划线", "钳工", "划线,划,放样"),
    ("ZUANKONG", "钻孔", "钳工", "钻孔,钻,输送带链条钻孔"),
    ("CHA", "插", "插工", "插,插床,插削"),
    ("JIAOZHI", "校直", "钳工", "校直,矫直"),
    ("DAOJIAO", "倒角", "钳工", "倒角"),
    ("DABAO", "打包", "辅助", "打包,装车,包装,装箱"),
]

# 工序列里其实是「件名 / 加工内容」，不是工序。默认不录；--include-unclassified 才录。
PROCESS_UNCLASSIFIED: set[str] = {
    "540内框", "540外框", "安装板1", "安装板2", "安装板3", "法兰", "端面法兰",
    "料仓", "箱体", "料筒", "侧板", "侧板2", "内框条1", "内框条2", "主轴滑套",
    "次轮", "端盖", "鸭嘴热模", "加厚刀轮", "刀轮内孔外径", "前预热模A/B",
    "倒膜条", "垫块左右", "木板垫块", "定位销", "工艺孔", "上批号", "下批号",
    "铣底座垫块",
}

# 汇总行 / 批注 / 设备维护，不是工序
PROCESS_NOISE: set[str] = {
    "合计", "工种：龙门刨", "工种:", "修么", "真空泵放水", "更换操作台滑轨",
}

# ---------------------------------------------------------------- 产品归类
# 台账里是同一产品的不同写法，合并到标准写法（后者作为别名保留）
PRODUCT_MERGE: dict[str, str] = {
    "YUJ-18Bz制丸机": "YUJ-18BZ制丸机",  # 大小写
    "90滚筒干燥": "滚筒干燥90",           # 词序颠倒
    "120三辊": "三辊120",
    "SJ-2剂": "SJ-2栓剂",                 # 漏字
    "HWZ-30B箱式": "HWZ-30B箱式微波",     # 简写
    "30B箱式微波": "HWZ-30B箱式微波",
    "移印记": "移印机",                   # 错字，件名同为「输送链中间链轮轴」
}

# 存疑写法：可能是简称或错字，但不做合并（宁可多一条，让用户在弹窗里自己定）
PRODUCT_SUSPECT: dict[str, str] = {
    "全剂": "疑为「栓剂」，件名同为轴类",
    "双制丸机": "疑为「双螺杆制丸机」，该行件名/工序均为空",
    "对辊": "疑为「对辊02」或「对辊拨条制丸机」简称",
    "双层": "疑为「双层炼药机」简称",
    "滚筒干燥": "与「滚筒干燥90」是否同一规格待定",
    "三辊": "与「三辊120」「老三辊」是否同一台待定",
}

# 不是产品，是杂活/工序名填进来了
PRODUCT_SKIP: set[str] = {
    "零活", "开车送货", "装配", "辅助试机",
    "车间安装配电箱", "外购拌料", "材料试板",
}

# 台账写法 ── 已在字典里、只是加了后缀的，作为别名并入现有条目，不新建
PRODUCT_ALIAS_TO_EXISTING: dict[str, str] = {
    "YUJ-18BZ制丸机": "YUJ-18BZ",
}


def norm(value) -> str:
    text = str(value or "").strip().replace("\u3000", " ")
    return re.sub(r"\s+", "", text)


def read_ledger(path: Path) -> dict:
    """返回 {产品写法: 出现次数} 与 {工序写法: 出现次数}。"""
    workbook = load_workbook(path, data_only=True)
    products: Counter[str] = Counter()
    processes: Counter[str] = Counter()
    stats: dict[str, str] = {}

    for sheet in workbook.worksheets:
        if sheet.title in SKIP_SHEETS:
            continue
        # 表头行：同时含「序号」和「工序」
        header_row = None
        for row_no in range(1, 8):
            cells = [norm(c.value) for c in sheet[row_no]]
            if any("序号" in c for c in cells) and any("工序" in c for c in cells):
                header_row = row_no
                break
        if header_row is None:
            stats[sheet.title] = "未找到表头，已跳过"
            continue

        header = [norm(c.value) for c in sheet[header_row]]
        # 按列名定位；列名缺失时按台账固定列位兜底（产品=2、件名=4、工序=5）
        col_product = next((i for i, v in enumerate(header) if "产品" in v), 1)
        col_process = next((i for i, v in enumerate(header) if "工序" in v), 4)

        rows = 0
        for row_no in range(header_row + 1, sheet.max_row + 1):
            cells = [c.value for c in sheet[row_no]]
            get = lambda i: norm(cells[i]) if i < len(cells) else ""  # noqa: E731
            product, process = get(col_product), get(col_process)
            if product:
                products[product] += 1
            if process:
                processes[process] += 1
            if product or process:
                rows += 1
        stats[sheet.title] = f"{rows} 行"

    return {"products": products, "processes": processes, "stats": stats}


def classify_processes(seen: Counter[str], include_unclassified: bool) -> dict:
    alias_to_std: dict[str, str] = {}
    for _code, name, _cat, aliases in PROCESS_STANDARD:
        alias_to_std[name] = name
        for alias in re.split(r"[/、|;,，\s]+", aliases):
            if alias:
                alias_to_std[alias] = name

    mapped: dict[str, list[str]] = {}
    unclassified: list[str] = []
    noise: list[str] = []
    unknown: list[str] = []

    for text, count in seen.items():
        if text in PROCESS_NOISE:
            noise.append(text)
            continue
        std = alias_to_std.get(text)
        if std:
            mapped.setdefault(std, []).append(text)
        elif text in PROCESS_UNCLASSIFIED:
            unclassified.append(text)
        else:
            unknown.append(text)

    # 变体归并：车1 已在别名表里；这里只把「标准名之外」的写法记进别名
    extra_aliases: dict[str, list[str]] = {}
    for std, variants in mapped.items():
        extras = [v for v in variants if v != std]
        extra_aliases[std] = sorted(set(extras))

    return {
        "mapped": mapped,
        "extra_aliases": extra_aliases,
        "unclassified": sorted(unclassified),
        "noise": sorted(noise),
        "unknown": sorted(unknown),
        "include_unclassified": include_unclassified,
    }


def build_process_rows(classified: dict) -> list[list[str]]:
    rows = []
    for code, name, category, aliases in PROCESS_STANDARD:
        merged = [aliases] if aliases else []
        extras = classified["extra_aliases"].get(name, [])
        if extras:
            merged.append("/".join(extras))
        rows.append([code, name, category, "/".join(merged)])
    if classified["include_unclassified"]:
        for idx, text in enumerate(classified["unclassified"], start=1):
            rows.append(["", text, "加工内容", ""])
    return rows


def build_product_rows(products: Counter[str]) -> tuple[list[list[str]], dict]:
    merged: Counter[str] = Counter()
    aliases: dict[str, list[str]] = {}
    skipped: list[str] = []
    to_existing: dict[str, list[str]] = {}

    for text, count in products.items():
        if text in PRODUCT_SKIP:
            skipped.append(text)
            continue
        std = PRODUCT_MERGE.get(text, text)
        merged[std] += count
        if std != text:
            aliases.setdefault(std, []).append(text)
        if text in PRODUCT_ALIAS_TO_EXISTING:
            to_existing.setdefault(PRODUCT_ALIAS_TO_EXISTING[text], []).append(text)

    # 已被 PRODUCT_ALIAS_TO_EXISTING 接管的写法不再单独建条目
    for target, variants in to_existing.items():
        merged.pop(target, None)
        for v in variants:
            merged.pop(v, None)

    rows = [
        [name, "", "/".join(sorted(set(aliases.get(name, [])))), ""]
        for name in sorted(merged)
    ]
    return rows, {
        "total": len(rows),
        "aliases": aliases,
        "skipped": skipped,
        "to_existing": to_existing,
        "kept_names": set(merged),
    }


def print_report(ledger: dict, classified: dict, product_info: dict) -> None:
    print("=" * 68)
    print("台账读取")
    print("=" * 68)
    for name, desc in ledger["stats"].items():
        print(f"  {name:<8} {desc}")
    print(f"  产品写法 {len(ledger['products'])} 种 · 工序写法 {len(ledger['processes'])} 种")

    print()
    print("=" * 68)
    print("工序归类")
    print("=" * 68)
    print(f"  归并为标准工序 {len(classified['mapped'])} 个：")
    for code, name, category, _a in PROCESS_STANDARD:
        variants = classified["mapped"].get(name, [])
        if not variants:
            continue
        extras = classified["extra_aliases"].get(name, [])
        tag = f"  ← 别名 {', '.join(extras)}" if extras else ""
        print(f"    {name:<5}({category}){tag}")
    if classified["unclassified"]:
        print()
        print(f"  件名类写法 {len(classified['unclassified'])} 个"
              f"（{'已按加工内容录入' if classified['include_unclassified'] else '未录入工序字典'}）：")
        print("    " + "、".join(classified["unclassified"]))
    if classified["noise"]:
        print()
        print(f"  噪声 {len(classified['noise'])} 个（丢弃）：{'、'.join(classified['noise'])}")
    if classified["unknown"]:
        print()
        print(f"  [!] 未归类 {len(classified['unknown'])} 个：{'、'.join(classified['unknown'])}")

    print()
    print("=" * 68)
    print("产品归类")
    print("=" * 68)
    print(f"  生成条目 {product_info['total']} 条")
    for std, variants in product_info["aliases"].items():
        print(f"    合并 {std} ← {'、'.join(variants)}")
    for target, variants in product_info["to_existing"].items():
        print(f"    并入现有 {target} ← {'、'.join(variants)}（不新建条目）")
    if product_info["skipped"]:
        print(f"  剔除非产品 {len(product_info['skipped'])} 个：{'、'.join(product_info['skipped'])}")
    suspects = {k: v for k, v in PRODUCT_SUSPECT.items() if k in product_info["kept_names"]}
    if suspects:
        print()
        print("  存疑写法（已按原文独立成条，未强行合并）：")
        for name, note in suspects.items():
            print(f"    {name:<8} {note}")


def apply_import(product_rows: list[list[str]], process_rows: list[list[str]],
                 product_info: dict) -> None:
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import ProductDict
    from app.api.admin import _import_rows

    db = SessionLocal()
    try:
        if product_rows:
            # 产品用 append：已存在的跳过，避免重跑时覆盖用户在弹窗里改过的别名/规格
            result = _import_rows("products", product_rows, "append", db)
            print(f"  产品：新增 {result['created']} / 更新 {result['updated']} / 跳过 {result['skipped']}")
            if result.get("errors"):
                print(f"  产品异常：{result['errors'][:3]}")

        # 台账写法并入已有条目：合并别名而不是覆盖
        for target, variants in product_info["to_existing"].items():
            obj = db.scalar(select(ProductDict).where(ProductDict.name == target))
            if not obj:
                print(f"  [!] 现有产品 {target} 不存在，跳过别名合并")
                continue
            current = [a for a in (obj.aliases or "").split(",") if a]
            for v in variants:
                if v not in current:
                    current.append(v)
            obj.aliases = ",".join(current)
            print(f"  产品 {target} 别名追加：{'、'.join(variants)}")

        result = _import_rows("processes", process_rows, "replace", db)
        print(f"  工序：新增 {result['created']} / 更新 {result['updated']} / 跳过 {result['skipped']}")
        if result.get("errors"):
            print(f"  工序异常：{result['errors'][:3]}")
        db.commit()
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="从工时台账导入产品与工序字典")
    parser.add_argument("--file", default=str(DEFAULT_LEDGER), help="台账路径")
    parser.add_argument("--dry-run", action="store_true", help="只输出报告，不写库")
    parser.add_argument("--include-unclassified", action="store_true",
                        help="把件名类写法也录入工序字典（工种记为「加工内容」）")
    args = parser.parse_args()

    path = Path(args.file)
    if not path.exists():
        sys.exit(f"台账不存在：{path}")

    ledger = read_ledger(path)
    classified = classify_processes(ledger["processes"], args.include_unclassified)
    product_rows, product_info = build_product_rows(ledger["products"])
    process_rows = build_process_rows(classified)

    print_report(ledger, classified, product_info)

    if args.dry_run:
        print()
        print("dry-run：未写库。去掉 --dry-run 即可导入。")
        return

    print()
    print("=" * 68)
    print("写入字典")
    print("=" * 68)
    apply_import(product_rows, process_rows, product_info)
    print("完成。")


if __name__ == "__main__":
    main()
