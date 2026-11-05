"""系统管理：基础数据维护。

这里的每一份字典都直接作为识别兜底名册使用（见 services/dicts.py），
因此增删改后无需重启，下一张单据识别即生效；历史单据可用 /admin/rematch 回补。

管理项：
- products  产品名称/代号
- processes 工序
- materials 材料
- workers   人员（含角色分配）
- roles     角色
"""
from __future__ import annotations

import json
import re
import zlib

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from urllib.parse import quote
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..domain.rules import check_route, summarize_flags, validate_item
from ..domain import sys_roles
from ..domain.sys_roles import PERM_BACKUP, PERM_BASE_DATA
from .auth import require_admin, require_perm
from ..models import (
    MaterialDict,
    PartDict,
    Period,
    ProcessDict,
    ProductDict,
    Role,
    Sheet,
    StandardHour,
    Worker,
    WorkerRole,
    WorkItem,
)
from ..ocr.normalize import (
    match_entry,
    match_worker,
    resolve_entry,
    resolve_process,
)
from ..services.dicts import dict_bundle

router = APIRouter(prefix="/api/admin", tags=["admin"])

KINDS = ("products", "processes", "materials", "workers", "roles")


# ------------------------------------------------------------------ 请求模型
class ProductIn(BaseModel):
    name: str = Field(min_length=1)
    code: str = ""
    aliases: str = ""
    spec: str = ""
    active: bool = True
    sort_order: int = 999


class ProcessIn(BaseModel):
    code: str = ""
    name: str = Field(min_length=1)
    aliases: str = ""
    category: str = ""
    active: bool = True
    sort_order: int = 999
    default_seq: int = 999


class PartIn(BaseModel):
    name: str = Field(min_length=1)
    aliases: str = ""
    category: str = ""
    active: bool = True
    sort_order: int = 999


class MaterialIn(BaseModel):
    name: str = Field(min_length=1)
    code: str = ""
    aliases: str = ""
    spec: str = ""
    active: bool = True
    sort_order: int = 999


class WorkerIn(BaseModel):
    name: str = Field(min_length=1)
    employee_no: str = ""
    aliases: str = ""
    team: str = "2车间"
    default_process: str = ""
    active: bool = True
    role_ids: list[int] = Field(default_factory=list)


class RoleIn(BaseModel):
    code: str = ""
    name: str = Field(min_length=1)
    kind: str = "other"  # worker / inspector / receiver / other
    description: str = ""
    sort_order: int = 999
    # 岗位职能：能承担哪几件系统功能。接受数组或逗号串，非法值会被丢掉。
    duties: list[str] | str | None = None


class ImportIn(BaseModel):
    text: str = ""
    mode: str = "append"  # append / replace


# ------------------------------------------------------------------ 备份
@router.get("/backups")
def list_backups(limit: int = 10):
    """已有备份清单 + 逐个校验（全量口径）。

    备份「有没有」和「能不能恢复」是两件事：只看到文件名不算数，
    所以每条都打开跑 quick_check，并把**备份里的表清单**与当前库逐表对照
    —— 缺任意一张表就判不合格，而不是只看几张核心表在不在。

    `source` 是当前库的规模（表数 / 行数），前端拿它说明「备份覆盖了这些」。
    """
    from ..services import backup as backup_service

    return {
        "dir": str(backup_service.backup_dir()),
        "source": backup_service.source_info(),
        "items": backup_service.listing(limit=limit),
    }


@router.post("/backup")
def run_backup(keep: int = 14, db: Session = Depends(get_db)):
    """立即备份一份**全库**快照（系统管理页按钮）。

    与定时脚本走同一个函数：管理页面的「已备份」必须等于定时任务会做出来的东西。
    """
    require_perm(db, PERM_BACKUP)
    from ..services import backup as backup_service

    try:
        out = backup_service.create(keep=keep, label="manual")
    except Exception as exc:
        raise HTTPException(500, f"备份失败：{exc}") from exc
    if not out["ok"]:
        raise HTTPException(500, f"备份校验未通过：{_backup_problem(out)}")
    return out


def _backup_problem(out: dict) -> str:
    """把「校验没过」翻译成人能看懂的一句话，别只丢一个列表。"""
    if out.get("empty"):
        return "快照里一张表都没有（VACUUM INTO 没写成功）"
    missing = out.get("missing_tables") or []
    if missing:
        more = f" 等 {len(missing)} 张" if len(missing) > 4 else ""
        return "备份缺表：" + "、".join(missing[:4]) + more
    core = out.get("core_missing") or []
    if core:
        return "备份缺核心表：" + "、".join(core)
    bad = out.get("bad_tables") or []
    if bad:
        return "这些表读不出来：" + "、".join(bad)
    return f"quick_check={out.get('quick_check')}"


class RestoreIn(BaseModel):
    name: str = Field(min_length=1, description="备份文件名，如 worktime-20261105-124104-manual.db")
    # 队列里还有识别任务时的强制恢复。界面上不开放，留给排障/应急用。
    force: bool = False


@router.post("/restore")
def run_restore(body: RestoreIn, db: Session = Depends(get_db)):
    """把某份备份的数据恢复进当前库（系统管理页「恢复」按钮）。

    这是覆盖整个库的操作，所以顺序是死的：
    校验备份 → 拍恢复前快照 → 单事务整表替换 → 留痕 → 清连接池。
    中间任何一步失败都不碰当前数据；替换中途出错则整体回滚。
    """
    admin = require_perm(db, PERM_BACKUP)
    from ..services import backup as backup_service

    try:
        out = backup_service.restore(body.name, force=body.force)
    except backup_service.RestoreError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:  # 没预料到的异常也必须是 500 + 原话，不能静默
        raise HTTPException(500, f"恢复失败：{exc}") from exc

    # 留痕必须写在恢复**之后**：audit_log 也是被覆盖的表之一，
    # 写在前面会被备份里的旧审计冲掉，等于没记。
    #
    # 写之前先 rollback：require_admin 查过一次库，这个会话还停在恢复前的读快照上，
    # 拿它直接写会因为快照过期报 database is locked（WAL 的 SQLITE_BUSY_SNAPSHOT）。
    db.rollback()
    _audit_restore(db, admin.username or "system", out)

    # 恢复把数据整批换掉，本进程连接池里的会话可能还持有旧的快照读事务；
    # 不重建的话，紧接着的一次查询可能返回恢复前的数据，看着像「恢复没生效」。
    try:
        from ..db import engine

        engine.dispose()
    except Exception as exc:  # pragma: no cover - 重建失败不影响已提交的数据
        print(f"[restore] 连接池重建失败（需重启服务）：{exc}")

    return out


