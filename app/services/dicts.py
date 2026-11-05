"""识别名册加载：把系统管理里维护的基础数据装成识别用的封闭集。

所有字段识别都靠这里的白名单兜底，因此：
- 只取 active 的记录，停用的字典不参与匹配；
- 人员按角色 kind 分册（工作者 / 检验员 / 接交人），一个人可同时出现在多册；
- 老库没有角色绑定时回退 is_inspector 标记，保证升级后立即可用。
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import (
    MaterialDict,
    PartDict,
    ProcessDict,
    ProductDict,
    Role,
    StandardHour,
    Worker,
    WorkerRole,
)
from . import routes as route_service

def _split_aliases(text: str) -> str:
    return text or ""


def dict_bundle(db: Session) -> dict[str, list]:
    """返回识别所需的全套名册。"""
    workers = db.scalars(select(Worker).where(Worker.active)).all()
    by_id = {w.id: w for w in workers}

    roles = db.scalars(select(Role)).all()
    kind_of = {r.id: r.kind for r in roles}
    worker_roles = db.scalars(select(WorkerRole)).all()

    by_kind: dict[str, list[tuple[int, str, str]]] = {
        "worker": [],
        "inspector": [],
        "receiver": [],
    }
    seen: dict[str, set[int]] = {k: set() for k in by_kind}
    for link in worker_roles:
        kind = kind_of.get(link.role_id, "")
        if kind not in by_kind:
            continue
        w = by_id.get(link.worker_id)
        if w and w.id not in seen[kind]:
            by_kind[kind].append((w.id, w.name, _split_aliases(w.aliases)))
            seen[kind].add(w.id)

    # 兜底：没有任何角色绑定的人（老数据、或用旧接口新建的）按 is_inspector 归类，
    # 保证「名册里有这个人」永远成立，不会因为漏配角色而在识别时变成未匹配。
    assigned = set().union(*seen.values()) if seen else set()
    for w in workers:
        if w.id in assigned:
            continue
        kind = "inspector" if w.is_inspector else "worker"
        by_kind[kind].append((w.id, w.name, _split_aliases(w.aliases)))

    processes = db.scalars(
        select(ProcessDict).where(ProcessDict.active).order_by(ProcessDict.sort_order)
    ).all()
    products = db.scalars(
        select(ProductDict).where(ProductDict.active).order_by(ProductDict.sort_order)
    ).all()
    materials = db.scalars(
        select(MaterialDict).where(MaterialDict.active).order_by(MaterialDict.sort_order)
    ).all()
    parts = db.scalars(
        select(PartDict).where(PartDict.active).order_by(PartDict.sort_order)
    ).all()
    std_hours = db.scalars(select(StandardHour).where(StandardHour.active)).all()
    # 标准工时按 (产品, 件名, 工序) 建索引：校验单件工时只用得上这一种查法。
    # 同时用工序 code 建一份：明细上可能写的是字典别名（「包板折弯」），
    # 标准工时表里记的是标准名（「折弯」），只按名字查会漏掉。
    code_of = {p.name: p.code for p in processes if p.code}
    std_index: dict[tuple[str, str, str], tuple[float, int]] = {}
    for h in std_hours:
        val = (h.unit_hours, h.samples)
        std_index[(h.product, h.part, h.process)] = val
        if code_of.get(h.process):
            std_index.setdefault((h.product, h.part, code_of[h.process]), val)

    return {
        "workers": by_kind["worker"],
        "inspectors": by_kind["inspector"],
        "receivers": by_kind["receiver"],
        "processes": [(p.code, p.name, _split_aliases(p.aliases)) for p in processes],
        "products": [
            (p.code or p.name, p.name, _split_aliases(p.aliases)) for p in products
        ],
        "materials": [
            (m.code or m.name, m.name, _split_aliases(m.aliases)) for m in materials
        ],
        "parts": [(p.name, p.name, _split_aliases(p.aliases)) for p in parts],
        # 标准工时按 (产品, 件名, 工序) 建索引：校验单件工时只用得上这一种查法
        "std_hours": std_index,
        # 已发布工艺路线索引：(产品, 件名) → 工序行。
        # 刻意放在 dict_bundle 里而不是校验时现查：校验一次要跑全部明细，
        # 现查就是 N 次 SQL；建一次索引全单共用。
        "routes": route_service.released_index(db),
    }


def rematch_item(item, bundle: dict) -> dict:
    """用最新名册重新归一一条已有明细。item 为 WorkItem ORM 对象。

    返回 {字段: 新值}，只包含发生变化的字段，便于调用方留痕。
    """
    from ..ocr.normalize import match_worker, resolve_process
    from ..ocr.normalize import match_material, match_product

    changes: dict[str, object] = {}

    if not item.worker_id and item.worker_raw:
        wid, name, score = match_worker(item.worker_raw, bundle["workers"])
        if wid:
            changes["worker_id"] = wid
            changes["worker_name"] = name
            changes["worker_score"] = score

    # 工序：字典里登记过的写法（含别名「包板折弯」）一律留原文，
    # 标准名与 code 单独存，供汇总和标准工时匹配
    proc_raw = getattr(item, "process_raw", "") or item.process
    if proc_raw:
        code, std, _score, disp, exact = resolve_process(proc_raw, bundle["processes"])
        if code and not item.process_code:
            changes["process_code"] = code
        if std and std != (getattr(item, "process_std", "") or ""):
            changes["process_std"] = std
        # 存量数据里显示值是旧的归并结果（折弯），原文才是「包板折弯」：按原文纠正
        if code and exact and disp and disp != item.process:
            changes["process"] = disp

    if item.inspector:
        _iid, name, _s = match_worker(item.inspector, bundle["inspectors"])
        if name and name != item.inspector:
            changes["inspector"] = name

    if getattr(item, "handover", "") and bundle["receivers"]:
        _hid, name, _s = match_worker(item.handover, bundle["receivers"])
        if name and name != item.handover:
            changes["handover"] = name

    if item.product_name:
        _c, name, score = match_product(item.product_name, bundle["products"])
        if name and score >= 0.55 and name != item.product_name:
            changes["product_name"] = name

    if getattr(item, "material", ""):
        _c, name, score = match_material(item.material, bundle["materials"])
        if name and score >= 0.55 and name != item.material:
            changes["material"] = name

    return changes
