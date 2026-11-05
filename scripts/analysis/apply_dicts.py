"""把 data/extracted/*.json 写入基本数据（字典表）。

原则：
- **只增不删**：已有条目一律保留，新产品/新工序匹配不上的才新建，避免历史单据引用断掉。
- 已有条目只做「补」：补别名、补空着的工种/规格。人工维护过的值不覆盖。
- 可重复执行：第二次跑不会产生重复条目。

用法：python scripts/analysis/apply_dicts.py [--dry]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE))

from app.db import SessionLocal, init_db  # noqa: E402
from app.models import (  # noqa: E402
    PartDict,
    ProcessDict,
    ProductDict,
    Role,
    StandardHour,
    Worker,
    WorkerRole,
)
from scripts.analysis.merge import split_product  # noqa: E402

SRC = BASE / "data" / "extracted"
DRY = "--dry" in sys.argv
MAX_ALIAS = 40


def load(name: str):
    return json.loads((SRC / f"{name}.json").read_text(encoding="utf-8"))


def split_alias(text: str) -> list[str]:
    return [x for x in (text or "").replace("，", ",").split(",") if x.strip()]


def merge_alias(existing: str, add: list[str]) -> str:
    have = split_alias(existing)
    seen = set(have)
    for a in add:
        a = (a or "").strip()
        if a and a not in seen:
            have.append(a)
            seen.add(a)
    return ",".join(have[:MAX_ALIAS])


def apply_workers(db, stats) -> None:
    roster = load("workers")
    inspectors = load("inspectors")
    role = db.query(Role).filter(Role.code == "INSPECTOR").first()
    worker_role = db.query(Role).filter(Role.code == "WORKER").first()

    by_name = {w.name: w for w in db.query(Worker).all()}
    for item in roster:
        w = by_name.get(item["name"])
        if not w:
            w = Worker(name=item["name"], team=item["team"], default_process=item["job"])
            db.add(w)
            db.flush()
            if worker_role and not DRY:
                db.add(WorkerRole(worker_id=w.id, role_id=worker_role.id))
            stats["worker_new"] += 1
        else:
            # 工种以工时账表头的「工种：」为准（人工填的），空着才补
            if item["job"] and not w.default_process:
                w.default_process = item["job"]
            elif item["job"] and w.default_process != item["job"]:
                stats["worker_job_diff"].append((w.name, w.default_process, item["job"]))
                w.default_process = item["job"]
            if item["team"] and w.team != item["team"]:
                w.team = item["team"]
                stats["worker_team_fixed"] += 1
        by_name[w.name] = w

    # 专职检验员（不填工时表，但检验员列里高频出现）
    for item in inspectors:
        name = item["name"]
        w = by_name.get(name)
        if not w:
            w = Worker(name=name, team="质检", default_process="检验")
            db.add(w)
            db.flush()
            by_name[name] = w
            stats["worker_new"] += 1
        if role and not DRY:
            exists = db.query(WorkerRole).filter(
                WorkerRole.worker_id == w.id, WorkerRole.role_id == role.id
            ).first()
            if not exists:
                db.add(WorkerRole(worker_id=w.id, role_id=role.id))
                stats["inspector_added"].append(name)
        w.is_inspector = True
    db.flush()


def apply_processes(db, stats) -> None:
    items = load("processes")
    by_code = {p.code: p for p in db.query(ProcessDict).all()}
    order = max([p.sort_order for p in db.query(ProcessDict).all()] or [0])
    for it in items:
        p = by_code.get(it["code"])
        if p:
            before = len(split_alias(p.aliases))
            p.aliases = merge_alias(p.aliases, [it["name"]] + it["aliases"])
            after = len(split_alias(p.aliases))
            if after > before:
                stats["process_alias_added"] += after - before
            if not p.category:
                p.category = it["category"]
        else:
            order += 1
            db.add(ProcessDict(
                code=it["code"], name=it["name"], category=it["category"],
                count_hours=it["count_hours"],
                aliases=merge_alias("", [it["name"]] + it["aliases"]),
                sort_order=order, active=True,
            ))
            stats["process_new"] += 1
    db.flush()


def apply_products(db, stats) -> None:
    items = load("products")
    existing = db.query(ProductDict).all()
    by_key: dict[tuple[str, str], ProductDict] = {}
    for d in existing:
        by_key.setdefault(split_product(d.name), d)

    for it in items:
        key = (it["family"], it["model"])
        variants = [v for v in it["variants"] if len(v) <= 40]
        d = by_key.get(key)
        if d:
            # 名称已有的不动（可能有单据引用），其余写法进别名
            before = len(split_alias(d.aliases))
            d.aliases = merge_alias(d.aliases, [v for v in variants if v != d.name])
            stats["product_alias_added"] += len(split_alias(d.aliases)) - before
            if not d.spec and it["family"]:
                d.spec = it["family"]
        else:
            order = (db.query(ProductDict).count() + 1)
            d = ProductDict(
                name=it["name"], code=it["model"] or it["family"], spec=it["family"],
                aliases=merge_alias("", [v for v in variants if v != it["name"]]),
                sort_order=order, active=True,
            )
            db.add(d)
            by_key[key] = d
            stats["product_new"] += 1
    db.flush()


def apply_parts(db, stats) -> None:
    items = load("parts")
    have = {p.name for p in db.query(PartDict).all()} if not DRY else set()
    order = len(have)
    for it in items:
        if it["name"] in have:
            continue
        order += 1
        db.add(PartDict(name=it["name"], sort_order=order, active=True))
        have.add(it["name"])
        stats["part_new"] += 1
    db.flush()


def apply_std_hours(db, stats) -> None:
    items = load("std_hours")
    have = set()
    if not DRY:
        have = {(h.product, h.part, h.process) for h in db.query(StandardHour).all()}
    for it in items:
        key = (it["product"], it["part"], it["process"])
        if key in have:
            continue
        db.add(StandardHour(
            product=it["product"], part=it["part"], process=it["process"],
            unit_hours=it["unit_hours"], samples=it["samples"],
            agreement=it["agreement"], active=True,
        ))
        have.add(key)
        stats["std_new"] += 1
    db.flush()


def main() -> None:
    init_db()  # 建 part_dict / standard_hour 新表（create_all 幂等，dry 也要先有表）
    db = SessionLocal()
    stats = {
        "worker_new": 0, "worker_job_diff": [], "worker_team_fixed": 0, "inspector_added": [],
        "process_new": 0, "process_alias_added": 0,
        "product_new": 0, "product_alias_added": 0,
        "part_new": 0, "std_new": 0,
    }
    try:
        apply_workers(db, stats)
        apply_processes(db, stats)
        apply_products(db, stats)
        apply_parts(db, stats)
        apply_std_hours(db, stats)
        if DRY:
            db.rollback()
            print("[dry] 回滚，未写入")
        else:
            db.commit()
    finally:
        db.close()

    print(f"人员 新增 {stats['worker_new']}，修正工种 {len(stats['worker_job_diff'])}，"
          f"修正车间 {stats['worker_team_fixed']}，补检验员 {len(stats['inspector_added'])}"
          f"{'：' + '/'.join(stats['inspector_added']) if stats['inspector_added'] else ''}")
    for name, old, new in stats["worker_job_diff"]:
        print(f"   工种 {name}: {old} → {new}")
    print(f"工序 新增 {stats['process_new']}，补别名 {stats['process_alias_added']} 条")
    print(f"产品 新增 {stats['product_new']}，补别名 {stats['product_alias_added']} 条")
    print(f"件名 新增 {stats['part_new']} 条")
    print(f"单件工时标准 新增 {stats['std_new']} 条")


if __name__ == "__main__":
    main()