def _audit_restore(db: Session, user: str, out: dict) -> None:
    """恢复也要留痕，而且要留「怎么退回去」——否则下次没人敢再点一次。"""
    from ..models import AuditLog

    before, after = out.get("before", {}), out.get("after", {})
    parts = [
        out["name"],
        f"恢复到 {(out.get('restored_from') or {}).get('at') or (out.get('restored_from') or {}).get('mtime') or '—'}",
        f"回退快照 {out.get('pre_restore')}",
    ]
    if out.get("kept"):
        parts.append("保留（备份时还没有）：" + "、".join(out["kept"]))
    if out.get("created"):
        parts.append("按备份新建的表：" + "、".join(out["created"]))
    if out.get("defaulted_columns"):
        parts.append(
            "备份缺列（取默认值）："
            + "、".join(f"{t}.{','.join(c)}" for t, c in out["defaulted_columns"].items())
        )
    db.add(
        AuditLog(
            entity="backup",
            entity_id=0,
            sheet_id=None,
            field="backup:restore",
            old_value=f"恢复前 {before.get('tables')} 表/{before.get('rows')} 行",
            new_value="；".join(parts),
            user=user or "system",
        )
    )
    db.commit()


# ------------------------------------------------------------------ 概览
@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    """系统管理首页：每个管理项一张卡片所需的统计。"""
    role_rows = db.scalars(select(Role).order_by(Role.sort_order, Role.id)).all()
    role_counts = dict(
        db.execute(select(WorkerRole.role_id, func.count(WorkerRole.worker_id)).group_by(WorkerRole.role_id)).all()
    )

    products = db.scalars(select(ProductDict)).all()
    materials = db.scalars(select(MaterialDict)).all()
    processes = db.scalars(select(ProcessDict)).all()
    workers = db.scalars(select(Worker)).all()
    parts = db.scalars(select(PartDict)).all()
    std_hours = db.scalars(select(StandardHour)).all()

    # 被使用情况：一眼看出哪些字典还没真正用起来
    used_products = {
        r[0] for r in db.execute(select(WorkItem.product_name).distinct()).all() if r[0]
    }
    used_materials = {r[0] for r in db.execute(select(WorkItem.material).distinct()).all() if r[0]}
    used_processes = {r[0] for r in db.execute(select(WorkItem.process).distinct()).all() if r[0]}
    used_parts = {r[0] for r in db.execute(select(WorkItem.part_name).distinct()).all() if r[0]}

    def gap(names: list[str], used: set[str]) -> int:
        """字典里有、但历史明细没出现过的值之外，落在明细里却没入档的写法数量。

        这里统计「明细里出现过但字典没收录」的值 —— 正是需要补进字典的那些。
        """
        return len(used - set(names))

    role_kind = {r.id: r.kind for r in role_rows}
    seen_kind: dict[str, set[int]] = {}
    for link in db.scalars(select(WorkerRole)).all():
        k = role_kind.get(link.role_id)
        if k:
            seen_kind.setdefault(k, set()).add(link.worker_id)
    kinds_count = {k: len(v) for k, v in seen_kind.items()}

    return {
        "cards": [
            {
                "key": "products",
                "title": "产品名称",
                "subtitle": "产品名称 / 代号",
                "count": len(products),
                "active": sum(1 for x in products if x.active),
                "gap": gap([p.name for p in products], used_products),
                "extra": "",
                "tip": "表头「产品名称 / 产品代号」按此收敛，识别时优先匹配代号与别名",
            },
            {
                "key": "processes",
                "title": "工序",
                "subtitle": "工序字典 / 工种归类",
                "count": len(processes),
                "active": sum(1 for x in processes if x.active),
                "gap": gap([p.name for p in processes], used_processes),
                "extra": "",
                "tip": "明细「工序」列按此收敛；别名可覆盖手写变体（如 铣1 / 铣床 / 铣工）",
            },
            {
                "key": "materials",
                "title": "材料",
                "subtitle": "材料字典",
                "count": len(materials),
                "active": sum(1 for x in materials if x.active),
                "gap": gap([m.name for m in materials], used_materials),
                "extra": "",
                "tip": "表头「材料」栏按此收敛，别名可覆盖 Q235 / 45# 等手写写法",
            },
            {
                "key": "parts",
                "title": "件名",
                "subtitle": "零部件名称",
                "count": len(parts),
                "active": sum(1 for x in parts if x.active),
                "gap": gap([p.name for p in parts], used_parts),
                "extra": f"标准工时 {len(std_hours)} 条",
                "tip": "明细「件名」栏按此收敛。件名是零件、工序是干法，两者组合才定得出单件工时",
            },
            {
                "key": "workers",
                "title": "人员",
                "subtitle": "工作者 / 检验员 / 接交人",
                "count": len(workers),
                "active": sum(1 for x in workers if x.active),
                "gap": 0,
                "extra": (
                    f"工作者 {kinds_count.get('worker', 0)} · "
                    f"检验员 {kinds_count.get('inspector', 0)} · "
                    f"接交人 {kinds_count.get('receiver', 0)}"
                ),
                "tip": "手写姓名走封闭集匹配；同姓唯一时残字也能兜住（如「杰」→「杜文杰」）",
            },
            {
                "key": "roles",
                "title": "岗位角色",
                "subtitle": "车间身份 · 岗位职能",
                "count": len(role_rows),
                "active": len(role_rows),
                "gap": 0,
                "extra": (
                    f"内置 {sum(1 for r in role_rows if r.builtin)} 个 · "
                    f"已配职能 {sum(1 for r in role_rows if sys_roles.duties_of_role(r.duties))} 个"
                ),
                "tip": "岗位角色定「这个人是谁、能承担哪几件系统功能」；登录账号的系统角色定「能走到哪一步」",
            },
        ],
        "roles": [
            {
                "id": r.id,
                "code": r.code,
                "name": r.name,
                "kind": r.kind,
                "description": r.description,
                "builtin": r.builtin,
                "sort_order": r.sort_order,
                "worker_count": int(role_counts.get(r.id, 0)),
                "duties": list(sys_roles.duties_of_role(r.duties)),
                "duty_names": [
                    sys_roles.duty_label(c) for c in sys_roles.duties_of_role(r.duties)
                ],
            }
            for r in role_rows
        ],
        # 勾选项清单由后端给：界面不另写一份，免得加了一项功能忘了加到前端
        "duty_options": [
            {"code": c, "label": lb, "scope": sys_roles.DUTY_SCOPES.get(c, "")}
            for c, lb in sys_roles.DUTY_LABELS
        ],
        "kinds": list(KINDS),
    }


