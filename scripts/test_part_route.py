"""结构里「编制工序」（PartRouteDialog）用到的接口链。

用法：python scripts/test_part_route.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

为什么单独测这一条链：新弹窗**不碰任务**，直接对「产品 × 件名」的全厂标准工艺
做 新建 → 改草稿 → 发布 → 升版 → 再发布。它和「生产任务 → 编辑工序」是两套语义
（那套走 /api/tasks/{id}/routes/{tr}/steps），但两套都写 process_route 表，
都受 uq_route_released（同一产品件名只允许一条已发布）约束。走错一步的结果是
要么撞唯一索引，要么把历史单据的比对基准悄悄改掉 —— 后者不会报错。

七组：
  [1] 新建：POST /api/routes 直接带工序 → 草稿 v1，工序数/工时对得上
  [2] 按产品×件名能查回来（弹窗打开时 /api/routes?product&part 的兜底查询）
  [3] 已发布不许就地改：PUT → 400，且库里的工序一个字都没动
  [4] 发布：草稿 → released；再发一次 → 400
  [5] 升版：revision → 新草稿 v2，旧版**仍是 released**（改的期间不出现基准空窗）
  [6] 新草稿发布后：v2 released、v1 archived，同一产品件名只有一条 released
  [7] 清理：路线、结构节点、工时组合全部删干净
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AppUser, ProcessRoute, ProductNode, WorkCombo  # noqa: E402
from app.security import make_token  # noqa: E402

PRODUCT = "ZZPRT测试产品"
PART = "ZZPRT测试件"

PASS = 0
FAIL: list[str] = []


def check(cond, msg: str) -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  OK   {msg}")
    else:
        FAIL.append(msg)
        print(f"  FAIL {msg}")


def section(name: str) -> None:
    print(f"\n[{name}]")


def auth(user: AppUser) -> dict:
    tok = make_token(user.username, int(user.token_epoch or 0))
    return {"Authorization": f"Bearer {tok['token']}"}


def step(seq: int, code: str, name: str, unit: float, setup: float = 0.0) -> dict:
    return {
        "seq": seq,
        "process_code": code,
        "process_name": name,
        "op_type": "self",
        "category": "机加工",
        "work_center": "1号车床",
        "setup_hours": setup,
        "unit_hours": unit,
        "is_key": False,
        "inspect_required": False,
        "remark": "",
    }


def purge() -> None:
    """测试前后都能跑：按产品+件名清掉路线、结构节点、工时组合。"""
    with SessionLocal() as db:
        for r in db.scalars(
            select(ProcessRoute).where(
                ProcessRoute.product == PRODUCT, ProcessRoute.part == PART
            )
        ).all():
            db.delete(r)
        for n in db.scalars(
            select(ProductNode).where(
                ProductNode.product == PRODUCT, ProductNode.part == PART
            )
        ).all():
            db.delete(n)
        for c in db.scalars(
            select(WorkCombo).where(WorkCombo.product == PRODUCT, WorkCombo.part == PART)
        ).all():
            db.delete(c)
        db.commit()


def main() -> int:
    purge()

    with SessionLocal() as db:
        admin = db.scalar(select(AppUser).where(AppUser.role == "admin"))
    if admin is None:
        print("没有管理员账号，无法跑接口用例")
        return 2

    c = TestClient(app)
    c.headers.update(auth(admin))

    section("1. 新建：直接带工序建草稿")
    r = c.post(
        "/api/routes",
        json={
            "product": PRODUCT,
            "part": PART,
            "steps": [step(10, "C01", "车", 0.5, 0.2), step(20, "M01", "铣", 1.25, 0.0)],
            "created_by": "test",
        },
    )
    check(r.status_code == 200, f"POST /api/routes → 200（实际 {r.status_code}）")
    d = r.json() if r.status_code == 200 else {}
    rid1 = d.get("id")
    check(d.get("status") == "draft", f"新建出来是草稿（{d.get('status')}）")
    check(d.get("version") == 1, f"版本是 v1（{d.get('version')}）")
    check(d.get("step_count") == 2, f"带进去 2 道工序（{d.get('step_count')}）")
    check(abs((d.get("unit_sum") or 0) - 1.75) < 1e-6, f"单件合计 1.75h（{d.get('unit_sum')}）")

    section("2. 按产品×件名查得回来（弹窗的兜底查询）")
    r = c.get("/api/routes", params={"product": PRODUCT, "part": PART})
    items = (r.json() or {}).get("items") or []
    check(r.status_code == 200 and len(items) == 1, f"查到 1 条（{len(items)}）")
    check(items[0]["id"] == rid1 if items else False, "查到的就是刚建的那条")

    section("3. 草稿可改")
    r = c.put(
        f"/api/routes/{rid1}",
        json={"steps": [step(10, "C01", "车", 0.5, 0.2), step(20, "M01", "铣", 1.25), step(30, "G01", "磨", 0.4)]},
    )
    check(r.status_code == 200 and r.json().get("step_count") == 3, f"改成 3 道（{r.status_code}）")

    section("4. 发布 → 已发布；重复发布被拦")
    r = c.post(f"/api/routes/{rid1}/publish", json={"new_version": False})
    check(r.status_code == 200 and r.json().get("status") == "released", f"发布成功（{r.status_code}）")
    r = c.post(f"/api/routes/{rid1}/publish", json={"new_version": False})
    check(r.status_code == 400, f"再发一次被拦（实际 {r.status_code}）")

    section("5. 已发布不许就地改（这是本弹窗最要紧的红线）")
    r = c.put(f"/api/routes/{rid1}", json={"steps": [step(10, "C01", "车", 9.9)]})
    check(r.status_code == 400, f"PUT 已发布 → 400（实际 {r.status_code}）")
    r = c.get(f"/api/routes/{rid1}")
    steps_now = (r.json() or {}).get("steps") or []
    check(len(steps_now) == 3, f"库里仍是 3 道（{len(steps_now)}）")
    check(
        all(abs(float(s["unit_hours"]) - 9.9) > 1e-6 for s in steps_now),
        "工时没有被悄悄改掉",
    )

    section("6. 升版：新草稿 v2，旧版继续生效")
    r = c.post(f"/api/routes/{rid1}/revision")
    check(r.status_code == 200, f"revision → 200（实际 {r.status_code}）")
    d2 = r.json() if r.status_code == 200 else {}
    rid2 = d2.get("id")
    check(d2.get("status") == "draft", f"升出来是草稿（{d2.get('status')}）")
    check(d2.get("version") == 2, f"版本 v2（{d2.get('version')}）")
    check(d2.get("step_count") == 3, f"工序被复制过来（{d2.get('step_count')}）")
    r = c.get(f"/api/routes/{rid1}")
    check(r.json().get("status") == "released", "旧版仍是 released（没有出现基准空窗）")

    section("7. 新草稿可改可发；发布后旧版归档")
    r = c.put(f"/api/routes/{rid2}", json={"steps": [step(10, "C01", "车", 0.6), step(20, "M01", "铣", 1.25)]})
    check(r.status_code == 200 and r.json().get("step_count") == 2, f"新草稿改成 2 道（{r.status_code}）")
    r = c.post(f"/api/routes/{rid2}/publish", json={"new_version": False})
    check(r.status_code == 200 and r.json().get("status") == "released", f"v2 发布成功（{r.status_code}）")
    r = c.get(f"/api/routes/{rid1}")
    check(r.json().get("status") == "archived", f"旧版自动归档（{r.json().get('status')}）")
    r = c.get("/api/routes", params={"product": PRODUCT, "part": PART, "status": "released"})
    check(len(r.json().get("items") or []) == 1, "同一产品×件名只有一条已发布")

    section("8. 清理")
    r = c.post(f"/api/routes/{rid2}/archive")
    check(r.status_code == 200, f"归档 v2（{r.status_code}）")
    for rid in (rid1, rid2):
        r = c.delete(f"/api/routes/{rid}")
        check(r.status_code == 200, f"删除 route {rid}（{r.status_code}）")
    purge()
    with SessionLocal() as db:
        left = db.scalars(
            select(ProcessRoute).where(
                ProcessRoute.product == PRODUCT, ProcessRoute.part == PART
            )
        ).all()
        nodes = db.scalars(
            select(ProductNode).where(
                ProductNode.product == PRODUCT, ProductNode.part == PART
            )
        ).all()
    check(not left, f"路线删干净（剩 {len(left)}）")
    check(not nodes, f"结构节点删干净（剩 {len(nodes)}）")

    print(f"\n{PASS} 项通过，{len(FAIL)} 项失败")
    if FAIL:
        for f in FAIL:
            print("  · " + f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
