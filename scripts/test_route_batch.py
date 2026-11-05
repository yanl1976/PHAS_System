"""S5 批量生成与覆盖看板的测试。

**隔离原则**：本测试只碰「测试自己造的路线」（created_by='test-batch'）。
库里可能有真实的首期路线（source='history', created_by='system'），
所有断言都必须限定在自己的数据范围内，不能用全表计数。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import SessionLocal  # noqa: E402
from app.models import ProcessRoute, ProcessRouteStep, StandardHour  # noqa: E402
from app.services import routes as R  # noqa: E402

FAILED: list[str] = []
TAG = "test-batch"
# 待建清单（coverage）的口径来自 standard_hour：有历史工时、没路线才叫「待建」。
# 原来这个测试靠「偏移 300 处正好还有没建线的组合」，可真实库补齐全量工艺之后
# 一条待建的都没有了。测试不能依赖生产数据恰好不完整，所以自己造 60 条，
# 并把价值给到最高，让它们稳定排在待建清单最前面 —— 这样 offset=0 就够，
# 不必再猜某个魔法偏移量。
FIXTURE_PRODUCT = "ZZ批量建线产品"
FIXTURE_N = 60
FIXTURE_VALUE_SAMPLES = 500
FIXTURE_HOURS = 10.0


def fixture_parts() -> list[str]:
    return [f"批量件{i:02d}" for i in range(1, FIXTURE_N + 1)]


def seed_fixture(db) -> None:
    """造 N 条「有历史工时、无路线」的待建项，价值远高于真实数据。"""
    for part in fixture_parts():
        db.add(
            StandardHour(
                product=FIXTURE_PRODUCT,
                part=part,
                process="车",
                unit_hours=FIXTURE_HOURS,
                samples=FIXTURE_VALUE_SAMPLES,
                agreement=1.0,
                active=True,
            )
        )
    db.commit()


def drop_fixture(db) -> None:
    db.query(StandardHour).filter(StandardHour.product == FIXTURE_PRODUCT).delete()
    db.commit()


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def mine(db) -> list[ProcessRoute]:
    return db.query(ProcessRoute).filter(ProcessRoute.created_by == TAG).all()


def cleanup(db) -> int:
    n = len(mine(db))
    for r in mine(db):
        r.status = "draft"
        db.add(r)
        db.flush()
        db.delete(r)
    db.commit()
    return n


def main() -> int:
    db = SessionLocal()
    cleanup(db)
    drop_fixture(db)
    seed_fixture(db)
    # 首期基线在 P3 迁移里已经发布成可用基准了，所以这里不能断言「全库 0 条已发布」；
    # 改成记录进入测试时的数量，结束时核对本测试没有额外发布任何一条。
    released_before = (
        db.query(ProcessRoute)
        .filter(ProcessRoute.status == "released", ProcessRoute.kind == "route")
        .count()
    )

    # ---- 1. 干跑不落库
    print("\n[1] 干跑")
    before = len(mine(db))
    out = R.batch_generate(db, limit=10, offset=0, dry_run=True, created_by=TAG)
    check(out["created_count"] == 10, f"预览 {out['created_count']} 条")
    check(len(mine(db)) == before, "干跑不落库")
    check(out["dry_run"] is True, "返回标记 dry_run")

    # ---- 2. 真生成
    print("\n[2] 批量生成草稿")
    out = R.batch_generate(db, limit=FIXTURE_N, offset=0, created_by=TAG)
    check(out["created_count"] == FIXTURE_N, f"生成 {out['created_count']} 条")
    check(len(mine(db)) == before + FIXTURE_N, f"落库 {FIXTURE_N} 条")
    check(all(r.status == "draft" for r in mine(db)), "全部是草稿（默认不发布）")
    check(all(r.source == "history" for r in mine(db)), "来源标记为 history")

    bad = []
    for r in mine(db):
        if not r.steps:
            bad.append((r.product, r.part, "无工序"))
            continue
        for s in r.steps:
            if s.unit_hours <= 0:
                bad.append((r.product, r.part, f"{s.process_name} 工时 0"))
    check(not bad, f"每条路线都有工序且工时为正（异常 {len(bad)}）")

    dup = []
    for r in mine(db):
        seqs = [s.seq for s in r.steps]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            dup.append((r.product, r.part, seqs))
    check(not dup, f"工序号递增且不重复（异常 {len(dup)}）")

    # 疑似非工序的行必须被标记出来
    marked = sum(
        1 for r in mine(db) for s in r.steps if "疑似非工序" in (s.remark or "")
    )
    print(f"  （其中标记为「疑似非工序」的行：{marked}）")

    # ---- 3. 重复生成会跳过
    print("\n[3] 重复生成跳过已有")
    out2 = R.batch_generate(db, limit=10, offset=0, created_by=TAG)
    check(out2["created_count"] == 0, "已有版本的不再生成")
    check(out2["skipped_count"] == 10, f"跳过 {out2['skipped_count']} 条")
    check(len(mine(db)) == before + FIXTURE_N, "本测试的数据没变（没有版本号分叉）")

    # ---- 4. 覆盖率
    print("\n[4] 覆盖率看板")
    cov = R.coverage(db, limit=1000)
    check(
        cov["covered_count"] + cov["draft_count"] + cov["unbuilt_count"] == cov["total"],
        "三类计数自洽",
    )
    check(bool(cov["products"]), f"按产品聚合 {len(cov['products'])} 条")
    check(
        cov["products"][0]["value"] >= cov["products"][-1]["value"],
        "产品聚合按价值降序",
    )
    # 我建的这一批一定都落在「草稿」里
    mine_keys = {(r.product, r.part) for r in mine(db)}
    hit = [
        x for x in cov["items"]
        if (x["product"], x["part"]) in mine_keys and x["state"] == "draft"
    ]
    check(len(hit) == FIXTURE_N, f"这批在覆盖清单里都标记为草稿（{len(hit)}）")

    # ---- 5. 发布后覆盖率上升
    print("\n[5] 发布后覆盖率上升")
    released = 0
    for r in mine(db)[:10]:
        R.publish(db, r)
        released += 1
    db.commit()
    cov2 = R.coverage(db, limit=1000)
    check(cov2["covered_count"] >= released, f"已发布 {cov2['covered_count']} 条")
    check(cov2["coverage_pct"] > 0, f"覆盖率 {cov2['coverage_pct']}%")
    check(
        cov2["covered_count"] + cov2["draft_count"] + cov2["unbuilt_count"] == cov2["total"],
        "发布后计数仍自洽",
    )

    # ---- 6. 清理
    print("\n[6] 清理")
    n = cleanup(db)
    check(n == FIXTURE_N, f"清理 {n} 条")
    check(len(mine(db)) == 0, f"本测试数据已清空（{len(mine(db))}）")
    drop_fixture(db)
    check(
        db.query(StandardHour).filter(StandardHour.product == FIXTURE_PRODUCT).count() == 0,
        "待建夹具已删除（否则会一直挂在覆盖看板上）",
    )
    # 全库还剩多少（首期真草稿应当完好）
    # 只数基线。P2 之后库里还有任务工艺（kind='task'），它们建好即 released，
    # 属于正常状态，不该算进「首期路线被发布了」。
    total = db.query(ProcessRoute).filter(ProcessRoute.kind == "route").count()
    released = (
        db.query(ProcessRoute)
        .filter(ProcessRoute.status == "released", ProcessRoute.kind == "route")
        .count()
    )
    print(f"  （库里其余路线 {total} 条属于首期建线，未受影响；已发布 {released} 条）")
    check(total > 0, "首期建线的路线未被本测试误删")
    # 本测试只生成草稿，不该让任何一条基线变成已发布
    check(
        released == released_before,
        f"本测试没有额外发布路线（进入 {released_before} → 现在 {released}）",
    )

    db.close()

    print("\n" + "=" * 56)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