# ------------------------------------------------------------------ 产品
def _product_out(p: ProductDict) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "code": p.code,
        "aliases": p.aliases,
        "spec": p.spec,
        "active": p.active,
        "sort_order": p.sort_order,
    }


@router.get("/products")
def list_products(keyword: str = "", db: Session = Depends(get_db)):
    stmt = select(ProductDict).order_by(ProductDict.sort_order, ProductDict.id)
    rows = db.scalars(stmt).all()
    if keyword:
        k = keyword.strip()
        rows = [r for r in rows if k in r.name or k in r.code or k in r.aliases or k in r.spec]
    return {"items": [_product_out(p) for p in rows], "total": len(rows)}


@router.post("/products")
def create_product(data: ProductIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    if db.scalar(select(ProductDict).where(ProductDict.name == data.name)):
        raise HTTPException(400, f"产品「{data.name}」已存在")
    p = ProductDict(**data.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return _product_out(p)


@router.put("/products/{pid}")
def update_product(pid: int, data: ProductIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    p = db.get(ProductDict, pid)
    if not p:
        raise HTTPException(404, "产品不存在")
    dup = db.scalar(select(ProductDict).where(ProductDict.name == data.name, ProductDict.id != pid))
    if dup:
        raise HTTPException(400, f"产品「{data.name}」已存在")
    for k, v in data.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return _product_out(p)


@router.delete("/products/{pid}")
def delete_product(pid: int, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    p = db.get(ProductDict, pid)
    if not p:
        raise HTTPException(404, "产品不存在")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 工序
def _process_out(p: ProcessDict) -> dict:
    return {
        "id": p.id,
        "code": p.code,
        "name": p.name,
        "aliases": p.aliases,
        "category": p.category,
        "active": p.active,
        "sort_order": p.sort_order,
        "default_seq": p.default_seq,
    }


@router.get("/processes")
def list_processes(keyword: str = "", db: Session = Depends(get_db)):
    rows = db.scalars(select(ProcessDict).order_by(ProcessDict.sort_order, ProcessDict.id)).all()
    if keyword:
        k = keyword.strip()
        rows = [r for r in rows if k in r.name or k in r.code or k in r.aliases or k in r.category]
    return {"items": [_process_out(p) for p in rows], "total": len(rows)}


@router.post("/processes")
def create_process(data: ProcessIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    code = (data.code or "").strip() or _auto_code(data.name)
    if db.scalar(select(ProcessDict).where(ProcessDict.code == code)):
        raise HTTPException(400, f"工序编码「{code}」已存在")
    p = ProcessDict(**{**data.model_dump(), "code": code})
    db.add(p)
    db.commit()
    db.refresh(p)
    return _process_out(p)


@router.put("/processes/{pid}")
def update_process(pid: int, data: ProcessIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    p = db.get(ProcessDict, pid)
    if not p:
        raise HTTPException(404, "工序不存在")
    code = (data.code or "").strip() or p.code
    dup = db.scalar(select(ProcessDict).where(ProcessDict.code == code, ProcessDict.id != pid))
    if dup:
        raise HTTPException(400, f"工序编码「{code}」已存在")
    for k, v in data.model_dump().items():
        setattr(p, k, v)
    p.code = code
    db.commit()
    return _process_out(p)


@router.delete("/processes/{pid}")
def delete_process(pid: int, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    p = db.get(ProcessDict, pid)
    if not p:
        raise HTTPException(404, "工序不存在")
    db.delete(p)
    db.commit()
    return {"ok": True}


def _auto_code(name: str) -> str:
    """无编码时按名称生成稳定编码（同一名称每次结果一致，便于重复导入幂等）。"""
    base = re.sub(r"\W+", "", name) or name
    return f"P{zlib.crc32(base.encode('utf-8')) % 1000000:06d}"


# ------------------------------------------------------------------ 材料
def _material_out(m: MaterialDict) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "code": m.code,
        "aliases": m.aliases,
        "spec": m.spec,
        "active": m.active,
        "sort_order": m.sort_order,
    }


@router.get("/materials")
def list_materials(keyword: str = "", db: Session = Depends(get_db)):
    rows = db.scalars(select(MaterialDict).order_by(MaterialDict.sort_order, MaterialDict.id)).all()
    if keyword:
        k = keyword.strip()
        rows = [r for r in rows if k in r.name or k in r.code or k in r.aliases or k in r.spec]
    return {"items": [_material_out(m) for m in rows], "total": len(rows)}


@router.post("/materials")
def create_material(data: MaterialIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    if db.scalar(select(MaterialDict).where(MaterialDict.name == data.name)):
        raise HTTPException(400, f"材料「{data.name}」已存在")
    m = MaterialDict(**data.model_dump())
    db.add(m)
    db.commit()
    db.refresh(m)
    return _material_out(m)


@router.put("/materials/{mid}")
def update_material(mid: int, data: MaterialIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    m = db.get(MaterialDict, mid)
    if not m:
        raise HTTPException(404, "材料不存在")
    dup = db.scalar(select(MaterialDict).where(MaterialDict.name == data.name, MaterialDict.id != mid))
    if dup:
        raise HTTPException(400, f"材料「{data.name}」已存在")
    for k, v in data.model_dump().items():
        setattr(m, k, v)
    db.commit()
    return _material_out(m)


@router.delete("/materials/{mid}")
def delete_material(mid: int, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    m = db.get(MaterialDict, mid)
    if not m:
        raise HTTPException(404, "材料不存在")
    db.delete(m)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 件名（零部件）
def _part_out(p: PartDict) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "aliases": p.aliases,
        "category": p.category,
        "active": p.active,
        "sort_order": p.sort_order,
    }


@router.get("/parts")
def list_parts(keyword: str = "", db: Session = Depends(get_db)):
    rows = db.scalars(select(PartDict).order_by(PartDict.sort_order, PartDict.id)).all()
    if keyword:
        k = keyword.strip()
        rows = [r for r in rows if k in r.name or k in r.aliases or k in r.category]
    return {"items": [_part_out(p) for p in rows], "total": len(rows)}


@router.post("/parts")
def create_part(data: PartIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    if db.scalar(select(PartDict).where(PartDict.name == data.name)):
        raise HTTPException(400, f"件名「{data.name}」已存在")
    p = PartDict(**data.model_dump())
    db.add(p)
    db.commit()
    db.refresh(p)
    return _part_out(p)


@router.put("/parts/{pid}")
def update_part(pid: int, data: PartIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    p = db.get(PartDict, pid)
    if not p:
        raise HTTPException(404, "件名不存在")
    dup = db.scalar(select(PartDict).where(PartDict.name == data.name, PartDict.id != pid))
    if dup:
        raise HTTPException(400, f"件名「{data.name}」已存在")
    for k, v in data.model_dump().items():
        setattr(p, k, v)
    db.commit()
    return _part_out(p)


@router.delete("/parts/{pid}")
def delete_part(pid: int, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    p = db.get(PartDict, pid)
    if not p:
        raise HTTPException(404, "件名不存在")
    db.delete(p)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 标准工时
@router.get("/std-hours")
def list_std_hours(
    keyword: str = "", limit: int = 200, db: Session = Depends(get_db)
):
    """历史单件工时标准（只读）。用于核对时查「这道工序该是多少小时」。"""
    stmt = select(StandardHour).where(StandardHour.active).order_by(
        StandardHour.samples.desc(), StandardHour.id
    )
    rows = db.scalars(stmt).all()
    if keyword:
        k = keyword.strip()
        rows = [r for r in rows if k in r.product or k in r.part or k in r.process]
    total = len(rows)
    return {
        "items": [
            {
                "id": r.id,
                "product": r.product,
                "part": r.part,
                "process": r.process,
                "unit_hours": r.unit_hours,
                "samples": r.samples,
                "agreement": r.agreement,
            }
            for r in rows[: max(1, min(limit, 1000))]
        ],
        "total": total,
    }


# ------------------------------------------------------------------ 人员
def _worker_out(w: Worker) -> dict:
    return {
        "id": w.id,
        "name": w.name,
        "employee_no": w.employee_no,
        "aliases": w.aliases,
        "team": w.team,
        "default_process": w.default_process,
        "active": w.active,
        "is_inspector": w.is_inspector,
        "role_ids": [link.role_id for link in w.roles],
        "role_names": [link.role.name for link in w.roles if link.role],
    }


def _sync_roles(db: Session, w: Worker, role_ids: list[int]) -> None:
    """重设人员角色，并同步 is_inspector 派生字段（旧逻辑依赖它）。"""
    db.execute(delete(WorkerRole).where(WorkerRole.worker_id == w.id))
    db.flush()
    kinds: list[str] = []
    for rid in dict.fromkeys(role_ids):
        role = db.get(Role, rid)
        if not role:
            continue
        db.add(WorkerRole(worker_id=w.id, role_id=rid))
        kinds.append(role.kind)
    w.is_inspector = "inspector" in kinds
    db.flush()


@router.get("/workers")
def list_workers(keyword: str = "", role_id: int | None = None, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Worker).options(selectinload(Worker.roles).selectinload(WorkerRole.role)).order_by(Worker.id)
    ).all()
    if keyword:
        k = keyword.strip()
        rows = [r for r in rows if k in r.name or k in r.aliases or k in r.team or k in r.employee_no]
    if role_id:
        rows = [r for r in rows if any(link.role_id == role_id for link in r.roles)]
    return {"items": [_worker_out(w) for w in rows], "total": len(rows)}


@router.post("/workers")
def create_worker(data: WorkerIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    if db.scalar(select(Worker).where(Worker.name == data.name)):
        raise HTTPException(400, f"人员「{data.name}」已存在")
    payload = data.model_dump()
    role_ids = payload.pop("role_ids", [])
    w = Worker(**payload)
    db.add(w)
    db.flush()
    _sync_roles(db, w, role_ids)
    db.commit()
    db.refresh(w)
    return _worker_out(w)


@router.put("/workers/{wid}")
def update_worker(wid: int, data: WorkerIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    w = db.scalar(
        select(Worker).where(Worker.id == wid).options(selectinload(Worker.roles).selectinload(WorkerRole.role))
    )
    if not w:
        raise HTTPException(404, "人员不存在")
    dup = db.scalar(select(Worker).where(Worker.name == data.name, Worker.id != wid))
    if dup:
        raise HTTPException(400, f"人员「{data.name}」已存在")
    payload = data.model_dump()
    role_ids = payload.pop("role_ids", [])
    for k, v in payload.items():
        setattr(w, k, v)
    _sync_roles(db, w, role_ids)
    db.commit()
    db.refresh(w)
    return _worker_out(w)


@router.delete("/workers/{wid}")
def delete_worker(wid: int, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    w = db.get(Worker, wid)
    if not w:
        raise HTTPException(404, "人员不存在")
    used = db.scalar(select(func.count(WorkItem.id)).where(WorkItem.worker_id == wid)) or 0
    if used:
        raise HTTPException(
            400, f"该人员已被 {used} 条工时明细引用，不能删除；如需停用请把「启用」关掉"
        )
    db.execute(delete(WorkerRole).where(WorkerRole.worker_id == wid))
    db.delete(w)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 角色
@router.get("/roles")
def list_roles(db: Session = Depends(get_db)):
    rows = db.scalars(select(Role).order_by(Role.sort_order, Role.id)).all()
    counts = dict(
        db.execute(select(WorkerRole.role_id, func.count(WorkerRole.worker_id)).group_by(WorkerRole.role_id)).all()
    )
    return {
        "items": [
            {
                "id": r.id,
                "code": r.code,
                "name": r.name,
                "kind": r.kind,
                "description": r.description,
                "builtin": r.builtin,
                "sort_order": r.sort_order,
                "worker_count": int(counts.get(r.id, 0)),
                # 岗位职能：codes 给前端比对，names 直接显示
                "duties": list(sys_roles.duties_of_role(r.duties)),
                "duty_names": [sys_roles.duty_label(c) for c in sys_roles.duties_of_role(r.duties)],
            }
            for r in rows
        ],
        "total": len(rows),
        "duty_options": [
            {"code": c, "label": lb, "scope": sys_roles.DUTY_SCOPES.get(c, "")}
            for c, lb in sys_roles.DUTY_LABELS
        ],
    }


@router.post("/roles")
def create_role(data: RoleIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    code = (data.code or "").strip() or _auto_code(data.name)
    if db.scalar(select(Role).where(Role.code == code)):
        raise HTTPException(400, f"角色编码「{code}」已存在")
    if data.kind not in ("worker", "inspector", "receiver", "other"):
        raise HTTPException(400, "角色归类只能是 worker / inspector / receiver / other")
    payload = data.model_dump()
    payload.pop("duties", None)
    duties = sys_roles.duties_of_role(data.duties)
    r = Role(**{**payload, "code": code}, duties=",".join(duties))
    db.add(r)
    db.commit()
    db.refresh(r)
    return {"id": r.id, "duties": list(duties)}


@router.put("/roles/{rid}")
def update_role(rid: int, data: RoleIn, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    r = db.get(Role, rid)
    if not r:
        raise HTTPException(404, "角色不存在")
    if r.builtin and data.kind != r.kind:
        raise HTTPException(400, "内置角色的识别归类不可修改，否则历史单据匹配会漂移")
    dup = db.scalar(select(Role).where(Role.code == (data.code or r.code), Role.id != rid))
    if dup:
        raise HTTPException(400, "角色编码已存在")
    r.name = data.name
    r.code = (data.code or r.code).strip()
    r.kind = data.kind
    r.description = data.description
    r.sort_order = data.sort_order
    # duties 不传 = 不改（老前端/脚本调用不应把职能清空）
    if data.duties is not None:
        r.duties = ",".join(sys_roles.duties_of_role(data.duties))
    db.commit()
    return {"id": r.id, "duties": list(sys_roles.duties_of_role(r.duties))}


@router.delete("/roles/{rid}")
def delete_role(rid: int, db: Session = Depends(get_db)):
    require_perm(db, PERM_BASE_DATA)
    r = db.get(Role, rid)
    if not r:
        raise HTTPException(404, "角色不存在")
    if r.builtin:
        raise HTTPException(400, "内置角色不可删除")
    db.execute(delete(WorkerRole).where(WorkerRole.role_id == rid))
    db.delete(r)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ 批量导入
# 一套列定义同时服务三条路径：手动新增（单条表单）、粘贴文本、Excel/CSV 文件。
# keys 决定列顺序，header_alias 用于「文件里列顺序和模板不一样」时按表头重排。
_IMPORT_SPEC: dict[str, dict] = {
    "products": {
        "keys": ["name", "code", "aliases", "spec"],
        "header_alias": {
            "code": ("编码", "代号", "图号", "产品编码", "物料编码"),
            "name": ("名称", "品名", "产品", "产品名称"),
            "aliases": ("别名", "别称", "俗称", "其他写法", "识别别名"),
            "spec": ("规格", "规格型号", "型号"),
        },
        "title": "产品字典",
        "columns": ["名称", "代号", "别名", "规格"],
        "sample": (
            "YUJ-18BZ,YUJ-18BZ,鱼机/YUJ18BZ/鱼式制丸机,鱼机 18BZ\n"
            "DDG-30,DDG-30,DDG30/单螺杆,30型"
        ),
    },
    "processes": {
        "keys": ["code", "name", "category", "aliases"],
        "header_alias": {
            "code": ("编码", "代号", "工序编码", "工序号"),
            "name": ("名称", "工序", "工序名称"),
            "category": ("工种", "归类", "分类", "所属工种"),
            "aliases": ("别名", "别称", "俗称", "其他写法", "识别别名"),
        },
        "title": "工序字典",
        "columns": ["编码", "名称", "工种", "别名"],
        "sample": (
            "TANG,镗,镗工,镗/镗工/镗床\n"
            "XI,铣,铣工,铣/铣工/铣床/铣1/铣2"
        ),
    },
    "parts": {
        "keys": ["name", "aliases", "category"],
        "header_alias": {
            "name": ("名称", "件名", "零件", "零件名称", "零部件"),
            "aliases": ("别名", "别称", "俗称", "其他写法", "识别别名"),
            "category": ("归类", "分类", "所属设备", "产品", "设备"),
        },
        "title": "件名字典",
        "columns": ["件名", "别名", "归类"],
        "sample": (
            "机架,机架体/主机架,\n"
            "刀轮,刀具轮/切刀轮,制丸机"
        ),
    },
    "materials": {
        "keys": ["name", "code", "aliases", "spec"],
        "header_alias": {
            "code": ("编码", "代号", "物料编码"),
            "name": ("名称", "材料", "材料名称", "材质"),
            "aliases": ("别名", "别称", "俗称", "其他写法", "识别别名"),
            "spec": ("规格", "规格型号", "牌号", "厚度"),
        },
        "title": "材料字典",
        "columns": ["名称", "编码", "别名", "规格"],
        "sample": (
            "Q235,Q235,Q235B/Q235-A/普通碳钢,δ12\n"
            "45#,45,45钢/45号钢,δ20"
        ),
    },
    "workers": {
        "keys": ["name", "employee_no", "team", "default_process", "aliases", "roles"],
        "header_alias": {
            "employee_no": ("工号", "员工号", "编号"),
            "name": ("姓名", "名字", "人员", "工作者"),
            "team": ("班组", "车间", "部门"),
            "default_process": ("工种", "岗位", "默认工序"),
            "aliases": ("别名", "别称", "俗称", "其他写法", "识别别名"),
            "roles": ("角色", "身份"),
        },
        "title": "人员名册",
        "columns": ["姓名", "工号", "班组", "工种", "别名", "角色"],
        "sample": (
            "马永刚,013,2车间,电工,,工作者\n"
            "杜文杰,101,质检,检验,,检验员"
        ),
    },
}

def _cell_text(value) -> str:
    """Excel 单元格转字符串；12345.0 这类浮点去掉小数尾巴。"""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _split_text_line(line: str) -> list[str]:
    """一行文本拆列：Tab / 英文逗号 / 竖线 / 中文逗号 / 连续空格。"""
    return [c.strip() for c in re.split(r"[\t,|，]+|\s{2,}", line)]


# 表头识别：不能只看有没有关键词 —— 数据行里的「镗/镗床/新别名」也会含「别名」二字。
# 真正的表头是「一排列名」，因此要求命中 ≥2 个不同的列，或整行只有一个纯列名。
def _norm_aliases(text: str) -> str:
    """别名统一成英文逗号分隔：手写常用 / 、; 空格 混排，入库前必须收敛。"""
    parts = [p.strip() for p in re.split(r"[/、|;,，\s]+", text or "") if p.strip()]
    return ",".join(dict.fromkeys(parts))


def _is_header(cells: list[str], kind: str) -> bool:
    if len(_header_index(cells, kind)) >= 2:
        return True
    labels = {name for names in _IMPORT_SPEC[kind]["header_alias"].values() for name in names}
    non_empty = [c.strip() for c in cells if c.strip()]
    return len(non_empty) == 1 and non_empty[0] in labels


def _header_index(cells: list[str], kind: str) -> dict[int, int]:
    """表头列名 → keys 下标；返回 {keys 下标: 原列下标}。

    只认「短单元格」：数据行里的「工作者/接交人」「2车间」这类长文本不是列名，
    否则纯数据行会被误判成表头整行丢掉。
    """
    spec = _IMPORT_SPEC[kind]
    found: dict[int, int] = {}
    for pos, raw in enumerate(cells):
        original = str(raw or "").strip()
        if not original:
            continue
        text = re.sub(r"[\s/、()（）:：]+", "", original)
        if not text:
            continue
        for ki, key in enumerate(spec["keys"]):
            if ki in found:
                continue
            names = spec["header_alias"][key]
            if not any(alias in text for alias in names):
                continue
            if len(original) > 6 and text not in names:
                continue
            found[ki] = pos
            break
    return found


def _prepare_rows(kind: str, raw_rows: list[list[str]]) -> tuple[list[list[str]], str]:
    """统一成 spec.keys 顺序；首行是表头时按列名重排，否则按位置对应。"""
    spec = _IMPORT_SPEC[kind]
    width = len(spec["keys"])
    rows = [[_cell_text(c) for c in row] for row in raw_rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return [], ""

    note = "按列位置对应"
    if _is_header(rows[0], kind):
        idx = _header_index(rows[0], kind)
        rows = rows[1:]
        if idx:
            rows = [
                [row[idx[i]] if i in idx and idx[i] < len(row) else "" for i in range(width)]
                for row in rows
            ]
            note = "按表头列名对应"
        else:
            note = "表头未识别，按列位置对应"
    return rows, note


def _find_dict_row(db: Session, model, name: str, code: str):
    """按名称查，查不到再按编码查。

    表里同时有「产品名称」和「产品代号」两列时，名称列可能填的是中文名，
    而字典主键是单据上写的代号（如 YUJ-18BZ）。只按名称查会重复建一条，
    因此用编码兜底命中已有记录。
    """
    obj = db.scalar(select(model).where(model.name == name))
    if obj or not code:
        return obj
    return db.scalar(select(model).where((model.code == code) | (model.name == code)))


def _import_rows(kind: str, raw_rows: list[list[str]], mode: str, db: Session) -> dict:
    if kind not in _IMPORT_SPEC:
        raise HTTPException(400, "不支持的管理项")
    keys = _IMPORT_SPEC[kind]["keys"]
    rows, note = _prepare_rows(kind, raw_rows)
    if not rows:
        raise HTTPException(400, "没有可导入的内容")
    mode = "replace" if mode == "replace" else "append"

    role_by_name = {r.name: r.id for r in db.scalars(select(Role)).all()}
    role_by_code = {r.code: r.id for r in db.scalars(select(Role)).all()}

    # 工序新增时接在末尾。默认 sort_order=0 会与「镗」并列，把新条目插到列表最前面
    next_order = 0
    if kind == "processes":
        next_order = db.scalar(select(func.max(ProcessDict.sort_order))) or 0

    created = updated = skipped = 0
    errors: list[str] = []

    for no, cells in enumerate(rows, start=1):
        v = {key: (cells[i] if i < len(cells) else "") for i, key in enumerate(keys)}
        try:
            if kind == "products":
                name = v["name"] or v["code"]
                if not name:
                    errors.append(f"第 {no} 行：名称为空，已跳过")
                    skipped += 1
                    continue
                obj = _find_dict_row(db, ProductDict, name, v["code"])
                if obj:
                    if mode == "replace":
                        obj.name = name
                        obj.code = v["code"] or obj.code
                        obj.aliases = _norm_aliases(v["aliases"]) or obj.aliases
                        obj.spec = v["spec"] or obj.spec
                        updated += 1
                    else:
                        skipped += 1
                else:
                    db.add(
                        ProductDict(
                            name=name,
                            code=v["code"] or name,
                            aliases=_norm_aliases(v["aliases"]),
                            spec=v["spec"],
                        )
                    )
                    created += 1

            elif kind == "parts":
                name = v["name"]
                if not name:
                    errors.append(f"第 {no} 行：件名为空，已跳过")
                    skipped += 1
                    continue
                obj = db.scalar(select(PartDict).where(PartDict.name == name))
                if obj:
                    if mode == "replace":
                        obj.aliases = _norm_aliases(v["aliases"]) or obj.aliases
                        obj.category = v["category"] or obj.category
                        updated += 1
                    else:
                        skipped += 1
                else:
                    order = (db.scalar(select(func.max(PartDict.sort_order))) or 0) + 1
                    db.add(PartDict(
                        name=name,
                        aliases=_norm_aliases(v["aliases"]),
                        category=v["category"],
                        sort_order=order,
                    ))
                    created += 1

            elif kind == "processes":
                name = v["name"] or v["code"]
                code = v["code"] or (_auto_code(name) if name else "")
                if not name or not code:
                    errors.append(f"第 {no} 行：工序名称与编码都为空，已跳过")
                    skipped += 1
                    continue
                obj = db.scalar(select(ProcessDict).where(ProcessDict.code == code))
                if obj:
                    if mode == "replace":
                        obj.name = name
                        obj.category = v["category"] or obj.category
                        obj.aliases = _norm_aliases(v["aliases"]) or obj.aliases
                        updated += 1
                    else:
                        skipped += 1
                else:
                    next_order += 1
                    db.add(
                        ProcessDict(
                            code=code,
                            name=name,
                            category=v["category"],
                            aliases=_norm_aliases(v["aliases"]),
                            sort_order=next_order,
                        )
                    )
                    created += 1

            elif kind == "materials":
                name = v["name"] or v["code"]
                if not name:
                    errors.append(f"第 {no} 行：材料名称为空，已跳过")
                    skipped += 1
                    continue
                obj = _find_dict_row(db, MaterialDict, name, v["code"])
                if obj:
                    if mode == "replace":
                        obj.name = name
                        obj.code = v["code"] or obj.code
                        obj.aliases = _norm_aliases(v["aliases"]) or obj.aliases
                        obj.spec = v["spec"] or obj.spec
                        updated += 1
                    else:
                        skipped += 1
                else:
                    db.add(
                        MaterialDict(
                            name=name,
                            code=v["code"] or name,
                            aliases=_norm_aliases(v["aliases"]),
                            spec=v["spec"],
                        )
                    )
                    created += 1

            else:  # workers
                name = v["name"]
                if not name:
                    errors.append(f"第 {no} 行：姓名为空，已跳过")
                    skipped += 1
                    continue
                obj = db.scalar(select(Worker).where(Worker.name == name))
                role_ids = [
                    role_by_name.get(rn) or role_by_code.get(rn)
                    for rn in re.split(r"[/、,，]+", v["roles"])
                    if rn.strip()
                ]
                role_ids = [rid for rid in role_ids if rid]
                if obj:
                    if mode == "replace":
                        obj.employee_no = v["employee_no"] or obj.employee_no
                        obj.team = v["team"] or obj.team
                        obj.default_process = v["default_process"] or obj.default_process
                        obj.aliases = _norm_aliases(v["aliases"]) or obj.aliases
                        if role_ids:
                            _sync_roles(db, obj, role_ids)
                        updated += 1
                    else:
                        skipped += 1
                else:
                    w = Worker(
                        name=name,
                        employee_no=v["employee_no"],
                        team=v["team"] or "2车间",
                        default_process=v["default_process"],
                        aliases=_norm_aliases(v["aliases"]),
                    )
                    db.add(w)
                    db.flush()
                    _sync_roles(db, w, role_ids or [role_by_code.get("WORKER", 1)])
                    created += 1
        except Exception as exc:  # 单条失败不影响其他行
            errors.append(f"第 {no} 行：{exc}")
        if no % 200 == 0:
            db.flush()

    db.commit()
    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors[:20],
        "total_lines": len(rows),
        "mapped_by": note,
    }


def _read_xlsx(raw: bytes) -> list[list[str]]:
    import io

    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise HTTPException(400, "Excel 文件无法解析，请确认是 .xlsx 格式且未加密")
    try:
        ws = wb[wb.sheetnames[0]]
        return [[_cell_text(c) for c in row] for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def _read_text_bytes(raw: bytes) -> list[list[str]]:
    import csv
    import io

    text = ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text.strip():
        raise HTTPException(400, "文件内容为空或编码无法识别，请另存为 UTF-8")

    first = text.split("\n", 1)[0]
    if "\t" in first:
        sep, use_csv = "\t", True
    elif "," in first:
        sep, use_csv = ",", True
    else:
        sep, use_csv = "", False
    if use_csv:
        return [[_cell_text(c) for c in row] for row in csv.reader(io.StringIO(text), delimiter=sep)]
    return [_split_text_line(ln) for ln in text.splitlines() if ln.strip()]


@router.post("/{kind}/import")
def bulk_import(kind: str, data: ImportIn, db: Session = Depends(get_db)):
    """粘贴文本批量导入。

    分隔符支持 Tab / 英文逗号 / 中文逗号 / 竖线 / 连续空格；首行若像表头会自动跳过。
    """
    require_perm(db, PERM_BASE_DATA)
    lines = [ln for ln in (data.text or "").splitlines() if ln.strip()]
    if not lines:
        raise HTTPException(400, "没有可导入的内容")
    return _import_rows(kind, [_split_text_line(ln) for ln in lines], data.mode, db)


@router.post("/{kind}/import-file")
async def import_file(
    kind: str,
    file: UploadFile = File(...),
    mode: str = Form("append"),
    db: Session = Depends(get_db),
):
    """从 .xlsx / .csv / .txt 文件导入。

    列顺序不必和模板一致：首行有表头时按列名自动对应；没有表头则按位置对应。
    """
    require_perm(db, PERM_BASE_DATA)
    if kind not in _IMPORT_SPEC:
        raise HTTPException(400, "不支持的管理项")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "文件为空")

    filename = (file.filename or "").lower()
    if filename.endswith((".xlsx", ".xlsm")):
        rows = _read_xlsx(raw)
    elif filename.endswith(".xls"):
        raise HTTPException(400, "不支持旧版 .xls，请在 Excel 中另存为 .xlsx 或 .csv")
    elif filename.endswith((".csv", ".txt")):
        rows = _read_text_bytes(raw)
    else:
        raise HTTPException(400, "只支持 .xlsx / .csv / .txt 文件")
    return _import_rows(kind, rows, mode, db)


@router.get("/{kind}/template")
def download_template(kind: str):
    """下载导入模板（CSV，带 BOM，Excel 打开中文不乱码）。"""
    spec = _IMPORT_SPEC.get(kind)
    if not spec:
        raise HTTPException(400, "不支持的管理项")
    content = "\ufeff" + ",".join(spec["columns"]) + "\n" + spec["sample"] + "\n"
    filename = f"{spec['title']}导入模板.csv"
    return Response(
        content=content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _at(cols: list[str], idx: int) -> str:
    return cols[idx] if idx < len(cols) else ""


# ------------------------------------------------------------------ 回补历史单据
@router.post("/rematch")
def rematch(ym: str | None = None, only_unverified: bool = True, db: Session = Depends(get_db)) -> dict:
    """用最新字典重新匹配已有明细（不重跑 OCR）。

    典型场景：识别时某产品/材料/人员不在字典里，补齐字典后点一下即可回填，
    不必重新拍照识别。
    """
    bundle = dict_bundle(db)
    stmt = select(Sheet).options(selectinload(Sheet.items))
    if ym:
        period = db.scalar(select(Period).where(Period.ym == ym))
        if not period:
            raise HTTPException(404, f"核算期 {ym} 不存在")
        stmt = stmt.where(Sheet.period_id == period.id)
    if only_unverified:
        stmt = stmt.where(Sheet.status != "verified")

    sheets = db.scalars(stmt).all()
    touched_items = 0
    touched_sheets = 0

    for sheet in sheets:
        period = db.get(Period, sheet.period_id)
        period_ym = period.ym if period else ""
        changed = False
        for it in sheet.items:
            before = (
                it.worker_id,
                it.process_code,
                it.inspector,
                it.handover,
                it.product_name,
                it.material,
            )
            _rematch_item(it, bundle)
            after = (
                it.worker_id,
                it.process_code,
                it.inspector,
                it.handover,
                it.product_name,
                it.material,
            )
            if before != after:
                changed = True
                touched_items += 1
            _revalidate_item(it, period_ym, bundle)
        if changed:
            touched_sheets += 1
        sheet.total_hours = round(sum(i.total_hours for i in sheet.items), 4)
        sheet.flag_count = summarize_flags(
            [{"flags": json.loads(i.flags_json or "[]")} for i in sheet.items]
        )["total"]

    db.commit()
    return {"sheets": touched_sheets, "items": touched_items, "scanned": len(sheets)}


def _rematch_item(it: WorkItem, bundle: dict) -> None:
    if not it.worker_id and it.worker_raw:
        wid, _name, _score = match_worker(it.worker_raw, bundle["workers"])
        if wid:
            it.worker_id = wid
    # 工序：字典登记过的写法（含别名「包板折弯」）保留原文，标准名与 code 单独存
    proc_raw = getattr(it, "process_raw", "") or it.process
    if proc_raw:
        code, std, _score, disp, exact = resolve_process(proc_raw, bundle["processes"])
        if code and not it.process_code:
            it.process_code = code
        if std:
            it.process_std = std
        if code and exact and disp:
            it.process = disp
    if it.inspector:
        _iid, name, _s = match_worker(it.inspector, bundle["inspectors"])
        if name:
            it.inspector = name
    if it.handover:
        _hid, name, _s = match_worker(it.handover, bundle["receivers"])
        if name:
            it.handover = name
    # 精确命中保留原写法，只在错字时纠偏（与识别流水线同一口径）
    if it.product_name:
        _c, name, score, exact = resolve_entry(it.product_name, bundle["products"])
        if not exact and name and score >= 0.55:
            it.product_name = name
    if it.material:
        _c, name, score, exact = resolve_entry(it.material, bundle["materials"])
        if not exact and name and score >= 0.55:
            it.material = name


def _revalidate_item(it: WorkItem, period_ym: str, bundle: dict) -> None:
    from ..domain.rules import compute_total_hours

    def in_dict(raw: str, entries: list) -> bool:
        if not raw or not entries:
            return True
        _c, name, score = match_entry(raw, entries)
        return bool(name) and score >= 0.55

    payload = {
        "unit_hours": it.unit_hours,
        "qualified_qty": it.qualified_qty,
        "rework_qty": it.rework_qty,
        "scrap_qty": it.scrap_qty,
        "declared_total": it.declared_total,
        "finish_date": it.finish_date.isoformat() if it.finish_date else None,
        "finish_date_raw": getattr(it, "finish_date_raw", "") or "",
        "worker_id": it.worker_id,
        "process": it.process,
        "process_std": getattr(it, "process_std", "") or "",
        "process_code": it.process_code,
        "product_name": it.product_name,
        "part_name": it.part_name,
        "material": it.material,
        "inspector": it.inspector,
        "handover": it.handover,
        "product_matched": in_dict(it.product_name, bundle["products"]),
        "material_matched": in_dict(it.material, bundle["materials"]),
        "inspector_matched": in_dict(it.inspector, bundle["inspectors"]),
        "handover_matched": in_dict(it.handover, bundle["receivers"]),
        "conf": json.loads(it.conf_json or "{}"),
    }
    flags = validate_item(payload, period_ym)
    # 路线校验只在有已发布路线时才有内容，没建路线的零件静默跳过
    flags.extend(check_route(payload, bundle.get("routes") or {}))
    it.flags_json = json.dumps(flags, ensure_ascii=False)
    it.total_hours = compute_total_hours(
        it.qualified_qty, it.unit_hours, it.rework_qty, it.scrap_qty
    )

    require_perm(db, PERM_BASE_DATA)