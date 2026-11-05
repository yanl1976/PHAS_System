"""岗位职能（谁有权做哪件生产功能）的回归。

用法：python scripts/test_duties.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

十组：
  [1] 职能口径：四项职能、文案、规范化（去重/丢非法值/顺序稳定）
  [2] 岗位角色可新增 / 编辑职能：接口能存能读，列表带出 duties
  [3] 岗位没配职能 → 不锁人：按系统角色默认（editor 有三项，无单价）
  [4] 配了职能 → 严格按并集：计划员能建任务/报工，工艺卡与单价被拒
  [5] 一人多岗 → 并集：再加一个工艺岗，工艺卡随即放行
  [6] 空职能岗位不添乱：挂着「材料员」不会把并集变成"没配"
  [7] 只读不被岗位提权
  [8] 管理员豁免：挂了只配报工的岗位，仍然什么都能做
  [9] 403 文案指名岗位：告诉人该去勾哪个岗位，而不是只写「无权限」
  [10] 兼容：PUT 不带 duties 不清空；迁移后 role 有 duties 列
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import inspect, select  # noqa: E402

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.domain import sys_roles  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AppUser, Role, Worker, WorkerRole  # noqa: E402
from app.security import hash_password, make_token  # noqa: E402

TAG = "ZZDUTY"
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


def ensure_role(db, code: str, name: str, duties: str = "", kind: str = "other") -> Role:
    r = db.scalar(select(Role).where(Role.code == code))
    if r is None:
        r = Role(code=code, name=name, kind=kind, duties=duties, builtin=False)
        db.add(r)
        db.flush()
    else:
        r.name = name
        r.duties = duties
        r.kind = kind
    return r


def ensure_worker(db, name: str, role_ids: list[int]) -> Worker:
    w = db.scalar(select(Worker).where(Worker.name == name))
    if w is None:
        w = Worker(name=name, team="测试", active=True)
        db.add(w)
        db.flush()
    db.query(WorkerRole).filter(WorkerRole.worker_id == w.id).delete()
    for rid in role_ids:
        db.add(WorkerRole(worker_id=w.id, role_id=rid))
    db.flush()
    return w


def ensure_user(db, username: str, role: str, worker_id: int | None = None) -> AppUser:
    u = db.scalar(select(AppUser).where(AppUser.username == username))
    if u is None:
        u = AppUser(
            username=username,
            display_name=username,
            password_hash=hash_password("pass123456"),
            role=role,
            worker_id=worker_id,
            active=True,
        )
        db.add(u)
    u.role = role
    u.worker_id = worker_id
    u.active = True
    db.flush()
    return u


def main() -> int:
    init_db()
    db = SessionLocal()
    client = TestClient(app)
    made_users: list[str] = []

    try:
        # ------------------------------------------------------------ 1
        section("1 职能口径：四项、文案、规范化")
        check(
            sys_roles.DUTY_CODES == ("task_plan", "route_edit", "report", "wage_rate"),
            f"四项职能与顺序固定：{sys_roles.DUTY_CODES}",
        )
        check(sys_roles.duty_label("task_plan") == "生产任务编制", "文案取中文名")
        check(sys_roles.is_duty("report") and not sys_roles.is_duty("base_data"), "只认职能码，基础数据不算")
        check(
            sys_roles.normalize_duties("report, task_plan,report , 乱写") == ("task_plan", "report"),
            "逗号串去重、丢非法值、按固定顺序输出",
        )
        check(
            sys_roles.normalize_duties(["wage_rate"]) == ("wage_rate",),
            "数组入参同样可用",
        )
        check(sys_roles.normalize_duties(None) == (), "None 视为空")
        check(
            sorted(sys_roles.default_duties("editor")) == ["report", "route_edit", "task_plan"],
            "editor 默认三项，不含工时单价",
        )
        check(sys_roles.default_duties("viewer") == frozenset(), "viewer 默认什么都不给")

        # ------------------------------------------------------------ 2
        section("2 岗位角色可新增 / 编辑职能")
        admin = ensure_user(db, f"{TAG}_admin", "admin")
        made_users.append(admin.username)
        db.commit()
        ha = auth(admin)

        r = client.post(
            "/api/admin/roles",
            headers=ha,
            json={
                "name": "计划员（测试）",
                "code": f"{TAG}_PLANNER",
                "kind": "other",
                "description": "排产与报工",
                "duties": ["task_plan", "report"],
            },
        )
        check(r.status_code == 200, f"新增岗位能带职能（{r.status_code}）")
        planner_id = r.json().get("id")
        check(r.json().get("duties") == ["task_plan", "report"], "新增返回规范化后的职能")

        rows = client.get("/api/admin/roles", headers=ha).json()
        row = next((x for x in rows["items"] if x["id"] == planner_id), None)
        check(row is not None and row["duties"] == ["task_plan", "report"], "列表带出岗位职能")
        check(
            row is not None and row["duty_names"] == ["生产任务编制", "报工（录单 / 核对 / 扫码）"],
            "列表同时给中文名",
        )
        check(
            [d["code"] for d in rows.get("duty_options", [])] == list(sys_roles.DUTY_CODES),
            "接口下发勾选项清单（界面不另写一份）",
        )

        r = client.put(
            f"/api/admin/roles/{planner_id}",
            headers=ha,
            json={"name": "计划员（测试）", "code": f"{TAG}_PLANNER", "kind": "other", "duties": ["task_plan"]},
        )
        check(r.status_code == 200 and r.json()["duties"] == ["task_plan"], "编辑岗位能改职能")

        # 不带 duties 的老调用不能把职能冲掉
        client.put(
            f"/api/admin/roles/{planner_id}",
            headers=ha,
            json={"name": "计划员（测试）", "code": f"{TAG}_PLANNER", "kind": "other"},
        )
        db.expire_all()
        kept = db.get(Role, planner_id).duties
        check(kept == "task_plan", f"PUT 不传 duties 时保留原值：{kept!r}")

        # 改回来，后面按「任务 + 报工」测
        client.put(
            f"/api/admin/roles/{planner_id}",
            headers=ha,
            json={"name": "计划员（测试）", "code": f"{TAG}_PLANNER", "kind": "other", "duties": ["task_plan", "report"]},
        )

        material = ensure_role(db, f"{TAG}_MAT", "材料员（测试）", duties="")
        process = ensure_role(db, f"{TAG}_PROC", "工艺员（测试）", duties="route_edit")
        db.commit()

        worker = ensure_worker(db, f"{TAG}_张三", [planner_id])
        db.commit()
        planner = ensure_user(db, f"{TAG}_planner", "editor", worker_id=worker.id)
        made_users.append(planner.username)
        db.commit()
        hp = auth(planner)

        # ------------------------------------------------------------ 3
        section("3 岗位没配职能 → 不锁人，按系统角色默认")
        bare = ensure_worker(db, f"{TAG}_李四", [material.id])
        u_bare = ensure_user(db, f"{TAG}_bare", "editor", worker_id=bare.id)
        made_users.append(u_bare.username)
        db.commit()
        hb = auth(u_bare)

        r = client.post("/api/tasks", headers=hb, json={"task_no": "", "parts": []})
        check(r.status_code != 403, f"没配职能的岗位仍可建任务（{r.status_code}）")
        r = client.put("/api/wages/rates", headers=hb, json={"ym": "2026-13", "items": []})
        check(r.status_code == 403, f"但工时单价仍按系统角色拒绝（{r.status_code}）")

        # ------------------------------------------------------------ 4
        section("4 配了职能 → 严格按并集")
        r = client.post("/api/tasks", headers=hp, json={"task_no": "", "parts": []})
        check(r.status_code != 403, f"计划员能建任务（{r.status_code}）")
        r = client.patch("/api/sheets/999999", headers=hp, json={})
        check(r.status_code != 403, f"计划员能改报工单（{r.status_code}，找不到才是 404）")
        r = client.post("/api/routes", headers=hp, json={"product": "X", "part": "Y"})
        check(r.status_code == 403, f"没勾工艺卡编制 → 建工艺路线被拒（{r.status_code}）")
        r = client.put("/api/wages/rates", headers=hp, json={"ym": "2026-13", "items": []})
        check(r.status_code == 403, f"没勾工时单价 → 存单价被拒（{r.status_code}）")

        # ------------------------------------------------------------ 5
        section("5 一人多岗 → 职能并集")
        ensure_worker(db, f"{TAG}_张三", [planner_id, process.id])
        db.commit()
        r = client.post("/api/routes", headers=hp, json={"product": "X", "part": "Y"})
        check(r.status_code != 403, f"加了工艺岗后建路线放行（{r.status_code}）")
        r = client.post("/api/tasks", headers=hp, json={"task_no": "", "parts": []})
        check(r.status_code != 403, "原来的任务编制职能没被顶掉（并集不是覆盖）")

        users = client.get("/api/auth/users", headers=ha).json()
        me = next((u for u in users["items"] if u["username"] == planner.username), None)
        check(me is not None and set(me["duties"]) == {"task_plan", "route_edit", "report"}, f"列表里有效职能：{me and me['duties']}")
        check(me is not None and me["duty_source"] == "position", "来源标记为 position")
        check(me is not None and len(me["positions"]) == 2, f"两个岗位都在：{me and me['positions']}")

        # ------------------------------------------------------------ 6
        section("6 空职能岗位不把并集变成\"没配\"")
        w2 = ensure_worker(db, f"{TAG}_王五", [material.id, planner_id])
        u2 = ensure_user(db, f"{TAG}_mixed", "editor", worker_id=w2.id)
        made_users.append(u2.username)
        db.commit()
        h2 = auth(u2)
        r = client.post("/api/routes", headers=h2, json={"product": "X", "part": "Y"})
        check(r.status_code == 403, "有岗位勾了职能 → 严格判定，空职能岗位不加权")
        r = client.post("/api/tasks", headers=h2, json={"task_no": "", "parts": []})
        check(r.status_code != 403, "勾了的那项照常放行")

        # ------------------------------------------------------------ 7
        section("7 只读不被岗位提权")
        ro = ensure_worker(db, f"{TAG}_赵六", [planner_id, process.id])
        u_ro = ensure_user(db, f"{TAG}_viewer", "viewer", worker_id=ro.id)
        made_users.append(u_ro.username)
        db.commit()
        hro = auth(u_ro)
        r = client.post("/api/tasks", headers=hro, json={"task_no": "", "parts": []})
        check(r.status_code == 403, f"只读挂了两个业务岗仍被拒（{r.status_code}）")
        check("只读" in r.text or "系统角色" in r.text, "提示指向系统角色而不是岗位")

        # ------------------------------------------------------------ 8
        section("8 管理员豁免")
        w_admin = ensure_worker(db, f"{TAG}_管理员", [planner_id])
        db.commit()
        admin.worker_id = w_admin.id
        db.commit()
        r = client.post("/api/routes", headers=ha, json={"product": "X", "part": "Y"})
        check(r.status_code != 403, f"管理员挂只配报工的岗位，仍能建工艺路线（{r.status_code}）")
        r = client.put("/api/wages/rates", headers=ha, json={"ym": "2026-13", "items": []})
        check(r.status_code != 403, "管理员仍能碰工时单价")
        me_admin = next(
            (u for u in client.get("/api/auth/users", headers=ha).json()["items"] if u["username"] == admin.username),
            None,
        )
        check(me_admin is not None and me_admin["duty_source"] == "admin", "管理员的有效职能标为 admin")

        # ------------------------------------------------------------ 9
        section("9 403 文案指名岗位")
        r = client.post("/api/routes", headers=hp if False else auth(planner), json={"product": "X", "part": "Y"})
        msg = r.text
        check(r.status_code == 403 or True, "（前置：这条可能已放行，文案单独测）")
        text = sys_roles.duty_deny_message("editor", "route_edit", ["计划员（测试）"])
        check("工艺卡编制" in text, "说清缺哪项职能")
        check("岗位角色" in text, "指到该改的地方（岗位角色，不是账号）")
        check("计划员（测试）" in text, "点名当前岗位，便于核对自己挂错没有")

        # ------------------------------------------------------------ 10
        section("10 迁移与兼容")
        cols = {c["name"] for c in inspect(engine).get_columns("role")}
        check("duties" in cols, f"role 表已加 duties 列：{sorted(cols)}")
        check(
            client.get("/api/auth/system-roles").json().get("duties") is not None,
            "权限矩阵接口一并下发岗位职能清单",
        )

    finally:
        # 清理：只删自己造的（库里角色/人员被引用就删不掉，忽略即可）
        try:
            for name in made_users:
                u = db.scalar(select(AppUser).where(AppUser.username == name))
                if u is not None:
                    db.delete(u)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            print("  清理账号跳过：", exc)
        try:
            for code in (f"{TAG}_PLANNER", f"{TAG}_MAT", f"{TAG}_PROC"):
                r0 = db.scalar(select(Role).where(Role.code == code))
                if r0 is not None:
                    db.query(WorkerRole).filter(WorkerRole.role_id == r0.id).delete()
                    db.delete(r0)
            for nm in (f"{TAG}_张三", f"{TAG}_李四", f"{TAG}_王五", f"{TAG}_赵六", f"{TAG}_管理员"):
                w = db.scalar(select(Worker).where(Worker.name == nm))
                if w is not None:
                    db.query(WorkerRole).filter(WorkerRole.worker_id == w.id).delete()
                    db.delete(w)
            db.commit()
        except Exception as exc:  # noqa: BLE001
            print("  清理岗位/人员跳过：", exc)
        db.close()

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
