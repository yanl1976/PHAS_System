"""工艺路线 API 的接口级测试（FastAPI TestClient）。

不依赖外部服务：直接用 app.main:app，测试库里跑真实 HTTP 语义。
跑法：.venv/Scripts/python.exe scripts/test_routes_api.py
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
    WorkCombo,
)

FAILED: list[str] = []
P = "接口测试产品"
PT = "接口测试件名"
# 「待建清单里有、但还没建线」的那一件。以前是从真实库里捞的，可真实库一旦
# 补齐全量工艺就一条都捞不到 —— 测试不能依赖生产数据恰好不完整，自己造。
# 注意待建清单的口径来自 standard_hour（历史工时），不是 work_combo，
# 所以要造的是「有历史工时、没路线」的行，光造组合不会出现在清单里。
PT_UNBUILT = "接口测试待建件"


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def cleanup(db) -> None:
    for r in db.query(ProcessRoute).filter(ProcessRoute.product == P).all():
        r.status = "draft"
        db.add(r)
        db.flush()
        db.delete(r)
    db.query(WorkCombo).filter(WorkCombo.product == P).delete()
    db.query(StandardHour).filter(StandardHour.product == P).delete()
    db.commit()


def main() -> int:
    init_db()
    db = SessionLocal()
    cleanup(db)
    # 造一条「有历史工时、没路线」的待建件，供覆盖看板断言 unbuilt 用
    db.add(
        StandardHour(
            product=P,
            part=PT_UNBUILT,
            process="车",
            unit_hours=0.5,
            samples=3,
            agreement=1.0,
            active=True,
        )
    )
    db.commit()
    db.close()

    client = TestClient(app)

    # ---------------------------------------------------- 1. 列表
    print("\n[1] 列表")
    r = client.get("/api/routes")
    check(r.status_code == 200, f"GET /api/routes → {r.status_code}")
    check("items" in r.json() and "total" in r.json(), "列表返回 items/total")

    r = client.get("/api/routes", params={"q": "接口测试产品"})
    check(r.status_code == 200, "按关键字筛选可用")

    # ---------------------------------------------------- 2. 生成候选
    print("\n[2] 生成候选（不落库）")
    r = client.get("/api/routes/suggest", params={"product": "SJ2栓剂", "part": "主动轴"})
    check(r.status_code == 200, f"suggest → {r.status_code}")
    body = r.json()
    check(body["total"] > 0, f"候选工序 {body['total']} 道")
    if body["steps"]:
        s0 = body["steps"][0]
        check(
            all(k in s0 for k in ("seq", "process_code", "process_name", "unit_hours", "order_source")),
            "候选行字段完整",
        )
        check(
            [x["seq"] for x in body["steps"]] == sorted(x["seq"] for x in body["steps"]),
            "候选工序号递增",
        )

    # 缺参数应报错
    r = client.get("/api/routes/suggest", params={"product": "只有产品"})
    check(r.status_code == 400, "缺件名返回 400")

    # ---------------------------------------------------- 3. 新建草稿
    print("\n[3] 新建草稿")
    r = client.post(
        "/api/routes",
        json={"product": P, "part": PT, "created_by": "tester", "batch_qty": 10},
    )
    check(r.status_code == 200, f"POST /api/routes → {r.status_code}")
    rid = r.json()["id"]
    check(r.json()["status"] == "draft", "新建物为草稿")
    check(r.json()["version"] == 1, "新建物版本号为 1")

    # 重复新建同一组合 → 409
    r = client.post("/api/routes", json={"product": P, "part": PT})
    check(r.status_code == 409, "重复新建同组合返回 409（防版本号分叉）")

    # 缺件名 → 400
    r = client.post("/api/routes", json={"product": "只有产品"})
    check(r.status_code == 400, "缺件名返回 400")

    # ---------------------------------------------------- 4. 保存工序行
    print("\n[4] 保存工序行")
    steps = [
        {
            "seq": 10,
            "process_code": "CHE",
            "process_name": "车",
            "unit_hours": 1.5,
            "setup_hours": 0.5,
            "is_key": True,
        },
        {
            "seq": 20,
            "process_code": "XI",
            "process_name": "铣",
            "unit_hours": 0.8333,
        },
    ]
    r = client.put(f"/api/routes/{rid}", json={"steps": steps, "title": "接口测试路线"})
    check(r.status_code == 200, f"PUT 工序行 → {r.status_code}")
    body = r.json()
    check(body["step_count"] == 2, f"工序行数 {body['step_count']}")
    check(abs(body["unit_sum"] - 2.3333) < 0.001, f"单件合计 {body['unit_sum']}")
    # 批量 10，准备 0.5 → 0.05 分摊
    check(abs(body["per_unit_hours"] - 2.3833) < 0.001, f"含准备分摊 {body['per_unit_hours']}")

    # ---------------------------------------------------- 5. 详情
    print("\n[5] 详情")
    r = client.get(f"/api/routes/{rid}")
    check(r.status_code == 200, "详情可取")
    check(len(r.json()["steps"]) == 2, "详情含 2 道工序")
    check(r.json()["steps"][0]["is_key"] is True, "关键工序标记保留")

    r = client.get("/api/routes/999999")
    check(r.status_code == 404, "不存在的路线返回 404")

    # ---------------------------------------------------- 6. 发布
    print("\n[6] 发布")
    r = client.post(f"/api/routes/{rid}/publish", json={"new_version": False})
    check(r.status_code == 200, f"发布 → {r.status_code}")
    check(r.json()["status"] == "released", "发布后状态为 released")

    # 重复发布 → 400
    r = client.post(f"/api/routes/{rid}/publish", json={"new_version": False})
    check(r.status_code == 400, "重复发布返回 400")

    # 已发布不能直接改
    r = client.put(f"/api/routes/{rid}", json={"title": "偷改"})
    check(r.status_code == 400, "已发布路线不能直接修改")

    # 不能删已发布
    r = client.delete(f"/api/routes/{rid}")
    check(r.status_code == 400, "已发布路线不能删除")

    # ---------------------------------------------------- 7. 升版
    print("\n[7] 升版（生成可编辑草稿，旧版继续作为基准）")
    r = client.post(f"/api/routes/{rid}/publish", json={"new_version": True})
    check(r.status_code == 200, f"升版 → {r.status_code}")
    new_id = r.json()["id"]
    check(new_id != rid, "升版产生新记录")
    check(r.json()["version"] == 2, "新版版本号为 2")
    check(r.json()["status"] == "draft", "升版产生的是可编辑草稿")

    r = client.get(f"/api/routes/{rid}")
    check(r.json()["status"] == "released", "旧版仍为已发布（基准不断档）")

    # 新版发布后旧版才归档
    r = client.post(f"/api/routes/{new_id}/publish", json={"new_version": False})
    check(r.status_code == 200 and r.json()["status"] == "released", "新版发布")
    r = client.get(f"/api/routes/{rid}")
    check(r.json()["status"] == "archived", "新版发布后旧版归档")

    # 同一组合仍只有一条 released
    r = client.get("/api/routes", params={"product": P, "part": PT, "status": "released"})
    check(r.json()["total"] == 1, "同一组合只保留一条已发布")

    # ---------------------------------------------------- 7.1 撤销归档 / 撤回发布
    print("\n[7.1] 撤销归档 / 撤回发布")
    r = client.post(f"/api/routes/{rid}/restore", json={"to": "draft"})
    check(r.status_code == 200 and r.json()["status"] == "draft", "归档版本可恢复为草稿")

    r = client.post(f"/api/routes/{rid}/restore", json={"to": "released"})
    check(r.status_code == 400, "已有基准时不能恢复成第二条已发布")

    r = client.post(f"/api/routes/{new_id}/reopen")
    check(r.status_code == 200 and r.json()["status"] == "draft", "撤回发布回到草稿")

    # 把刚恢复的草稿重新发布，恢复成单基准状态
    r = client.post(f"/api/routes/{new_id}/publish", json={"new_version": False})
    check(r.status_code == 200 and r.json()["status"] == "released", "重新发布成功")

    # ---------------------------------------------------- 8. 复制
    print("\n[8] 复制")
    r = client.post(f"/api/routes/{new_id}/copy")
    check(r.status_code == 200, "复制成功")
    copy_id = r.json()["id"]
    check(r.json()["status"] == "draft", "复制出来是草稿")
    check(r.json()["step_count"] == 2, "复制带着工序行")

    r = client.delete(f"/api/routes/{copy_id}")
    check(r.status_code == 200, "草稿可删除")

    # ---------------------------------------------------- 9. 归档
    print("\n[9] 归档")
    r = client.post(f"/api/routes/{new_id}/archive")
    check(r.status_code == 200, "归档成功")
    check(r.json()["status"] == "archived", "状态为 archived")

    # ---------------------------------------------------- 10. 覆盖率 / 树
    print("\n[10] 覆盖率与树")
    r = client.get("/api/routes/coverage", params={"limit": 20})
    check(r.status_code == 200, "覆盖率可取")
    cov = r.json()
    check(cov["total"] > 0, f"待建清单 {cov['total']} 条")
    check(
        cov["items"][0]["value"] >= cov["items"][-1]["value"],
        "清单按价值降序",
    )

    r = client.get("/api/routes/tree")
    check(r.status_code == 200, "树可取")
    tree = r.json()
    check(tree["total"] > 0, f"树有 {tree['total']} 个产品节点")
    has_part = any(
        x["part"] == PT for t in tree["items"] for x in t["parts"]
    )
    check(has_part, "树里能看到测试件名")
    has_unbuilt = any(
        x["part"] == PT_UNBUILT and x["state"] == "none"
        for t in tree["items"] for x in t["parts"]
    )
    check(has_unbuilt, "树里包含未建路线的组合（本测试自建的未建件）")

    r = client.get("/api/routes/tree", params={"only_unbuilt": True})
    check(r.status_code == 200, "只看未建可用")
    check(
        all(
            x["state"] == "none"
            for t in r.json()["items"]
            for x in t["parts"]
        ),
        "只看未建时只返回 none 状态",
    )

    # ---------------------------------------------------- 清理
    db = SessionLocal()
    cleanup(db)
    left_r = db.query(ProcessRoute).filter(ProcessRoute.product == P).count()
    left_s = db.query(ProcessRouteStep).filter(
        ProcessRouteStep.route_id.in_(
            db.query(ProcessRoute.id).filter(ProcessRoute.product == P)
        )
    ).count()
    check(left_r == 0, f"清理后无残留路线（剩 {left_r}）")
    check(left_s == 0, f"清理后无残留工序行（剩 {left_s}）")
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
