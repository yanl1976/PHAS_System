"""系统角色（权限）与岗位角色（车间身份）的分离回归。

用法：python scripts/test_sys_roles.py
（脚本名以 test_ 开头，config 会自动切到数据库副本，生产库只读）

八组：
  [1] 权限矩阵口径：三类系统角色、权限点、未知角色降级只读
  [2] 两套角色互不串：改岗位角色不动系统角色，反之亦然
  [3] 账号可挂名册人员：返回姓名与岗位角色；可解绑；挂不存在的人被拒
  [4] 唯一管理员保护：不能把最后一个管理员改成别的系统角色
  [5] 只读账号的写请求由服务端拦（403），读与改自己密码放行
  [6] 录入核对：业务与基础数据可写，账号/备份/单价不可
  [7] 管理员：四类特权接口都进得去
  [8] 数据库迁移：app_user 有 worker_id 列
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import func, inspect, select  # noqa: E402

from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.domain import sys_roles  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AppUser, Role, Worker, WorkerRole  # noqa: E402
from app.security import hash_password, make_token  # noqa: E402

TAG = "ZZROLE"
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


def ensure_user(db, username: str, role: str, worker_id: int | None = None, active: bool = True) -> AppUser:
    u = db.scalar(select(AppUser).where(AppUser.username == username))
    if u is None:
        u = AppUser(
            username=username,
            display_name=username,
            password_hash=hash_password("pass123456"),
            role=role,
            worker_id=worker_id,
            active=active,
        )
        db.add(u)
    u.role = role
    u.worker_id = worker_id
    u.active = active
    db.flush()
    return u


def auth(user: AppUser) -> dict:
    tok = make_token(user.username, int(user.token_epoch or 0))
    return {"Authorization": f"Bearer {tok['token']}"}


def main() -> int:
    init_db()
    db = SessionLocal()
    client = TestClient(app)
    made_users: list[str] = []
    made_roles: list[int] = []
    made_workers: list[int] = []
    made_products: list[int] = []
    try:
        # ------------------------------------------------------------ 1
        section("1 权限矩阵口径")
        check(
            [r["code"] for r in sys_roles.SYSTEM_ROLES] == ["admin", "editor", "viewer"],
            "系统角色就是三类：admin / editor / viewer",
        )
        check(
            [c for c, _ in sys_roles.PERM_LABELS]
            == ["read", "business", "base_data", "wage_rate", "account", "backup"],
            "六个权限点固定",
        )
        check(sys_roles.can("admin", "backup") and sys_roles.can("admin", "account"), "管理员有备份与账号权限")
        check(
            not sys_roles.can("editor", "backup")
            and not sys_roles.can("editor", "account")
            and not sys_roles.can("editor", "wage_rate"),
            "录入核对没有备份/账号/单价权限",
        )
        check(sys_roles.can("editor", "business") and sys_roles.can("editor", "base_data"), "录入核对能写业务与基础数据")
        check(sys_roles.system_role("viewer")["perms"] == ("read",), "只读只有 read 一个权限")
        check(not sys_roles.can("viewer", "business"), "只读不能写")
        # 未知/损坏的角色名不能把人挡在门外，也不能悄悄给权限
        check(sys_roles.system_role("nonsense")["code"] == "viewer", "未知角色降级为只读，不报错")
        check(not sys_roles.is_system_role("nonsense"), "未知角色不被认作合法系统角色")
        check("只读" in sys_roles.deny_message("viewer", "business"), "403 的话说清了当前角色")
        check("系统管理" in sys_roles.deny_message("viewer", "business"), "403 的话告诉了去哪儿改")
        matrix = sys_roles.matrix()
        # 工时单价已经从「系统角色能力」移到「岗位职能」，所以系统角色矩阵是 5 行；
        # 两张表分开列，读者才不会去账号里找那个其实在岗位上的开关。
        check(
            len(matrix["perms"]) == 5 and len(matrix["roles"]) == 3,
            f"系统角色矩阵 5 权限 × 3 角色：{[p['code'] for p in matrix['perms']]}",
        )
        check(
            [d["code"] for d in matrix["duties"]] == list(sys_roles.DUTY_CODES),
            "同一接口另给岗位职能清单（4 项）",
        )
        check(
            "wage_rate" in [d["code"] for d in matrix["duties"]],
            "工时单价出现在岗位职能里，而不是系统角色矩阵里",
        )

        # ------------------------------------------------------------ 2
        section("2 两套角色互不串")
        admin = ensure_user(db, f"{TAG}_admin", "admin")
        made_users.append(admin.username)
        # 造一个岗位角色 + 一个人，挂到账号上
        role = Role(code=f"{TAG}_OP", name="操作员", kind="worker", description="自测用", builtin=False)
        db.add(role)
        db.flush()
        made_roles.append(role.id)
        worker = Worker(name=f"{TAG}操作员", employee_no="T001", team="自测班组")
        db.add(worker)
        db.flush()
        made_workers.append(worker.id)
        db.add(WorkerRole(worker_id=worker.id, role_id=role.id))
        db.flush()

        editor = ensure_user(db, f"{TAG}_editor", "editor", worker_id=worker.id)
        made_users.append(editor.username)
        db.commit()

        # 改岗位角色 → 账号的系统角色不能跟着动
        role.name = "操作员（改名）"
        db.commit()
        db.refresh(editor)
        check(editor.role == "editor", "改岗位角色名称，账号系统角色不变（仍是 editor）")

        # 改系统角色 → 人的岗位角色不能跟着动
        editor.role = "viewer"
        db.commit()
        check(
            bool(db.scalar(select(WorkerRole).where(WorkerRole.worker_id == worker.id))),
            "改系统角色，人的岗位角色关联还在",
        )
        check(role.name == "操作员（改名）", "岗位角色本身没被系统角色改动")
        editor.role = "editor"
        db.commit()

        # ------------------------------------------------------------ 3
        section("3 账号可挂名册人员")
        r = client.get("/api/auth/users", headers=auth(admin))
        check(r.status_code == 200, f"管理员能读账号列表（{r.status_code}）")
        items = {u["username"]: u for u in r.json()["items"]}
        me_item = items.get(editor.username, {})
        check(me_item.get("worker_id") == worker.id, "列表里带 worker_id")
        check(me_item.get("worker_name") == worker.name, f"带出人员姓名：{me_item.get('worker_name')}")
        check(me_item.get("positions") == ["操作员（改名）"], f"带出岗位角色名：{me_item.get('positions')}")
        check(me_item.get("role") == "editor" and me_item.get("role_name") == "录入核对", "系统角色与中文名分开给")
        check(me_item.get("position_kinds") == ["worker"], "岗位角色的归类也带出来（worker）")
        # 挂着一个真实存在的人时不能标「名册里已找不到」——以前这里把
        # 「id 在字段字典里」当成了判断，结果每个挂了人的账号都自相矛盾。
        check(me_item.get("worker_missing") is False, "挂了真实人员时不标「名册里已找不到」")

        # 新建账号时直接挂人
        newname = f"{TAG}_withworker"
        r = client.post(
            "/api/auth/users",
            headers=auth(admin),
            json={"username": newname, "password": "pass123456", "role": "viewer", "worker_id": worker.id},
        )
        check(r.status_code == 200, f"创建带人员关联的账号（{r.status_code}）")
        if r.status_code == 200:
            made_users.append(newname)
            check(r.json()["user"]["worker_name"] == worker.name, "创建结果直接回人员姓名")
            check(r.json()["user"]["role_name"] == "只读", "创建结果回系统角色中文名")

        # 挂一个不存在的人：必须拒绝，不能默默存个野 id
        r = client.post(
            "/api/auth/users",
            headers=auth(admin),
            json={"username": f"{TAG}_bad", "password": "pass123456", "role": "editor", "worker_id": 999999},
        )
        check(r.status_code == 400, f"挂不存在的人员被拒（{r.status_code}）")
        check("名册" in (r.json().get("detail") or ""), "拒绝时告诉人去「人员」里新增")

        # 解绑：clear_worker
        r = client.put(
            f"/api/auth/users/{editor.id}",
            headers=auth(admin),
            json={"clear_worker": True},
        )
        check(r.status_code == 200 and r.json()["user"]["worker_id"] is None, "能解绑（clear_worker）")
        check(r.json()["user"]["worker_name"] == "", "解绑后不再带人员姓名")
        # 再挂回去，后面只读测试要用
        client.put(f"/api/auth/users/{editor.id}", headers=auth(admin), json={"worker_id": worker.id, "role": "editor"})
        db.refresh(editor)
        check(editor.worker_id == worker.id and editor.role == "editor", "重新挂人并保持系统角色")

        # ------------------------------------------------------------ 4
        section("4 唯一管理员保护")
        # 把其它管理员先降级，确保只剩当前这一个
        others = db.scalars(select(AppUser).where(AppUser.role == "admin", AppUser.id != admin.id)).all()
        backup_roles = {u.id: u.role for u in others}
        for u in others:
            u.role = "editor"
        db.commit()
        r = client.put(f"/api/auth/users/{admin.id}", headers=auth(admin), json={"role": "editor"})
        check(r.status_code == 400, f"把唯一管理员降级被拒（{r.status_code}）")
        check("唯一的管理员" in (r.json().get("detail") or ""), "拒绝理由说清是唯一管理员")
        # 恢复原样
        for u in others:
            u.role = backup_roles.get(u.id, "editor")
        db.commit()

        # ------------------------------------------------------------ 5
        section("5 只读账号：写被服务端拦，读与改密放行")
        viewer = ensure_user(db, f"{TAG}_viewer", "viewer")
        made_users.append(viewer.username)
        db.commit()

        r = client.get("/api/auth/me", headers=auth(viewer))
        check(r.status_code == 200, "只读能读自己的信息")
        check(r.json()["user"]["role_name"] == "只读", "接口如实说自己是只读")

        r = client.get("/api/admin/products", headers=auth(viewer))
        check(r.status_code == 200, f"只读能看基础数据（{r.status_code}）")

        r = client.post("/api/admin/products", headers=auth(viewer), json={"name": f"{TAG}产品"})
        check(r.status_code == 403, f"只读写基础数据被拦（{r.status_code}）")
        detail = r.json().get("detail") or {}
        check(isinstance(detail, dict) and detail.get("code") == "READ_ONLY", "错误码是 READ_ONLY")
        check("只读" in (detail.get("message") or ""), "提示里说明了当前系统角色")

        # 业务写接口用「删一条不存在的单据」验证：中间件在进 handler 之前就该拦下，
        # 所以既不会真的删到东西，也证明拦截发生在业务逻辑之外。
        r = client.delete("/api/sheets/999999", headers=auth(viewer))
        check(r.status_code == 403, f"只读写业务接口同样被拦（{r.status_code}）")

        r = client.post(
            "/api/auth/password",
            headers=auth(viewer),
            json={"old_password": "pass123456", "new_password": "newpass123456"},
        )
        check(r.status_code == 200, f"只读能改自己的密码（{r.status_code}）")
        # 改密后旧令牌失效，换新令牌继续
        db.refresh(viewer)
        r = client.get("/api/auth/me", headers=auth(viewer))
        check(r.status_code == 200, "新令牌可用")

        # ------------------------------------------------------------ 6
        section("6 录入核对：业务与基础数据可写，特权接口不可")
        editor.role = "editor"
        db.commit()
        name = f"{TAG}产品A"
        r = client.post("/api/admin/products", headers=auth(editor), json={"name": name})
        check(r.status_code == 200, f"录入核对能新增产品（{r.status_code}）")
        if r.status_code == 200:
            made_products.append(r.json()["id"])

        r = client.post("/api/admin/backup?keep=14", headers=auth(editor))
        check(r.status_code == 403, f"录入核对不能备份（{r.status_code}）")
        check("备份" in ((r.json().get("detail") or {}) if isinstance(r.json().get("detail"), dict) else str(r.json().get("detail"))), "拒绝时点明缺的是备份权限")

        r = client.post("/api/admin/restore", headers=auth(editor), json={"name": "x.db"})
        check(r.status_code == 403, f"录入核对不能恢复（{r.status_code}）")

        r = client.put("/api/wages/rates", headers=auth(editor), json={"ym": "2035-01", "items": []})
        check(r.status_code == 403, f"录入核对不能改工时单价（{r.status_code}）")

        r = client.get("/api/auth/users", headers=auth(editor))
        check(r.status_code == 403, f"录入核对不能读账号列表（{r.status_code}）")

        # ------------------------------------------------------------ 7
        section("7 管理员：特权接口都进得去")
        r = client.get("/api/auth/users", headers=auth(admin))
        check(r.status_code == 200, "管理员能读账号列表")
        # 备份要写真文件，这里只验证"没被权限挡"：换成一个必然失败的参数也行，
        # 但更干净的是看 /api/admin/backups（读）与 system-roles（读）都 200
        r = client.get("/api/admin/backups", headers=auth(admin))
        check(r.status_code == 200, "管理员能读备份清单")
        r = client.get("/api/auth/system-roles")
        check(r.status_code == 200, "权限矩阵接口不登录也能读（说明性内容，无需鉴权）")
        check(len(r.json()["roles"]) == 3, "矩阵接口返回三个角色")

        # ------------------------------------------------------------ 8
        section("8 数据库迁移")
        cols = {c["name"] for c in inspect(engine).get_columns("app_user")}
        check("worker_id" in cols, "app_user 有 worker_id 列")
        check("role" in cols, "app_user 仍有 role 列（系统角色）")
        tables = set(inspect(engine).get_table_names())
        check("role" in tables and "worker_role" in tables, "岗位角色仍存 role / worker_role 表")
        check("app_user" in tables, "账号仍在 app_user 表")

        # 两套角色在库里确实是分开的：没有把 role_id 塞进 app_user
        check("role_id" not in cols, "app_user 上没有 role_id —— 系统角色不是岗位角色")
    finally:
        # 清理：只删自己造的，副本本身进程结束会删
        try:
            from app.models import ProductDict

            for pid in made_products:
                p = db.get(ProductDict, pid)
                if p is not None:
                    db.delete(p)
            for wid in made_workers:
                for link in db.scalars(select(WorkerRole).where(WorkerRole.worker_id == wid)):
                    db.delete(link)
                w = db.get(Worker, wid)
                if w is not None:
                    db.delete(w)
            for rid in made_roles:
                rl = db.get(Role, rid)
                if rl is not None:
                    db.delete(rl)
            for name in made_users:
                u = db.scalar(select(AppUser).where(AppUser.username == name))
                if u is not None:
                    db.delete(u)
            db.commit()
        except Exception as exc:  # pragma: no cover - 清理失败不该盖住测试结果
            print(f"  [清理] 部分测试数据未删除：{exc}")
        db.close()

    print(f"\n{'=' * 46}\n通过 {PASS} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  -", f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
