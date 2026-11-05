"""工时组合台账 + 单据打印 接口测试。

原则：只用自己造的组合（前缀 ZZTEST），跑完按名字清干净；
打印用现成的真实单据只读验证，不改任何业务数据。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import ProcessRoute, StandardHour, WorkCombo  # noqa: E402

TAG = "ZZTEST"
PRODUCT = f"{TAG}产品"
PART = f"{TAG}件名"
PART2 = f"{TAG}件名改"

PASSED: list[str] = []
FAILED: list[str] = []


def check(ok: bool, label: str) -> None:
    if ok:
        PASSED.append(label)
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def cleanup() -> None:
    db = SessionLocal()
    for h in db.scalars(select(StandardHour)).all():
        if (h.product or "").startswith(TAG) or (h.part or "").startswith(TAG):
            db.delete(h)
    for r in db.scalars(select(ProcessRoute)).all():
        if (r.product or "").startswith(TAG) or (r.part or "").startswith(TAG):
            db.delete(r)
    db.commit()
    db.close()


def _expected_combos() -> tuple[int, int]:
    """从库里实时算「组合总数 / 产品为空的组合数」。

    原来这里写死 996 / 195。P1 迁移把同一产品的 6 种历史写法归一、
    并给 106 个空产品按唯一件名补回产品，这两个数字必然变。
    写死就变成「数据一动测试就红」，改成按台账自己的口径现算：
    历史工时与基线路线的 (产品,件名) 并集，件名为空的不算组合。
    """
    with SessionLocal() as db:
        keys: set[tuple[str, str]] = set()
        for p, pt in db.execute(
            select(StandardHour.product, StandardHour.part).distinct()
        ).all():
            p, pt = (p or "").strip(), (pt or "").strip()
            if pt:
                keys.add((p, pt))
        for p, pt in db.execute(
            select(ProcessRoute.product, ProcessRoute.part)
            .where(ProcessRoute.kind == "route")
            .distinct()
        ).all():
            p, pt = (p or "").strip(), (pt or "").strip()
            if pt:
                keys.add((p, pt))
        combo_total = db.scalar(select(func.count()).select_from(WorkCombo)) or 0
        return combo_total, len({k for k in keys if not k[0]})


def _source_keys() -> set[tuple[str, str]]:
    """「应该有组合身份」的来源：历史工时 ∪ **已发布**路线。

    草稿路线不算：身份行是发布时才登记的（草稿只是"这版工艺在编"，
    `create_route` 会建结构节点但不建 `work_combo`）。把草稿也算进来，
    任何人新建一条草稿就会让这条断言变红，而台账本来就不该有它。
    """
    with SessionLocal() as db:
        keys: set[tuple[str, str]] = set()
        for p, pt in db.execute(
            select(StandardHour.product, StandardHour.part).distinct()
        ).all():
            p, pt = (p or "").strip(), (pt or "").strip()
            if pt:
                keys.add((p, pt))
        for p, pt in db.execute(
            select(ProcessRoute.product, ProcessRoute.part)
            .where(ProcessRoute.kind == "route", ProcessRoute.status == "released")
            .distinct()
        ).all():
            p, pt = (p or "").strip(), (pt or "").strip()
            if pt:
                keys.add((p, pt))
        return keys


def main() -> int:
    client = TestClient(app)
    cleanup()
    expect_total, expect_issue = _expected_combos()

    # ---------------------------------------------------- 1. 台账列表
    print("\n[1] 组合台账")
    d = client.get("/api/combos", params={"limit": 1000}).json()
    # 组合身份表现在是权威口径：身份一旦登记就保留，来源（工时行、路线）被删
    # 也不会让组合凭空消失 —— 所以「台账 ≥ 来源并集」才对，不能用等号。
    check(d["total"] == expect_total, f"组合总数等于身份表行数 {expect_total}（实际 {d['total']}）")
    listed = {(x["product"], x["part"]) for x in d["items"]}
    missing = _source_keys() - listed
    check(not missing, f"来源里的组合一条不漏地出现在台账里（缺 {sorted(missing)[:3]}）")
    s = d["summary"]
    # 「无路线」不再是独立状态：组合状态就是 draft/released/archived，
    # unbuilt 只是 draft 里还没建工艺的那部分，不能重复计入
    check(
        s["released"] + s["draft"] + s["archived"] == d["total"],
        f"三种状态合计等于总数（{s['released']}+{s['draft']}+{s['archived']}={d['total']}）",
    )
    check(s["unbuilt"] <= s["draft"], f"无路线的都在草稿里（{s['unbuilt']} ≤ {s['draft']}）")
    vals = [x["value"] for x in d["items"]]
    check(vals == sorted(vals, reverse=True), "按工时价值降序")
    issue = client.get("/api/combos", params={"only_issue": True, "limit": 1000}).json()
    check(
        issue["total"] == expect_issue,
        f"产品名为空的组合 {expect_issue} 条（实际 {issue['total']}）",
    )

    # ---------------------------------------------------- 2. 新建组合
    print("\n[2] 新建组合")
    r = client.post(
        "/api/combos",
        json={"product": PRODUCT, "part": PART, "steps": [{"process": "车", "unit_hours": 1.5}]},
    )
    check(r.status_code == 200, f"创建成功 → {r.status_code}")
    check(r.json()["steps"] == 1, "建了 1 道工序")

    r = client.post(
        "/api/combos",
        json={"product": PRODUCT, "part": PART, "steps": []},
    )
    check(r.status_code == 400, "重复组合被拒")

    lst = client.get("/api/combos", params={"q": TAG, "limit": 50}).json()
    check(lst["total"] == 1, "新组合出现在台账里")
    check(
        lst["items"][0]["state"] == "draft" and not lst["items"][0]["has_route"],
        "新组合是草稿且还没建路线",
    )

    # ---------------------------------------------------- 3. 工序维护
    print("\n[3] 工序维护")
    det = client.get("/api/combos/detail", params={"product": PRODUCT, "part": PART}).json()
    check(len(det["steps"]) == 1 and det["steps"][0]["process"] == "车", "明细带出 1 道工序")
    check(abs(det["unit_sum"] - 1.5) < 1e-6, "单件合计 1.5")

    r = client.post(
        "/api/combos/step",
        json={"product": PRODUCT, "part": PART, "process": "铣", "unit_hours": 2.0},
    )
    check(r.status_code == 200, "加一道工序")
    det = client.get("/api/combos/detail", params={"product": PRODUCT, "part": PART}).json()
    check(len(det["steps"]) == 2, "明细变 2 道")
    check(abs(det["unit_sum"] - 3.5) < 1e-6, "单件合计 3.5")

    # 同工序再加一次 = 改，不是重复插入
    client.post(
        "/api/combos/step",
        json={"product": PRODUCT, "part": PART, "process": "铣", "unit_hours": 2.5},
    )
    det = client.get("/api/combos/detail", params={"product": PRODUCT, "part": PART}).json()
    check(len(det["steps"]) == 2, "同工序重复提交不会多出一行")
    check(abs(det["unit_sum"] - 4.0) < 1e-6, "改成 2.5 后合计 4.0")

    step_id = det["steps"][0]["id"]
    r = client.put(f"/api/combos/step/{step_id}", json={"process": "车", "unit_hours": 1.0})
    check(r.status_code == 200, "改单件工时")
    det = client.get("/api/combos/detail", params={"product": PRODUCT, "part": PART}).json()
    check(abs(det["unit_sum"] - 3.5) < 1e-6, "改完合计回到 3.5")

    r = client.delete(f"/api/combos/step/{step_id}")
    check(r.status_code == 200, "删工序")
    det = client.get("/api/combos/detail", params={"product": PRODUCT, "part": PART}).json()
    check(len(det["steps"]) == 1, "剩 1 道")

    # ---------------------------------------------------- 4. 改名
    print("\n[4] 改名")
    r = client.post(
        "/api/combos/rename",
        json={"product": PRODUCT, "part": PART, "new_product": PRODUCT, "new_part": PART2},
    )
    check(r.status_code == 200 and r.json()["changed"] == 1, "改名影响 1 条工时记录")
    det = client.get("/api/combos/detail", params={"product": PRODUCT, "part": PART2}).json()
    check(det["part"] == PART2 and len(det["steps"]) == 1, "改名后明细还在")

    # ---------------------------------------------------- 5. 停用 / 启用
    print("\n[5] 停用 / 启用")
    client.post("/api/combos/active", json={"product": PRODUCT, "part": PART2, "active": False})
    lst = client.get("/api/combos", params={"q": TAG, "limit": 50}).json()
    # 台账默认显示全部组合：历史工时行停用不等于这条组合不存在
    check(lst["total"] == 1 and lst["items"][0]["active"] is False, "停用后默认仍能看到，且标记为停用")
    lst = client.get(
        "/api/combos", params={"q": TAG, "include_disabled": False, "limit": 50}
    ).json()
    check(lst["total"] == 0, "只看在用时才被筛掉")
    client.post("/api/combos/active", json={"product": PRODUCT, "part": PART2, "active": True})
    lst = client.get("/api/combos", params={"q": TAG, "limit": 50}).json()
    check(lst["total"] == 1, "启用后重新出现")

    # ---------------------------------------------------- 6. 打印
    print("\n[6] 加工路线单打印")
    r = client.get("/api/sheets/8/print.pdf")
    check(r.status_code == 200, f"打印接口 → {r.status_code}")
    check(r.headers["content-type"] == "application/pdf", "返回 application/pdf")
    raw = r.content
    check(raw[:5] == b"%PDF-", "是合法 PDF")
    box = re.search(rb"/MediaBox\s*\[([^\]]+)\]", raw)
    check(box is not None and b"841.68" in box.group(1), "纸张 A4 横向（841.68×595.2pt）")

    pages = client.get("/api/sheets/8/print.pdf", params={"dpi": 150, "download": True})
    check(pages.status_code == 200, "下载模式可用")
    check("attachment" in pages.headers["content-disposition"], "download=true 时为附件下载")

    # 明细 7 行 -> 单页；超过 10 行才续页
    from app.render import route_form as form

    vals = {"rows": [{"process": f"工序{i}"} for i in range(23)], "subtotal": "1:00:00"}
    check(len(form.render_pages(vals, dpi=80)) == 3, "23 行明细拆成 3 页")
    check(len(form.render_pages({"rows": [{"process": "x"}] * 10}, dpi=80)) == 1, "10 行不拆页")

    # ---------------------------------------------------- 7. 路线状态机
    print("\n[7] 归档撤销 / 发布后修改")
    r = client.post(
        "/api/routes",
        json={
            "product": PRODUCT,
            "part": PART2,
            "steps": [{"seq": 10, "process_code": "CHE", "process_name": "车", "unit_hours": 1.0}],
        },
    )
    rid = r.json()["id"]
    client.post(f"/api/routes/{rid}/publish", json={"new_version": False})
    r = client.post(f"/api/routes/{rid}/revision")
    check(r.status_code == 200 and r.json()["status"] == "draft", "升版得到可编辑草稿")
    new_id = r.json()["id"]
    check(
        client.get(f"/api/routes/{rid}").json()["status"] == "released",
        "升版后旧版仍为已发布",
    )
    client.post(f"/api/routes/{new_id}/publish", json={"new_version": False})
    check(
        client.get(f"/api/routes/{rid}").json()["status"] == "archived",
        "新版发布后旧版归档",
    )
    r = client.post(f"/api/routes/{rid}/restore", json={"to": "draft"})
    check(r.status_code == 200 and r.json()["status"] == "draft", "归档可撤销恢复")
    r = client.post(f"/api/routes/{new_id}/reopen")
    check(r.status_code == 200 and r.json()["status"] == "draft", "撤回发布回到草稿")

    # ---------------------------------------------------- 清理
    cleanup()
    db = SessionLocal()
    left = [
        h.id
        for h in db.scalars(select(StandardHour)).all()
        if (h.product or "").startswith(TAG) or (h.part or "").startswith(TAG)
    ]
    left_r = [
        r.id
        for r in db.scalars(select(ProcessRoute)).all()
        if (r.product or "").startswith(TAG) or (r.part or "").startswith(TAG)
    ]
    db.close()
    check(left == [], f"清理后无残留工时行（{len(left)}）")
    check(left_r == [], f"清理后无残留路线（{len(left_r)}）")

    print("\n" + "=" * 56)
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print(f"全部通过（{len(PASSED)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
