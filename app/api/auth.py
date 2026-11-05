"""登录与账号管理。

为什么要单独做这一层：审计表里记的「操作人」如果来自请求体，那它就不是证据。
现在写操作的审计身份由服务端从令牌解出，改密/停用后旧令牌立刻失效。

## 账号上的三个「角色」不是一回事

- `role` = **系统角色**（admin / editor / viewer），是权限**上限**；
- `worker_id` 指向名册人员，他的**岗位角色**（工作者/检验员/计划员）在 `worker_role` 里；
- 岗位角色上挂的 **`duties`**（任务编制/工艺卡编制/报工/工时单价）才是具体授权。

判定顺序写在 `require_perm` 里：管理员放行 → 职能类看岗位职能并集 → 其余看系统角色。
别拿岗位角色去推工时归属，也别拿系统角色去推职能边界 —— 两条路都试过，
最终都是在工资表上算错人。
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import AUTH_REQUIRED, DEFAULT_ADMIN, DEFAULT_ADMIN_PASSWORD, TRUST_LOCAL
from ..db import get_db
from ..domain import sys_roles
from ..models import AppUser, Worker, WorkerRole
from ..security import (
    bearer_of,
    current_operator,
    hash_password,
    is_loopback,
    make_token,
    operator_source,
    parse_token,
    verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


class PasswordRequest(BaseModel):
    old_password: str = ""
    new_password: str = Field(min_length=6, description="至少 6 位")


class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6)
    display_name: str = ""
    role: str = sys_roles.DEFAULT_SYSTEM_ROLE  # 系统角色：admin / editor / viewer
    worker_id: int | None = None  # 对应的名册人员（可空）


class UserUpdate(BaseModel):
    display_name: str | None = None
    role: str | None = None
    active: bool | None = None
    password: str | None = None
    worker_id: int | None = None
    clear_worker: bool = False  # 显式解绑：worker_id=None 分不清「没传」和「要清空」


def _worker_meta(db: Session, users: list[AppUser]) -> dict[int, dict]:
    """一次查出这些账号对应的人员与岗位角色。

    账号数量是个位数，但仍然批量查：`_user_dict` 里逐条查库迟早会有人在列表页
    加上几十个账号，那时候 N+1 就变成了"打开账号页要等两秒"。
    """
    ids = [u.worker_id for u in users if u.worker_id]
    if not ids:
        return {}
    rows = db.scalars(
        select(Worker)
        .where(Worker.id.in_(ids))
        .options(selectinload(Worker.roles).selectinload(WorkerRole.role))
    ).all()
    meta: dict[int, dict] = {}
    for w in rows:
        links = [link for link in w.roles if link.role]
        duties: set[str] = set()
        for link in links:
            duties.update(sys_roles.duties_of_role(link.role.duties))
        meta[w.id] = {
            "worker_name": w.name,
            "worker_no": w.employee_no or "",
            # 组织信息：车间（班组）与工种。顶栏要回答「这个人属于哪个组织」，
            # 只写姓名的话，多车间部署时分不清是谁在操作。
            "worker_team": w.team or "",
            "worker_process": w.default_process or "",
            "worker_active": bool(w.active),
            "positions": [link.role.name for link in links],
            "position_kinds": [link.role.kind for link in links],
            # 岗位授予的职能并集；空=岗位没配职能（账号按系统角色默认）
            "position_duties": [c for c in sys_roles.DUTY_CODES if c in duties],
        }
    return meta


def _user_dict(u: AppUser, meta: dict[int, dict] | None = None) -> dict:
    role_code = u.role or sys_roles.DEFAULT_SYSTEM_ROLE
    info = (meta or {}).get(u.worker_id or -1, {})
    granted = info.get("position_duties") or []
    if role_code == "admin":
        duties, source = list(sys_roles.DUTY_CODES), "admin"
    elif granted:
        duties, source = granted, "position"
    else:
        duties = sorted(sys_roles.default_duties(role_code))
        source = "default"
    return {
        "id": u.id,
        "username": u.username,
        "display_name": u.display_name or u.username,
        # 系统角色（权限上限）—— 字段仍叫 role，改动面太大；界面上一律写「系统角色」
        "role": role_code,
        "role_name": sys_roles.system_role_name(role_code),
        "role_known": sys_roles.is_system_role(role_code),
        "active": bool(u.active),
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "password_is_default": u.username == DEFAULT_ADMIN,
        # 对应人员 / 岗位角色
        "worker_id": u.worker_id,
        "worker_name": info.get("worker_name", ""),
        "worker_no": info.get("worker_no", ""),
        "worker_team": info.get("worker_team", ""),
        "worker_process": info.get("worker_process", ""),
        "worker_active": info.get("worker_active", None),
        "positions": info.get("positions", []),
        "position_kinds": info.get("position_kinds", []),
        # info 是「这个人有哪些字段」的字典，不是按 id 索引的。
        # 写成 `u.worker_id in info` 会去比字段名，答案永远是 False ——
        # 结果所有挂了人的账号都标着「名册里已找不到」，跟旁边的岗位角色自相矛盾。
        "worker_missing": bool(u.worker_id) and not info,
        # 有效职能：source = admin / position / default
        "duties": duties,
        "duty_names": [sys_roles.duty_label(c) for c in duties],
        "duty_source": source,
        "duty_configured": source != "default",
    }


def _check_worker(db: Session, worker_id: int | None) -> int | None:
    """校验账号要挂的人存在。停用的人可以挂（历史账号要看得出是谁），但会标出来。"""
    if worker_id in (None, 0, ""):
        return None
    w = db.get(Worker, int(worker_id))
    if w is None:
        raise HTTPException(400, f"名册里没有 id={worker_id} 的人员，请先在「人员」里新增")
    return w.id


def current_user_obj(db: Session) -> AppUser:
    """取当前登录账号。匿名本机模式下返回一个虚拟管理员。

    匿名也按 admin：单机部署里没人建第一个账号，不给这个兜底的话
    「账号」「工时单价」「备份」这些卡片永远打不开，看着像权限坏了。
    """
    name = current_operator("")
    if not name:
        if AUTH_REQUIRED and not TRUST_LOCAL:
            raise HTTPException(401, "请先登录")
        return AppUser(id=0, username="local", display_name="本机", role="admin", active=True)
    user = db.scalar(select(AppUser).where(AppUser.username == name))
    if user is None:
        # 本机免登录模式下，中间件放进上下文的身份是「本机操作员」这类虚拟名，
        # 库里当然查不到账号。只有「身份来自本机免登录」这一条路径才放行；
        # 带令牌来的查不到人仍然拒绝。
        if operator_source() == "local" and TRUST_LOCAL:
            return AppUser(id=0, username="local", display_name="本机", role="admin", active=True)
        raise HTTPException(401, "登录已失效，请重新登录")
    if not user.active:
        raise HTTPException(401, "登录已失效，请重新登录")
    return user


def position_duties(db: Session, user: AppUser) -> tuple[set[str] | None, list[str]]:
    """该账号**由岗位授予**的职能并集，以及岗位名清单。

    返回 `None` 表示「没配」而不是「什么都没有」，两者后果完全不同：
    没配 → 按系统角色的默认职能走（不锁人）；配了 → 严格按并集（授权）。
    没有这个区分，所有历史账号会因为岗位还没配职能而一夜之间什么都干不了。
    """
    if not user.worker_id:
        return None, []
    links = db.scalars(
        select(WorkerRole)
        .where(WorkerRole.worker_id == user.worker_id)
        .options(selectinload(WorkerRole.role))
    ).all()
    names: list[str] = []
    granted: set[str] = set()
    for link in links:
        if not link.role:
            continue
        names.append(link.role.name)
        granted.update(sys_roles.duties_of_role(link.role.duties))
    if not names:
        return None, []
    # 挂了岗位但一个职能都没勾 → 仍然是「没配」，不能当成「什么都不许干」
    return (granted or None), names


def effective_duties(db: Session, user: AppUser) -> dict:
    """账号最终有效的职能，以及它是怎么来的（给界面和 403 文案用）。"""
    if user.role == "admin":
        return {
            "duties": list(sys_roles.DUTY_CODES),
            "source": "admin",
            "configured": True,
            "positions": [],
        }
    granted, positions = position_duties(db, user)
    if granted is None:
        return {
            "duties": sorted(sys_roles.default_duties(user.role)),
            "source": "default",
            "configured": False,
            "positions": positions,
        }
    return {
        "duties": [c for c in sys_roles.DUTY_CODES if c in granted],
        "source": "position",
        "configured": True,
        "positions": positions,
    }


def require_perm(db: Session, perm: str) -> AppUser:
    """权限拦截。三层各管一段，顺序不能换（详见 `domain/sys_roles.py`）：

    1. **管理员直接放行** —— 运维身份不该因为没挂岗位就做不了事；
    2. **职能类**（任务编制/工艺卡编制/报工/工时单价）→ 看岗位职能并集，
       没配岗位职能时才回落到系统角色的默认值；
    3. **其余**（查看、基础数据、账号、备份）→ 只看系统角色。

    第 2 步排在系统角色之前是故意的：editor 本身没有「工时单价」这条硬权限，
    但可以让管理员给「核算员」岗位勾上，由岗位授予 —— 这就是「按岗位授权」。
    而 viewer 的默认职能集是空的，岗位勾得再多也进不到第 2 步。
    """
    user = current_user_obj(db)
    if user.role == "admin":
        return user

    if sys_roles.is_duty(perm):
        info = effective_duties(db, user)
        if perm not in info["duties"]:
            raise HTTPException(
                403, sys_roles.duty_deny_message(user.role, perm, info["positions"])
            )
        return user

    if not sys_roles.can(user.role, perm):
        raise HTTPException(403, sys_roles.deny_message(user.role, perm))
    return user


def require_admin(db: Session) -> AppUser:
    """账号管理只有管理员能做（保留这个名字，历史调用点很多）。"""
    return require_perm(db, sys_roles.PERM_ACCOUNT)


@router.get("/status")
def auth_status(request: Request, db: Session = Depends(get_db)):
    """未登录也能看：是否需要登录、本机是否免登录。

    前端启动时先问这里，才知道该跳登录页还是直接进业务页。
    """
    total = db.scalar(select(func.count()).select_from(AppUser)) or 0
    token = bearer_of(request.headers.get("authorization", ""))
    payload = parse_token(token) if token else None
    me = None
    if payload:
        u = db.scalar(select(AppUser).where(AppUser.username == payload.get("u")))
        if u and u.active:
            me = _user_dict(u, _worker_meta(db, [u]))
    return {
        "auth_required": AUTH_REQUIRED,
        "trust_local": TRUST_LOCAL,
        "user_count": int(total),
        "source": operator_source(),
        "user": me,
        "need_setup": total == 0,
    }


@router.get("/system-roles")
def system_roles():
    """系统角色定义与权限矩阵。

    单独一个接口而不是塞进 `/status`：权限矩阵是「说明性内容」，
    谁都能读（不登录也读），前端只渲染一次，不该跟着每次登录态变化。
    """
    return sys_roles.matrix()


@router.post("/login")
def login(data: LoginRequest, request: Request, db: Session = Depends(get_db)):
    name = (data.username or "").strip()
    user = db.scalar(select(AppUser).where(AppUser.username == name))
    if user is None or not user.active or not verify_password(data.password, user.password_hash):
        # 不区分「用户名不存在」和「密码错误」：区分了等于送人一份账号清单
        raise HTTPException(401, "用户名或密码不正确")
    user.last_login_at = dt.datetime.now()
    db.commit()
    meta = _worker_meta(db, [user])
    token = make_token(user.username, int(user.token_epoch or 0))
    return {
        **token,
        "user": _user_dict(user, meta),
        "from": request.client.host if request.client else "",
    }


@router.get("/me")
def me(db: Session = Depends(get_db)):
    name = current_operator("")
    if not name:
        if TRUST_LOCAL and not AUTH_REQUIRED:
            return {"user": None, "anonymous": True}
        raise HTTPException(401, "请先登录")
    user = db.scalar(select(AppUser).where(AppUser.username == name))
    if user is None:
        return {"user": None, "anonymous": True, "name": name}
    return {"user": _user_dict(user, _worker_meta(db, [user])), "anonymous": False}


@router.post("/password")
def change_password(data: PasswordRequest, db: Session = Depends(get_db)):
    name = current_operator("")
    if not name:
        raise HTTPException(401, "请先登录")
    user = db.scalar(select(AppUser).where(AppUser.username == name))
    if user is None:
        raise HTTPException(404, "账号不存在")
    if not verify_password(data.old_password, user.password_hash):
        raise HTTPException(400, "原密码不正确")
    user.password_hash = hash_password(data.new_password)
    # 改密后旧令牌全部作废，再签一枚新的给当前浏览器
    user.token_epoch = int(user.token_epoch or 0) + 1
    db.commit()
    return {**make_token(user.username, user.token_epoch), "ok": True}


# ------------------------------------------------------------------ 账号管理
@router.get("/users")
def list_users(db: Session = Depends(get_db)):
    require_admin(db)
    users = db.scalars(select(AppUser).order_by(AppUser.id)).all()
    meta = _worker_meta(db, users)
    return {
        "items": [_user_dict(u, meta) for u in users],
        "current": current_operator(""),
        "anonymous": not bool(current_operator("")),
    }


@router.post("/users")
def create_user(data: UserCreate, db: Session = Depends(get_db)):
    require_admin(db)
    exists = db.scalar(select(AppUser).where(AppUser.username == data.username.strip()))
    if exists:
        raise HTTPException(400, f"账号「{data.username}」已存在")
    if not sys_roles.is_system_role(data.role):
        raise HTTPException(
            400, "系统角色只能是 " + " / ".join(sys_roles.SYSTEM_ROLE_CODES)
        )
    user = AppUser(
        username=data.username.strip(),
        display_name=(data.display_name or data.username).strip(),
        password_hash=hash_password(data.password),
        role=data.role,
        worker_id=_check_worker(db, data.worker_id),
        active=True,
    )
    db.add(user)
    db.commit()
    return {"ok": True, "user": _user_dict(user, _worker_meta(db, [user]))}


@router.put("/users/{user_id}")
def update_user(user_id: int, data: UserUpdate, db: Session = Depends(get_db)):
    require_admin(db)
    user = db.get(AppUser, user_id)
    if user is None:
        raise HTTPException(404, "账号不存在")
    if data.display_name is not None:
        user.display_name = data.display_name.strip()
    if data.role is not None:
        if not sys_roles.is_system_role(data.role):
            raise HTTPException(
                400, "系统角色只能是 " + " / ".join(sys_roles.SYSTEM_ROLE_CODES)
            )
        # 别把自己降级后就再也进不来：最后一个管理员改成非管理员 = 系统失去管理员
        if user.role == "admin" and data.role != "admin":
            others = db.scalar(
                select(func.count())
                .select_from(AppUser)
                .where(AppUser.role == "admin", AppUser.active.is_(True), AppUser.id != user.id)
            ) or 0
            if others == 0:
                raise HTTPException(400, "这是唯一的管理员账号，改成其它系统角色后将没人能管账号")
        user.role = data.role
    if data.clear_worker:
        user.worker_id = None
    elif data.worker_id is not None:
        user.worker_id = _check_worker(db, data.worker_id)
    if data.active is not None:
        user.active = data.active
        # 停用即踢下线：不等令牌自然过期
        user.token_epoch = int(user.token_epoch or 0) + 1
    if data.password:
        if len(data.password) < 6:
            raise HTTPException(400, "密码至少 6 位")
        user.password_hash = hash_password(data.password)
        user.token_epoch = int(user.token_epoch or 0) + 1
    db.commit()
    return {"ok": True, "user": _user_dict(user, _worker_meta(db, [user]))}


def ensure_default_admin(db: Session) -> int:
    """库里一个账号都没有时建一个默认管理员。

    不建的话，第一次装完没人能登录；但默认口令必须显式提醒改，
    所以状态接口把 `password_is_default` 一起吐给前端。
    """
    if (db.scalar(select(func.count()).select_from(AppUser)) or 0) > 0:
        return 0
    db.add(
        AppUser(
            username=DEFAULT_ADMIN,
            display_name="管理员",
            password_hash=hash_password(DEFAULT_ADMIN_PASSWORD),
            role="admin",
            active=True,
        )
    )
    db.commit()
    return 1
