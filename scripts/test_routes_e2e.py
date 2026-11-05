"""S3 端到端流程验证：用真实数据走一遍工作台的操作序列。

模拟用户在界面上依次做的事，每一步都核对接口返回，
最后确认数据落库正确（而不是只看 HTTP 200）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import (  # noqa: E402
    ProcessRoute,
    ProcessRouteStep,
    StandardHour,
)

FAILED: list[str] = []
# 「有历史工时、还没建线」的目标件由测试自己造。
# 原来是从树接口里捞第一个 state='none' 的件，可真实库补齐全量工艺后
# 一条未建的都没有，测试就卡在「找不到目标」。测试不能依赖数据残缺。
FIX_PRODUCT = "E2E待建产品"
FIX_PART = "E2E待建件"


def seed_fixture(db) -> None:
    db.query(StandardHour).filter(StandardHour.product == FIX_PRODUCT).delete()
    db.add(
        StandardHour(
            product=FIX_PRODUCT,
            part=FIX_PART,
            process="车",
            unit_hours=0.8,
            samples=4,
            agreement=1.0,
            active=True,
        )
    )
    db.commit()


def drop_fixture(db) -> None:
    db.query(StandardHour).filter(StandardHour.product == FIX_PRODUCT).delete()
    db.commit()


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def main() -> int:
    init_db()
    db = SessionLocal()
    for r in db.query(ProcessRoute).filter(ProcessRoute.product == "E2E产品").all():
        r.status = "draft"
        db.add(r)
        db.flush()
        db.delete(r)
    db.commit()
    seed_fixture(db)
    db.close()

    client = TestClient(app)

    # ---- 1. 目标：自建的「有工时、未建线」的件
    print("\n[1] 树接口里确认待建件")
    tree = client.get("/api/routes/tree").json()
    target = None
    for p in tree["items"]:
        if p.get("unspecified") or not p["name"]:
            continue
        for x in p["parts"]:
            if p["name"] == FIX_PRODUCT and x["part"] == FIX_PART and x["state"] == "none":
                target = (p["name"], x["part"])
                break
        if target:
            break
    check(bool(target), f"待建件出现在树里且状态为未建：{target}")
    product, part = target

    # ---- 2. 候选生成（界面上的「从历史生成」）
    print("\n[2] 从历史生成候选")
    sug = client.get("/api/routes/suggest", params={"product": product, "part": part}).json()
    check(sug["total"] > 0, f"候选 {sug['total']} 道工序")
    check(
        all(s.get("order_source") in ("sheet", "process_seq") for s in sug["steps"]),
        "每行都标了顺序依据",
    )

    # ---- 3. 新建草稿并带生成的工序（界面上的「新建路线 + 勾自动生成」）
    print("\n[3] 新建草稿（自动生成）")
    r = client.post(
        "/api/routes",
        json={"product": product, "part": part, "generate": True, "created_by": "e2e"},
    )
    check(r.status_code == 200, f"新建 → {r.status_code}")
    rid = r.json()["id"]
    check(r.json()["step_count"] == sug["total"], f"落库 {r.json()['step_count']} 道，与候选一致")
    check(r.json()["unit_sum"] > 0, f"单件合计 {r.json()['unit_sum']}")

    # ---- 4. 界面上的编辑：改工时、加行、调顺序
    print("\n[4] 编辑工序行")
    detail = client.get(f"/api/routes/{rid}").json()
    steps = [{k: v for k, v in s.items() if k not in ("id", "route_id")} for s in detail["steps"]]
    steps[0]["unit_hours"] = 9.5
    steps[0]["is_key"] = True
    steps[0]["setup_hours"] = 1.0
    steps.append(
        {
            "seq": (len(steps) + 1) * 10,
            "process_code": "QI",
            "process_name": "其它",
            "op_type": "self",
            "category": "",
            "work_center": "工位A",
            "setup_hours": 0,
            "unit_hours": 0.5,
            "hours_src": "manual",
            "samples": 0,
            "is_key": False,
            "inspect_required": False,
            "sop": "",
            "remark": "手工加的一行",
        }
    )
    # 调顺序：把最后一行的工序号改小，模拟拖到前面
    steps[-1]["seq"] = 5
    r = client.put(
        f"/api/routes/{rid}",
        json={"steps": steps, "batch_qty": 20, "title": "E2E 路线"},
    )
    check(r.status_code == 200, "保存成功")
    body = r.json()
    check(body["step_count"] == len(steps), f"工序行数 {body['step_count']}")
    # 批量 20，准备 1.0 → 0.05
    expect = body["unit_sum"] + 0.05
    check(abs(body["per_unit_hours"] - expect) < 0.001, f"含准备分摊 {body['per_unit_hours']}")

    got = client.get(f"/api/routes/{rid}").json()["steps"]
    # 断言必须按 seq 找行，不能按下标：上面的用例把新行 seq 改成了 5，
    # 它会排到第一位，got[0] 不是被改过的那行。
    first = next((s for s in got if s["seq"] == 10), None)
    check(first is not None and first["is_key"] is True, "关键工序标记已保存")
    check(first is not None and first["unit_hours"] == 9.5, "改过的工时已保存")
    check(
        any(s["remark"] == "手工加的一行" for s in got),
        "手工加的行已保存",
    )
    check(
        [s["seq"] for s in got] == sorted(s["seq"] for s in got),
        "工序号升序（拖拽后重排生效）",
    )

    # ---- 5. 发布
    print("\n[5] 发布")
    r = client.post(f"/api/routes/{rid}/publish", json={"new_version": False})
    check(r.status_code == 200 and r.json()["status"] == "released", "发布成功")
    check(r.json()["step_count"] == len(steps), "发布后工序行完整")

    # ---- 6. 升版：生成可编辑草稿，旧版继续作为基准
    print("\n[6] 升版")
    r = client.post(f"/api/routes/{rid}/publish", json={"new_version": True})
    check(r.status_code == 200, "升版成功")
    new_id = r.json()["id"]
    check(new_id != rid, "产生新版本记录")
    check(r.json()["version"] == 2, "版本号 2")
    check(r.json()["status"] == "draft", "新版是可编辑草稿")
    old = client.get(f"/api/routes/{rid}").json()
    check(old["status"] == "released", "旧版仍为已发布")
    check(old["step_count"] == len(steps), "旧版内容保留")

    # 发布新版后才归档旧版
    r = client.post(f"/api/routes/{new_id}/publish", json={"new_version": False})
    check(r.status_code == 200 and r.json()["status"] == "released", "新版发布成功")
    old = client.get(f"/api/routes/{rid}").json()
    check(old["status"] == "archived", "新版发布后旧版归档")

    # 同一组合只能有一条 released
    rel = client.get(
        "/api/routes", params={"product": product, "part": part, "status": "released"}
    ).json()
    check(rel["total"] == 1, "同一组合只保留一条已发布")

    # 撤销归档后又能改
    r = client.post(f"/api/routes/{rid}/restore", json={"to": "draft"})
    check(r.status_code == 200 and r.json()["status"] == "draft", "归档版本可撤销恢复")

    # ---- 7. 覆盖率应随之上升
    print("\n[7] 覆盖率更新")
    cov = client.get("/api/routes/coverage", params={"limit": 1000}).json()
    check(cov["covered_count"] >= 1, f"已覆盖组合数 {cov['covered_count']}")
    check(
        cov["coverage_pct"] > 0,
        f"覆盖率 {cov['coverage_pct']}%（低价值组合占比很小，但必须 >0）",
    )
    hit = [i for i in cov["items"] if i["product"] == product and i["part"] == part]
    check(bool(hit) and hit[0]["state"] == "released", "该组合在覆盖清单里标记为已发布")
    check(
        cov["covered_count"] + cov["draft_count"] + cov["unbuilt_count"] == cov["total"],
        "组合数分类自洽",
    )

    # ---- 8. 归档后覆盖率回落
    print("\n[8] 归档")
    r = client.post(f"/api/routes/{new_id}/archive")
    check(r.status_code == 200 and r.json()["status"] == "archived", "归档成功")
    cov2 = client.get("/api/routes/coverage", params={"limit": 1000}).json()
    check(cov2["coverage_pct"] < cov["coverage_pct"], "归档后覆盖率回落")

    # ---- 清理
    # 只删本测试自己建的路线（created_by='e2e'）。
    # 早先这里按 product 名删，结果把首期建线的真草稿一起删了——
    # 本测试挑的是树里的真实产品，那个产品恰好有首期草稿。
    # 树改成按工时价值排序后，第一个被挑中的产品变了，
    # 被误删的路线也跟着变多，很难察觉。
    db = SessionLocal()
    for r in db.query(ProcessRoute).filter(ProcessRoute.created_by == "e2e").all():
        r.status = "draft"
        db.add(r)
        db.flush()
        db.delete(r)
    db.commit()
    left = db.query(ProcessRoute).filter(ProcessRoute.created_by == "e2e").count()
    # 工序行要限定在本测试范围内：用全表计数会把首期建线的真草稿算成「残留」
    left_s = db.query(ProcessRouteStep).filter(
        ProcessRouteStep.route_id.in_(
            db.query(ProcessRoute.id).filter(ProcessRoute.created_by == "e2e")
        )
    ).count()
    check(left == 0, f"清理后无残留（{left}）")
    check(left_s == 0, f"清理后无残留工序行（{left_s}）")
    drop_fixture(db)
    check(
        db.query(StandardHour).filter(StandardHour.product == FIX_PRODUCT).count() == 0,
        "待建夹具已删除（否则它会一直挂在覆盖看板上）",
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
