"""工艺路线的读取、生成、发布与覆盖率统计。

这套逻辑独立于识别流水线（app/ocr/*）：识别负责把单据读成明细行，
本模块负责把「明细 + 历史工时」组织成**有顺序、有定额、有版本**的工艺路线。

几个刻意的设计取舍：

1. **允许片段**。路线只登记已知工序，`completeness='partial'`。
   校验环节因此绝不报「缺工序」——按现有数据（801 个组合里 601 个只有
   1 道工序），报缺工序等于天天误报。

2. **顺序三档推断**，越靠前越可信：
   a. 该「产品×件名」在 work_item 里有真实 seq → 直接用真单据顺序（最可信）；
   b. 否则按 process_dict.default_seq（工艺默认序）；
   c. 同段内按历史样本数降序。

3. **不覆盖实测数据**。standard_hour（历史实测）与路线定额是两回事：
   实测是机器统计的结果，定额是有人确认过的标准。两者并列显示偏差，
   由人决定是否采纳，绝不互相回写。

4. **准备工时默认留空**。setup_hours=0 或 batch_qty=0 时按纯单件工时计算，
   与现行核算口径完全一致；填了才分摊。不允许出现除零。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..models import (
    ProcessDict,
    ProcessRoute,
    ProcessRouteStep,
    RouteVersionNotice,
    Sheet,
    StandardHour,
    TaskRoute,
    WorkCombo,
    WorkItem,
    WorkTask,
)

SEQ_STEP = 10  # 工序号步长：留空位便于中间插行


# ------------------------------------------------------------------ 基础索引
def process_index(db: Session) -> dict[str, dict]:
    """工序名 / 编码 / 别名 → 工序信息。

    标准工时表里记的可能是标准名（「折弯」），明细上写的可能是别名
    （「包板折弯」），两边都要能查到同一道工序。
    """
    idx: dict[str, dict] = {}
    for p in db.scalars(select(ProcessDict)).all():
        info = {
            "code": p.code,
            "name": p.name,
            "category": p.category or "",
            "default_seq": p.default_seq if p.default_seq is not None else 999,
        }
        idx[p.name] = info
        if p.code:
            idx.setdefault(p.code, info)
        for alias in (p.aliases or "").split(","):
            alias = alias.strip()
            if alias:
                idx.setdefault(alias, info)
    return idx


def _key_of(product: str, part: str) -> tuple[str, str]:
    return ((product or "").strip(), (part or "").strip())


# ------------------------------------------------------------------ 生成候选
def suggest_steps(db: Session, product: str, part: str) -> list[dict]:
    """按「产品×件名」生成候选工序行（不落库，供预览与草稿使用）。

    返回的每行都带 `order_source`（顺序依据）与 `samples`（历史样本数），
    让人一眼看出这行是「真单据里就是这么排的」还是「按工艺序推的」。
    """
    product, part = _key_of(product, part)
    pidx = process_index(db)
    # 产品主数据归一：调用方可能传历史写法（SJ2栓剂），库里存的是规范名
    # （SJ-2栓剂）。两个都当候选查，否则旧写法一律生成空路线。
    from .tasks import resolve_product

    names = [product]
    _prod = resolve_product(db, product)
    if _prod and _prod.name != product:
        names.append(_prod.name)

    # ---- 1) 历史工时：这道工序的单件工时是多少
    hours: dict[str, dict] = {}
    for h in db.scalars(
        select(StandardHour).where(
            StandardHour.active,
            StandardHour.product.in_(names),
            StandardHour.part == part,
        )
    ).all():
        info = pidx.get(h.process) or pidx.get((h.process or "").strip())
        key = info["code"] if info else (h.process or "").strip()
        if not key:
            continue
        prev = hours.get(key)
        # 同一道工序有多条记录时取样本多的那条
        if prev is None or (h.samples or 0) > (prev.get("samples") or 0):
            hours[key] = {
                "code": key,
                "name": info["name"] if info else h.process,
                "category": info["category"] if info else "",
                "default_seq": info["default_seq"] if info else 999,
                "unit_hours": float(h.unit_hours or 0.0),
                "samples": int(h.samples or 0),
                "agreement": float(h.agreement or 0.0),
                "from": "history",
            }

    # ---- 2) 真单据顺序：同一零件在报工明细里出现过的工序序列
    # 扫码报工没有 sheet（sheet_id 为空），但工序顺序是同一件事，
    # 必须一起算进来，否则「扫过码的件」在工艺编排里永远没有历史顺序。
    actual: list[str] = []
    rows = db.execute(
        select(WorkItem, Sheet)
        .outerjoin(Sheet, WorkItem.sheet_id == Sheet.id)
        .where(WorkItem.product_name.in_(names), WorkItem.part_name == part)
        .order_by(func.coalesce(Sheet.id, 0), WorkItem.seq)
    ).all()
    for item, _sheet in rows:
        raw = item.process_code or item.process_std or item.process
        info = pidx.get(raw) or pidx.get(item.process_raw or "") or pidx.get(item.process or "")
        code = info["code"] if info else (raw or "").strip()
        if not code:
            continue
        if code not in actual:
            actual.append(code)
        if item.unit_hours and code not in hours:
            hours[code] = {
                "code": code,
                "name": info["name"] if info else (item.process or item.process_std or ""),
                "category": info.get("category", "") if info else "",
                "default_seq": info.get("default_seq", 999) if info else 999,
                "unit_hours": float(item.unit_hours),
                "samples": 1,
                "agreement": 0.0,
                "from": "sheet",
            }

    # ---- 3) 排序：真单据顺序优先，其余按工艺默认序追加在后
    order_hint = {code: i for i, code in enumerate(actual)}
    steps = list(hours.values())
    for s in steps:
        s["order_source"] = "sheet" if s["code"] in order_hint else "process_seq"

    steps.sort(
        key=lambda s: (
            0 if s["code"] in order_hint else 1,
            order_hint.get(s["code"], 0),
            s["default_seq"],
            -s["samples"],
            s["name"],
        )
    )

    out: list[dict] = []
    # 非字典工序要标出来：历史工时表里有 32 行（2.5%）的「工序」其实是
    # 件名或作业描述（「撒粉喷浆组件」「加固防护板」…），它们不是工序。
    # 直接塞进路线会让人以为这是道工序，必须让人工判断后再定。
    # 判断依据是这个名称能不能在工序字典（含别名）里查到，
    # 不能用「code == 名称」来猜——字典里真有 code 与名称相同的项。
    dict_names = set(pidx)
    for i, s in enumerate(steps, start=1):
        not_in_dict = (s["name"] or "").strip() not in dict_names
        remark = ""
        if not_in_dict:
            remark = "疑似非工序：历史统计里这个名称不在工序字典内，请确认是否该是件名"
        out.append(
            {
                "seq": i * SEQ_STEP,
                "process_code": s["code"],
                "process_name": s["name"],
                "op_type": "self",
                "category": s["category"],
                "work_center": "",
                "setup_hours": 0.0,
                "unit_hours": s["unit_hours"],
                "hours_src": s["from"],
                "samples": s["samples"],
                "is_key": False,
                "inspect_required": False,
                "sop": "",
                "remark": remark,
                "order_source": s["order_source"],
                "not_in_dict": not_in_dict,
            }
        )
    return out


# ------------------------------------------------------------------ 路线读写
def route_out(route: ProcessRoute, steps: list[ProcessRouteStep] | None = None) -> dict:
    steps = steps if steps is not None else list(route.steps)
    unit_sum = sum(s.unit_hours or 0.0 for s in steps)
    setup_sum = sum(s.setup_hours or 0.0 for s in steps)
    batch = route.batch_qty or 0.0
    per_unit = unit_sum + (setup_sum / batch if batch > 0 else 0.0)
    return {
        "id": route.id,
        "kind": route.kind,
        "product": route.product,
        "part": route.part,
        "title": route.title,
        "version": route.version,
        "status": route.status,
        "completeness": route.completeness,
        "batch_qty": route.batch_qty,
        "allowance_rate": route.allowance_rate,
        "source": route.source,
        "effective_from": route.effective_from.isoformat() if route.effective_from else None,
        "effective_to": route.effective_to.isoformat() if route.effective_to else None,
        "remark": route.remark,
        "created_by": route.created_by,
        "created_at": route.created_at.isoformat() if route.created_at else None,
        "updated_at": route.updated_at.isoformat() if route.updated_at else None,
        "published_at": route.published_at.isoformat() if route.published_at else None,
        "published_by": route.published_by or "",
        "task_id": route.task_id,
        "snapshot_of_id": route.snapshot_of_id,
        "snapshot_version": route.snapshot_version,
        "snapshot_status": route.snapshot_status,
        "step_count": len(steps),
        "unit_sum": round(unit_sum, 4),
        "setup_sum": round(setup_sum, 4),
        "per_unit_hours": round(per_unit, 4),
        "steps": [step_out(s) for s in steps],
    }


def step_out(s: ProcessRouteStep) -> dict:
    return {
        "id": s.id,
        "route_id": s.route_id,
        "seq": s.seq,
        "process_code": s.process_code,
        "process_name": s.process_name,
        "op_type": s.op_type,
        "category": s.category,
        "work_center": s.work_center,
        "setup_hours": s.setup_hours,
        "unit_hours": s.unit_hours,
        "hours_src": s.hours_src,
        "samples": s.samples,
        "is_key": s.is_key,
        "inspect_required": s.inspect_required,
        "sop": s.sop,
        "remark": s.remark,
        # 「疑似非工序」标记存在 remark 里，这里还原成布尔量给前端高亮
        "not_in_dict": "疑似非工序" in (s.remark or ""),
    }


def get_route(db: Session, route_id: int) -> ProcessRoute | None:
    return db.get(ProcessRoute, route_id)


def find_route(
    db: Session,
    product: str,
    part: str,
    kind: str = "route",
    status: str | None = None,
) -> ProcessRoute | None:
    stmt = select(ProcessRoute).where(
        ProcessRoute.kind == kind,
        ProcessRoute.product == (product or ""),
        ProcessRoute.part == (part or ""),
    )
    if status:
        stmt = stmt.where(ProcessRoute.status == status)
    return db.scalars(stmt.order_by(ProcessRoute.version.desc())).first()


def create_draft(
    db: Session,
    product: str,
    part: str,
    *,
    kind: str = "route",
    title: str = "",
    steps: list[dict] | None = None,
    source: str = "manual",
    created_by: str = "",
    batch_qty: float = 0.0,
    remark: str = "",
) -> ProcessRoute:
    """新建路线草稿。同一 (kind, product, part) 已有未归档版本时版本号顺延。"""
    product, part = _key_of(product, part)
    existing = db.scalars(
        select(ProcessRoute).where(
            ProcessRoute.kind == kind,
            ProcessRoute.product == product,
            ProcessRoute.part == part,
        )
    ).all()
    version = max((r.version for r in existing), default=0) + 1
    # 产品主数据外键：建路线时就绑定，别等到事后回填。
    # 只按文本存产品名的话，「统管」还是字符串匹配，换个写法就断。
    from .tasks import resolve_product

    _prod = resolve_product(db, product)
    route = ProcessRoute(
        kind=kind,
        product=product,
        product_id=_prod.id if _prod else None,
        part=part,
        title=title or (f"{product} / {part}" if part else product),
        version=version,
        status="draft",
        completeness="partial",
        batch_qty=batch_qty or 0.0,
        source=source,
        remark=remark,
        created_by=created_by,
    )
    db.add(route)
    db.flush()
    if steps:
        save_steps(db, route, steps)
    return route


def normalize_steps(steps: list[dict]) -> list[dict]:
    """工序行入库前的统一校验与规整。

    三条硬规则，都是「错了会让下游算错账」的那种：

    1. **一行必须能认出是哪道工序**：没选工序编码也没填工序名，这行就是废行，
       留着它会以「空工序」混进定额，实报永远对不上。
    2. **工时不能为负**：负数会凭空抵扣别人的工时，比漏填更难查。
    3. **工序号不能重复**：`uq_route_step_seq` 会直接拒绝整批保存，
       但报出来的是数据库错误，用户不知道是哪两行撞了。这里顺延处理并保留顺序，
       比让整次保存失败友好得多。
    """
    out: list[dict] = []
    seen: set[int] = set()
    for i, s in enumerate(steps, start=1):
        code = str(s.get("process_code") or "").strip()
        name = str(s.get("process_name") or "").strip()
        if not code and not name:
            raise ValueError(f"第 {i} 行没有选工序，也没有填工序名")
        try:
            setup = float(s.get("setup_hours") or 0.0)
            unit = float(s.get("unit_hours") or 0.0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"第 {i} 行的工时不是数字") from exc
        if setup < 0 or unit < 0:
            raise ValueError(f"第 {i} 行（{name or code}）的工时不能为负数")
        try:
            seq = int(s.get("seq") or i * SEQ_STEP)
        except (TypeError, ValueError):
            seq = i * SEQ_STEP
        if seq <= 0:
            seq = i * SEQ_STEP
        while seq in seen:  # 撞号顺延，保持相对顺序
            seq += 1
        seen.add(seq)
        row = dict(s)
        row.update(
            {
                "seq": seq,
                # 只填了编码的（从字典里选的），用编码兜底名字，
                # 否则工序列表里会显示成一串数字
                "process_name": name or code,
                "setup_hours": setup,
                "unit_hours": unit,
            }
        )
        out.append(row)
    return out


def active_ref_count(db: Session, route_id: int) -> int:
    """这条路线被多少个在制任务引用（挂任务上、未移除）。"""
    return int(
        db.scalar(
            select(func.count())
            .select_from(TaskRoute)
            .where(TaskRoute.route_id == route_id, TaskRoute.status == "active")
        )
        or 0
    )


# ------------------------------------------------------------ 升版通知
def _unit_total(route: ProcessRoute) -> float:
    return round(sum(float(s.unit_hours or 0.0) for s in route.steps), 4)


def record_notice(
    db: Session,
    new_route: ProcessRoute,
    old_route: ProcessRoute | None,
    *,
    action: str = "publish",
    actor: str = "",
    note: str = "",
) -> RouteVersionNotice | None:
    """记下「某条在用基准被换成了新版本」这件事。

    没有旧版就不算升版（新品首次发布），不产生通知 —— 没人需要为一个
    从来没用过的标准做确认，报了只会稀释真正要看的提醒。

    受影响任务数在这里只是一个快照：引用重指可能发生在之后（调用方负责），
    界面上真正显示的数量一律现算，不依赖这个数。
    """
    if old_route is None or old_route.id == new_route.id:
        return None
    tasks = set(
        db.scalars(
            select(TaskRoute.task_id).where(
                TaskRoute.route_id.in_([old_route.id, new_route.id]),
                TaskRoute.status == "active",
            )
        ).all()
    )
    notice = RouteVersionNotice(
        route_id=new_route.id,
        from_route_id=old_route.id,
        product=new_route.product,
        part=new_route.part,
        from_version=int(old_route.version or 0),
        to_version=int(new_route.version or 0),
        action=action or "publish",
        actor=(actor or new_route.published_by or "").strip(),
        scope_tasks=len(tasks),
        unit_delta=round(_unit_total(new_route) - _unit_total(old_route), 4),
        note=(note or "").strip(),
    )
    db.add(notice)
    db.flush()
    return notice


def notice_out(n: RouteVersionNotice | None) -> dict | None:
    if n is None:
        return None
    return {
        "id": n.id,
        "route_id": n.route_id,
        "from_route_id": n.from_route_id,
        "product": n.product,
        "part": n.part,
        "from_version": n.from_version,
        "to_version": n.to_version,
        "action": n.action,
        "actor": n.actor,
        "scope_tasks": n.scope_tasks,
        "unit_delta": n.unit_delta,
        "note": n.note,
        "at": n.created_at.isoformat() if n.created_at else None,
    }


def notices_by_pair(db: Session, pairs: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """每个「产品×件名」最近一次升版的通知，批量取，避免逐条查。"""
    if not pairs:
        return {}
    products = {p for p, _ in pairs}
    rows = db.scalars(
        select(RouteVersionNotice)
        .where(RouteVersionNotice.product.in_(products))
        .order_by(RouteVersionNotice.created_at.desc(), RouteVersionNotice.id.desc())
    ).all()
    wanted = set(pairs)
    out: dict[tuple[str, str], dict] = {}
    for n in rows:
        key = (n.product, n.part)
        if key in wanted and key not in out:
            out[key] = notice_out(n)
    return out


def ref_tasks(db: Session, route_id: int, *, limit: int = 200) -> list[dict]:
    """哪些任务在引用这条路线 —— 编排页改基准前的影响面。

    光说「3 个任务」不够：得让人看见是哪三单，才知道要不要先通知车间。
    """
    rows = db.execute(
        select(TaskRoute.id, TaskRoute.origin, TaskRoute.part, WorkTask.id, WorkTask.task_no,
               WorkTask.title, WorkTask.status, TaskRoute.seen_route_id)
        .join(WorkTask, WorkTask.id == TaskRoute.task_id)
        .where(TaskRoute.route_id == route_id, TaskRoute.status == "active")
        .order_by(WorkTask.id)
        .limit(max(1, limit))
    ).all()
    return [
        {
            "task_route_id": tr_id,
            "task_id": task_id,
            "task_no": task_no,
            "title": title,
            "task_status": status,
            "origin": origin,
            "part": part,
            "confirmed": seen_route_id == route_id,
        }
        for tr_id, origin, part, task_id, task_no, title, status, seen_route_id in rows
    ]


def save_steps(
    db: Session, route: ProcessRoute, steps: list[dict], *, allow_task: bool = False
) -> None:
    """全量替换工序行。

    只允许改草稿。唯一例外是**任务工艺**（kind='task' 的本单副本）：
    它建好即生效、不走发布流程，所以没有草稿态可等 —— 调用方必须显式传
    `allow_task=True` 才能改，避免编排页误改任务的私有工艺。

    用 relationship 的 clear/append 而不是 db.add(子对象)：直接 add 的话
    route.steps 这个集合不会被同步，调用方紧接着读 route.steps 会拿到空列表
    （首次访问时缓存了空集合），于是 publish 的「空路线不能发布」会误判。
    """
    if route.status != "draft":
        if not (allow_task and route.kind == "task"):
            raise ValueError("已发布或已归档的路线不能直接修改，请升版")
    clean = normalize_steps(steps)
    route.steps.clear()
    db.flush()
    for i, s in enumerate(clean, start=1):
        seq = int(s["seq"])
        route.steps.append(
            ProcessRouteStep(
                seq=seq,
                process_code=s.get("process_code") or "",
                process_name=s.get("process_name") or "",
                op_type=s.get("op_type") or "self",
                category=s.get("category") or "",
                work_center=s.get("work_center") or "",
                setup_hours=float(s.get("setup_hours") or 0.0),
                unit_hours=float(s.get("unit_hours") or 0.0),
                hours_src=s.get("hours_src") or "manual",
                samples=int(s.get("samples") or 0),
                is_key=bool(s.get("is_key")),
                inspect_required=bool(s.get("inspect_required")),
                sop=s.get("sop") or "",
                remark=s.get("remark") or "",
            )
        )
    db.flush()


def _combo_of(db: Session, route: ProcessRoute) -> WorkCombo | None:
    """路线对应的组合身份行（没有就是 None，不在这里造）。"""
    if route.kind != "route":
        return None
    return db.scalar(
        select(WorkCombo).where(
            WorkCombo.product == (route.product or "").strip(),
            WorkCombo.part == (route.part or "").strip(),
        )
    )


def _sync_combo(db: Session, route: ProcessRoute) -> None:
    """路线发布/撤回时把组合的对外状态一起改，别让人点两次同一个意思的按钮。

    为什么联动：在编排页点「发布」的人就是工艺员，这个动作本身就是
    「这版工艺可以用了」。如果路线发了、组合还停在草稿，任务挂线照样看不见它，
    等于逼人去台账再点一次。台账那边的「发布」反过来也调 publish()，
    两条路最后落在同一个状态上，不会各说各话。
    """
    if route.kind != "route":
        return

    combo = _combo_of(db, route)
    if combo is None:
        combo = WorkCombo(
            product=(route.product or "").strip(),
            part=(route.part or "").strip(),
            product_id=route.product_id,
            status="draft",
            note="发布路线时自动登记",
        )
        db.add(combo)
        db.flush()
    # 停用的组合不因路线动作复活：在编排页随手发布一版，就等于静默撤销了
    # 别人做的「下架」。启用是人的显式动作（台账上的「启用」），不是副作用。
    if combo.status == "archived":
        return
    if route.status == "released":
        combo.status = "released"
        combo.route_id = route.id
        combo.published_at = route.published_at
        combo.published_by = route.published_by
        combo.demoted_from = ""
    elif combo.route_id == route.id:
        # 撤回到草稿：只有「原来指向的就是这一版」时才清掉引用，
        # 升版留下的旧版归档不该把新版的生效关系一起抹掉
        combo.status = "draft"
        combo.route_id = None
    db.add(combo)
    db.flush()


def publish(
    db: Session,
    route: ProcessRoute,
    new_version: bool = False,
    *,
    published_by: str = "",
    action: str = "publish",
    note: str = "",
) -> ProcessRoute:
    """发布路线。同一 (kind, product, part) 只允许一条 released。

    已发布的路线不能就地改，否则历史单据的比对基准会被悄悄改掉，
    所以「改已发布的路线」走升版：`new_version=True`（等同 revision()）
    生成一份**可编辑的新草稿**，旧版继续生效，直到新草稿真正发布才归档。

    发布是**唯一**能让路线进入「可被任务引用」状态的动作，因此要留痕：
    谁、什么时候定的这一版基准，出了偏差要能追到人。

    若这一版顶掉了原先生效的基准（旧版被归档），同时记一条 `RouteVersionNotice`：
    引用这条标准的任务会因此「换了一个定额」，必须能在任务、单据、编排页
    各个引用处看见提醒，而不是静默生效。
    """
    if route.kind == "task":
        raise ValueError("任务工艺建好即生效，不走发布流程；要调整请直接改任务工艺")
    if route.kind == "route":
        # 停用的组合不能再产出新的已发布版本。否则库里会多出一条没人认领的
        # 已发布路线：索引不收它（组合是归档的），编排页却显示「已发布」，
        # 两边从此对不上。要发布就先在台账里启用。
        combo = _combo_of(db, route)
        if combo is not None and combo.status == "archived":
            raise ValueError("该组合已停用（归档），不能发布；请先在「工时组合」台账里启用")
    if new_version:
        return revision(db, route)
    if not route.steps:
        raise ValueError("空路线不能发布：至少要有 1 道工序")
    if route.status == "released":
        raise ValueError("该路线已发布，如需修改请升版")
    if route.status == "archived":
        raise ValueError("已归档的路线不能直接发布，请先恢复为草稿")

    # 同一产品件名的其它已发布版全部归档，保证只有一条基准
    replaced: list[ProcessRoute] = []
    for r in db.scalars(
        select(ProcessRoute).where(
            ProcessRoute.kind == route.kind,
            ProcessRoute.product == route.product,
            ProcessRoute.part == route.part,
            ProcessRoute.status == "released",
            ProcessRoute.id != route.id,
        )
    ).all():
        r.status = "archived"
        r.updated_at = dt.datetime.now()
        db.add(r)
        replaced.append(r)
    # 必须先落盘再改本条：两张 UPDATE 同表，执行顺序不保证，
    # 一旦本条先改成 released 就会撞 uq_route_released 唯一索引。
    db.flush()

    route.status = "released"
    route.published_at = dt.datetime.now()
    route.published_by = (published_by or route.created_by or "").strip()
    route.updated_at = dt.datetime.now()
    db.add(route)
    db.flush()

    if replaced:
        # 旧版里版本号最高的那一条才是刚刚被顶掉的现行基准
        old = max(replaced, key=lambda r: (r.version or 0))
        record_notice(db, route, old, action=action, actor=route.published_by, note=note)
    _sync_combo(db, route)
    return route



def revision(db: Session, route: ProcessRoute) -> ProcessRoute:
    """升版：复制当前路线成一份可编辑的新草稿。

    旧版**不归档**——它仍是核对基准，直到新草稿发布时才会被 publish() 归档。
    这样「改已发布路线」既不会出现没有基准的空窗，也保留了旧版可追溯。
    """
    if route.kind == "task":
        raise ValueError("任务工艺不支持升版，请用快照重建（force=True）")
    new_draft = create_draft(
        db,
        route.product,
        route.part,
        kind=route.kind,
        title=route.title,
        steps=[step_out(s) for s in route.steps],
        source=route.source,
        created_by=route.created_by,
        batch_qty=route.batch_qty,
        remark=route.remark,
    )
    db.flush()
    return new_draft


def reopen(db: Session, route: ProcessRoute) -> ProcessRoute:
    """撤回发布：已发布 → 草稿，让原稿能就地改。

    与升版的区别：升版保留旧版作为基准，撤回则让该组合**暂时没有基准**
    （单据核对不再按它比对）。所以只在「小改一处、马上重发」时用。

    被在制任务引用时**不允许撤回**：那些任务会瞬间失去核对基准，
    实报工时就没了对照。要改就走升版 —— 旧版在被新版取代前一直有效。
    """
    if route.status != "released":
        raise ValueError("只有已发布的路线可以撤回")
    refs = active_ref_count(db, route.id)
    if refs:
        raise ValueError(
            f"这条路线正被 {refs} 个在制任务引用，撤回会让它们失去核对基准；"
            "要改请用「升版修改」（旧版会继续生效直到新版发布）"
        )
    route.status = "draft"
    route.updated_at = dt.datetime.now()
    db.add(route)
    db.flush()
    _sync_combo(db, route)
    return route


def restore(db: Session, route: ProcessRoute, to: str | None = None) -> ProcessRoute:
    """撤销归档：已归档 → 草稿（默认）或 → 已发布。

    归档不删数据，只是退出「当前基准」，所以必须能退回来：
    误点归档、旧版要重新启用、审核发现归档错了，都不该只剩重建一条路。
    """
    if route.status != "archived":
        raise ValueError("只有已归档的路线可以恢复")
    target = to or "draft"
    if target not in ("draft", "released"):
        raise ValueError("只能恢复为草稿或已发布")
    if target == "released":
        combo = _combo_of(db, route)
        if combo is not None and combo.status == "archived":
            raise ValueError(
                "该组合已停用（归档），不能恢复为已发布；请先在「工时组合」台账里启用"
            )
        # 同一组合已有基准时不能出现第二条已发布版
        other = db.scalar(
            select(ProcessRoute).where(
                ProcessRoute.kind == route.kind,
                ProcessRoute.product == route.product,
                ProcessRoute.part == route.part,
                ProcessRoute.status == "released",
                ProcessRoute.id != route.id,
            )
        )
        if other:
            raise ValueError(
                f"该组合已有已发布版本 v{other.version}，请先撤回或归档它，再恢复这一条"
            )
    route.status = target
    route.updated_at = dt.datetime.now()
    db.add(route)
    db.flush()
    _sync_combo(db, route)
    return route


def archive(db: Session, route: ProcessRoute) -> ProcessRoute:
    if route.status != "released":
        raise ValueError("只有已发布的路线需要归档；草稿请直接删除")
    refs = active_ref_count(db, route.id)
    if refs:
        raise ValueError(
            f"这条路线正被 {refs} 个在制任务引用，归档会让它们指向一条失效基准；"
            "先把这些任务改到其它版本，或直接升版发布新版（旧版会自动归档）"
        )
    route.status = "archived"
    route.updated_at = dt.datetime.now()
    db.add(route)
    db.flush()
    _sync_combo(db, route)
    return route


def delete_route(db: Session, route: ProcessRoute) -> None:
    """删除草稿或归档版本。已发布的不能删——它是核对基准，要先撤回或归档。"""
    if route.kind == "task":
        raise ValueError("任务工艺随任务存在，不能单独删除；请删除任务本身")
    if route.status == "released":
        raise ValueError("已发布的路线不能删除，请先撤回为草稿或归档")
    refs = active_ref_count(db, route.id)
    if refs:
        raise ValueError(f"这条路线正被 {refs} 个在制任务引用，不能删除")
    # 升版通知是跟着"哪一版被换成哪一版"走的，版本都没了，通知就是无主记录，
    # 留着只会在「最近一次升到 v几」里报一个不存在的版本号。
    product = (route.product or "").strip()
    part = (route.part or "").strip()
    route_id = route.id
    db.query(RouteVersionNotice).filter(
        or_(
            RouteVersionNotice.route_id == route.id,
            RouteVersionNotice.from_route_id == route.id,
        )
    ).delete(synchronize_session=False)
    db.delete(route)
    db.flush()
    # 删掉最后一版工艺之后，`work_combo` 这一行会变成「有身份、没工艺」的空壳：
    # 树上挂着「草稿」，回头删结构又被「有工艺身份」拦住。收尾放在这里，
    # 谁删路线都走同一条路，不指望每个入口都记得顺手清。
    from .combo_registry import cleanup_after_route_delete

    cleanup_after_route_delete(db, product, part, removed_id=route_id)


# ------------------------------------------------------------------ 组合树
UNSPECIFIED_PRODUCT = "（未指定产品）"


def tree(
    db: Session,
    *,
    kind: str = "route",
    q: str = "",
    only_unbuilt: bool = False,
    product: str | None = None,
) -> dict:
    """产品 → 件名 → 路线 的树，供工作台左栏使用。

    件名以「已建路线 + 有历史工时」的并集为准：只看已建的树会漏掉
    「该建还没建」的零件，恰好是工作台最需要提示的那部分。

    每个产品节点自带覆盖统计（已发布/草稿/归档/待建件数、工时价值），
    覆盖看板因此可以整个并进树里，不再单独占一块版面；产品按工时价值
    降序排，与批量生成的价值序一致——从上往下建就是最优路径。

    product：只看某个产品（精确匹配）。树里点「待建 N」时用它把视图收窄，
    比用 q 做子串匹配可靠——产品名互为前缀时子串匹配会带出别的产品。
    None = 不限产品；空串 = 「未指定产品」那个虚拟节点（真实产品名就是空的）。
    """
    routes = db.scalars(
        select(ProcessRoute)
        .where(ProcessRoute.kind == kind)
        .order_by(ProcessRoute.product, ProcessRoute.part, ProcessRoute.version.desc())
    ).all()

    # 一次聚合查完每条路线的工序数与单件工时合计，
    # 逐条 lazy load 会把树接口拖成 N+1
    step_stats: dict[int, tuple[int, float]] = {}
    if routes:
        for rid, cnt, usum in db.execute(
            select(
                ProcessRouteStep.route_id,
                func.count(ProcessRouteStep.id),
                func.coalesce(func.sum(ProcessRouteStep.unit_hours), 0.0),
            )
            .where(ProcessRouteStep.route_id.in_([r.id for r in routes]))
            .group_by(ProcessRouteStep.route_id)
        ).all():
            step_stats[rid] = (int(cnt or 0), round(float(usum or 0.0), 4))

    by_pair: dict[tuple[str, str], list[ProcessRoute]] = {}
    for r in routes:
        by_pair.setdefault((r.product, r.part), []).append(r)

    # 历史工时组合：没建路线的也要出现在树里，否则「该建还没建」的零件看不见
    combos = combo_index(db)
    for key in combos:
        by_pair.setdefault(key, [])

    # 装配层级：件名在产品结构里的位置（谁装在谁下面、第几层、同层次序）。
    # 左栏如果不带层级，47 个件名按件名字母排就是「47 个平级零件」，
    # 部件与它下面的子件混在一起，装配关系全丢 —— 而工艺编排恰恰要按
    # 「总装 → 部件 → 子件」的顺序自上而下编，顺序错了人就得反复找。
    from ..models import ProductNode

    node_rows = db.scalars(select(ProductNode)).all()
    # 结构里的件全部并入左栏。结构有、但既没建路线也没有历史工时的件
    # （刚在结构里新登记的），以前在这里根本不会出现 —— 人加完件找不着，
    # 只能怀疑自己没加成功。结构是真源，左栏就该照它列。
    for n in node_rows:
        by_pair.setdefault((n.product, n.part), [])
    nodes_by_product: dict[str, list[ProductNode]] = {}
    for n in node_rows:
        nodes_by_product.setdefault(n.product, []).append(n)
    node_of: dict[tuple[str, str], ProductNode] = {
        (n.product, n.part): n for n in node_rows
    }
    pair_of_node: dict[int, tuple[str, str]] = {
        n.id: (n.product, n.part) for n in node_rows
    }

    def _struct_order(pname: str) -> dict[str, int]:
        """按装配顺序给该产品的件名编号：总装 → 部件的所有子件 → 下一个部件。

        深度优先而不是按 level 分桶排：分桶排会把「部件 A 的子件」和
        「部件 B 的子件」并成一段，缩进对得上、父子关系却对不上。
        """
        kids: dict[int | None, list[ProductNode]] = {}
        for n in nodes_by_product.get(pname, []):
            kids.setdefault(n.parent_id, []).append(n)
        for lst in kids.values():
            lst.sort(key=lambda n: (n.seq or 0, n.part))
        order: dict[str, int] = {}
        stack = list(reversed(kids.get(None, [])))
        while stack:
            n = stack.pop()
            order.setdefault(n.part, len(order))
            stack.extend(reversed(kids.get(n.id, [])))
        # 父节点不在库里的孤儿件（结构没登记全）排在后面，别让它凭空消失
        for n in sorted(
            nodes_by_product.get(pname, []), key=lambda n: (n.level, n.seq or 0, n.part)
        ):
            order.setdefault(n.part, len(order))
        return order

    # 任务：树上要看到「这个零件被多少订单引用」。
    # 任务本身不再是「一个任务对应一个产品×件名」，归属在 task_route 上：
    # 一个订单挂多条路线，一条路线又可能被多个订单共用（引用基线）。
    from ..models import TaskRoute, WorkTask as _WorkTask
    from .tasks import norm_key as _norm_key, product_index as _product_index

    _pidx = _product_index(db)
    actual_by_pair: dict[tuple[str, str], float] = {}
    # 扫码报工的明细没有 sheet，靠 work_item 自己的归属进账；
    # inner join 会把它们全部丢掉，结构树上「实报工时」就永远是 0。
    for pname, part, hours in db.execute(
        select(WorkItem.product_name, WorkItem.part_name, func.sum(WorkItem.total_hours))
        .outerjoin(Sheet, Sheet.id == WorkItem.sheet_id)
        .where(or_(Sheet.status == "verified", Sheet.id.is_(None)))
        .group_by(WorkItem.product_name, WorkItem.part_name)
    ).all():
        prod = _pidx.get(_norm_key(pname or ""))
        key = (prod.name if prod else (pname or ""), part or "")
        actual_by_pair[key] = actual_by_pair.get(key, 0.0) + float(hours or 0.0)

    tasks_by_pair: dict[tuple[str, str], list[dict]] = {}
    route_by_id = {r.id: r for r in routes}
    for tr, task in db.execute(
        select(TaskRoute, _WorkTask)
        .join(_WorkTask, _WorkTask.id == TaskRoute.task_id)
        .where(TaskRoute.status == "active")
        .order_by(TaskRoute.task_id)
    ).all():
        ref = route_by_id.get(tr.route_id)
        tasks_by_pair.setdefault((tr.product, tr.part), []).append(
            {
                "id": task.id,
                "task_no": task.task_no,
                "title": task.title,
                "batch_no": tr.batch_no,
                "qty": round(float(tr.qty or 0), 4),
                "actual_hours": round(actual_by_pair.get((tr.product, tr.part), 0.0), 4),
                "status": task.status,
                "origin": tr.origin,
                "task_route_id": tr.id,
                "has_route": True,
                "route_id": tr.route_id,
                "route_version": ref.version if ref else None,
                # 引用是否已经被人认过：树上的「N 单」要能分出「有待确认的」
                "confirmed": tr.seen_route_id == tr.route_id,
                "ref_released": bool(ref and ref.status == "released"),
            }
        )

    # 产品级价值：树按它降序排，先建值钱的
    # 注意：循环变量不能叫 product，它会覆盖同名的筛选参数
    product_value: dict[str, float] = {}
    for (pname, _part), bucket in combos.items():
        product_value[pname] = product_value.get(pname, 0.0) + bucket["value"]
    total_value = sum(product_value.values())

    # 本单副本：基线树里看不到它们（kind='task'），但必须让人知道
    # 「这个件名的标准之外还存在 N 份本单工艺」—— 否则副本拆出去就成了暗账。
    copies_by_pair: dict[tuple[str, str], list[ProcessRoute]] = {}
    for c in db.scalars(
        select(ProcessRoute).where(ProcessRoute.kind == "task").order_by(ProcessRoute.id)
    ).all():
        copies_by_pair.setdefault((c.product, c.part), []).append(c)

    tree_items: list[dict] = []
    for (pkey, ppart), rs in by_pair.items():
        released = [r for r in rs if r.status == "released"]
        drafts = [r for r in rs if r.status == "draft"]
        archived = [r for r in rs if r.status == "archived"]
        # 组合下只剩归档版本时（发布过又归档），仍要能看到入口，
        # 否则用户在树上点不进去，没法从旧版本升版。
        if released:
            state = "released"
        elif drafts:
            state = "draft"
        elif archived:
            state = "archived"
        else:
            state = "none"
        anchor = (released or drafts or archived or [None])[0]
        scount, sunit = step_stats.get(anchor.id, (0, 0.0)) if anchor else (0, 0.0)
        pair_tasks = tasks_by_pair.get((pkey, ppart), [])
        # 待确认升版：树上一眼能看出来「这件的引用里有几单还没认新版」
        pending_n = sum(
            1
            for t in pair_tasks
            if t["origin"] == "baseline"
            and (
                not t["confirmed"]
                or not t["ref_released"]
                or (anchor is not None and t["route_id"] != anchor.id)
            )
        )
        # 结构位置：没登记的件名（只有历史工时、结构里还没建）level 为空，
        # 前端按「无父级 = 顶层」处理，不让它因为缺结构就看不见
        node = node_of.get((pkey, ppart))
        parent_pair = pair_of_node.get(node.parent_id) if node and node.parent_id else None
        tree_items.append(
            {
                "product": pkey,
                "part": ppart,
                "level": node.level if node else None,
                "parent": parent_pair[1] if parent_pair else None,
                "seq": node.seq if node else None,
                # 结构数量：左栏要能直接读出「装一台用几件」和「这一批做几件」，
                # 不必为看一眼数量而打开结构弹窗
                "qty_per_unit": round(float(node.qty_per_unit or 1), 4) if node else None,
                "plan_qty": round(float(node.plan_qty or 0), 4) if node else None,
                "plan_src": (node.plan_src or "auto") if node else None,
                "state": state,
                "route_id": anchor.id if anchor else None,
                "version": anchor.version if anchor else None,
                # 树上一行就能看出这条路线的规模，不用点进去
                "step_count": scount,
                "unit_hours": sunit,
                # 任务来源：这个零件被几张单做过、实报多少工时
                "task_count": len(pair_tasks),
                "task_hours": round(sum(x["actual_hours"] for x in pair_tasks), 4),
                "tasks": pair_tasks,
                "pending_count": pending_n,
                # 本单副本：与基线并列显示，界面用不同颜色标签区分
                "copy_count": len(copies_by_pair.get((pkey, ppart), [])),
                "copies": [
                    {
                        "id": c.id,
                        "version": c.version,
                        "status": c.status,
                        "task_id": c.task_id,
                        "step_count": len(c.steps),
                        "unit_hours": round(
                            sum(float(s.unit_hours or 0) for s in c.steps), 4
                        ),
                    }
                    for c in copies_by_pair.get((pkey, ppart), [])
                ],
                "versions": [
                    {
                        "id": r.id,
                        "version": r.version,
                        "status": r.status,
                        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
                    }
                    for r in rs
                ],
            }
        )

    # 按产品分组
    grouped: dict[str, list[dict]] = {}
    for item in tree_items:
        grouped.setdefault(item["product"], []).append(item)

    out: list[dict] = []
    for pname, parts in grouped.items():
        # product=None 表示不限；空串表示「未指定产品」那个节点，
        # 所以不能用 `if product` 这种真值判断
        if product is not None and pname != product:
            continue
        if q and q.strip():
            kw = q.strip()
            if kw not in pname and not any(kw in (x["part"] or "") for x in parts):
                continue
        # 件名按装配顺序排：父在上、子紧跟在父下面，同层按结构里的 seq。
        # 结构里没登记的件名（只有历史工时）排在最后，按件名兜底。
        _order = _struct_order(pname)
        released_n = sum(1 for x in parts if x["state"] == "released")
        draft_n = sum(1 for x in parts if x["state"] == "draft")
        archived_n = sum(1 for x in parts if x["state"] == "archived")
        unbuilt_n = sum(1 for x in parts if x["state"] == "none")
        value = product_value.get(pname, 0.0)
        # 覆盖率要在 only_unbuilt 过滤**之前**算：过滤后 parts 里只剩未建的，
        # 再算就永远是 0%，进度条会失真
        covered_value = sum(
            combos[(pname, x["part"])]["value"]
            for x in parts
            if x["state"] == "released" and (pname, x["part"]) in combos
        )
        # 「被任务验证过」是比「建了基线」更硬的指标：基线可能只是照统计生成的，
        # 有任务真按它做过，这条定额才算落过地。
        verified_n = sum(1 for x in parts if x["state"] == "released" and x["task_count"] > 0)
        verified_value = sum(
            combos[(pname, x["part"])]["value"]
            for x in parts
            if x["state"] == "released"
            and x["task_count"] > 0
            and (pname, x["part"]) in combos
        )
        task_n = sum(x["task_count"] for x in parts)
        pending_total = sum(x["pending_count"] for x in parts)
        task_hours = round(sum(x["task_hours"] for x in parts), 4)
        copy_n = sum(x["copy_count"] for x in parts)
        if only_unbuilt:
            # 只有「从未建过」的组合算未建；草稿/已发布/已归档都算建过了
            parts = [x for x in parts if x["state"] == "none"]
            if not parts:
                continue
        parts.sort(key=lambda x: (_order.get(x["part"], 10**6), x["part"]))
        out.append(
            {
                # 历史工时里有 195 条没写产品（全是钳工序），这类组合归到
                # 「未指定产品」节点下，而不是显示一个空标题让人以为是 bug
                "name": pname or UNSPECIFIED_PRODUCT,
                "raw_name": pname,
                "unspecified": not pname,
                # —— 覆盖看板并入树后，产品行自带这组统计 ——
                "part_count": released_n + draft_n + archived_n + unbuilt_n,
                "released_count": released_n,
                "draft_count": draft_n,
                "archived_count": archived_n,
                "unbuilt_count": unbuilt_n,
                # 任务来源：该产品挂了多少生产任务、实报工时、多少件名的基线被任务用过
                "task_count": task_n,
                "task_hours": task_hours,
                "copy_count": copy_n,
                # 该产品下还有多少条引用没确认升版
                "pending_count": pending_total,
                "verified_count": verified_n,
                "value": round(value, 2),
                # 占全厂工时的比重
                "pct": round(value / total_value * 100, 2) if total_value else 0.0,
                # 该产品自身的工时覆盖率（已发布路线覆盖了该产品多少工时）
                "coverage_pct": (
                    round(covered_value / value * 100, 2) if value else 0.0
                ),
                # 被任务验证过的工时占比：0% 说明工艺还是纸面的
                "verified_pct": (
                    round(verified_value / value * 100, 2) if value else 0.0
                ),
                "parts": parts,
            }
        )
    # 按工时价值降序：先建值钱的，和批量生成的价值序保持一致
    out.sort(key=lambda x: (-x["value"], x["name"]))
    return {"total": len(out), "items": out}


# ------------------------------------------------------------------ 已发布索引
def released_index(db: Session) -> dict[tuple[str, str], list[dict]]:
    """(产品, 件名) → 已发布路线的工序行，供识别校验使用。

    只取 released：草稿是未定稿的东西，拿去校验单据会让人无所适从。

    必须读库里最新的状态，不能用 session 里的对象：发布/归档路线后紧接着
    重校验时，SQLAlchemy 的身份映射会返回**缓存里的旧对象**（status 还是
    改之前的值），导致刚归档的路线仍在参与校验、刚发布的路线查不到。
    populate_existing 强制刷新已加载进 session 的对象。
    """
    from ..models import WorkCombo

    # 「已发布路线」不再等于「可用工艺」：组合本身要是 draft（工艺科没认），
    # 它连被引用去校验单据的资格都没有。所以先看组合状态，再回表取路线。
    #
    # 兼容兜底：work_combo 还一条都没铺时（老库升级、迁移脚本没跑），
    # 退回旧口径。否则升级那一刻全厂单据会同时失去工序校验 —— 一个静默的、
    # 谁也说不清的能力真空，比多校验几条更糟。
    combo_total = db.scalar(select(func.count()).select_from(WorkCombo)) or 0
    if combo_total:
        allowed = {
            c.route_id
            for c in db.scalars(
                select(WorkCombo)
                .where(WorkCombo.status == "released", WorkCombo.route_id.is_not(None))
                .execution_options(populate_existing=True)
            ).all()
        }
        stmt = select(ProcessRoute).where(
            ProcessRoute.kind == "route",
            ProcessRoute.status == "released",
            ProcessRoute.id.in_(allowed or [-1]),
        )
    else:
        stmt = select(ProcessRoute).where(
            ProcessRoute.kind == "route", ProcessRoute.status == "released"
        )
    routes = db.scalars(stmt.execution_options(populate_existing=True)).all()
    index: dict[tuple[str, str], list[dict]] = {}
    for r in routes:
        steps = [
            {
                "seq": s.seq,
                "process_code": s.process_code,
                "process_name": s.process_name,
                "unit_hours": s.unit_hours,
                "setup_hours": s.setup_hours,
                "batch_qty": r.batch_qty,
                "is_key": s.is_key,
                "inspect_required": s.inspect_required,
            }
            for s in r.steps
        ]
        index[(_key_of(r.product, r.part))] = steps
    return index


def batch_generate(
    db: Session,
    limit: int = 50,
    *,
    offset: int = 0,
    auto_publish: bool = False,
    skip_existing: bool = True,
    created_by: str = "system",
    dry_run: bool = False,
) -> dict:
    """按价值排序批量生成路线草稿。

    首期建线用：手工建 50~80 条要一整天，批量生成 + 人工抽查快得多。
    顺序与工时口径跟 suggest_steps 完全一致（真单据顺序 → 工艺默认序 → 样本数）。

    skip_existing=True 时跳过已经有任何版本的组合，避免版本号分叉。
    auto_publish=True 时生成后直接发布——适合「先铺开再逐条调整」的用法，
    但要注意发布后就得升版才能改。
    offset：跳过清单前 N 条再开始。首期建线分批推进时用得上
    （第一批建完前 80 条，第二批从第 81 条继续，而不是每次都从头跳）。
    """
    cov = coverage(db, limit=1000)
    existing = {
        _key_of(r.product, r.part)
        for r in db.scalars(
            select(ProcessRoute).where(ProcessRoute.kind == "route")
            .execution_options(populate_existing=True)
        ).all()
    }

    created: list[dict] = []
    skipped: list[dict] = []
    processed = 0
    for idx, item in enumerate(cov["items"]):
        # offset：跳过清单前 N 条。分批建线时用（第二批从第 81 条继续）
        if idx < offset:
            continue
        # limit 是「本次处理多少条」，跳过的也占额度。
        # 否则重复点一次会跳过已有的、再往下补满新的，
        # 用户以为没重复生成，实际多建了一堆。
        if processed >= limit:
            break
        processed += 1
        key = _key_of(item["product"], item["part"])
        if skip_existing and key in existing:
            skipped.append({"product": key[0], "part": key[1], "reason": "已有版本"})
            continue
        steps = suggest_steps(db, key[0], key[1])
        if not steps:
            skipped.append({"product": key[0], "part": key[1], "reason": "无可用工序"})
            continue
        if dry_run:
            created.append(
                {
                    "product": key[0],
                    "part": key[1],
                    "value": item["value"],
                    "step_count": len(steps),
                    "unit_sum": round(sum(s["unit_hours"] for s in steps), 4),
                }
            )
            continue
        route = create_draft(
            db, key[0], key[1], steps=steps, source="history", created_by=created_by
        )
        if auto_publish:
            publish(db, route)
        created.append(
            {
                "id": route.id,
                "product": key[0],
                "part": key[1],
                "value": item["value"],
                "step_count": len(steps),
                "unit_sum": round(sum(s["unit_hours"] for s in steps), 4),
                "status": route.status,
            }
        )
    db.commit()
    return {
        "created": created,
        "skipped": skipped,
        "created_count": len(created),
        "skipped_count": len(skipped),
        "dry_run": dry_run,
    }


# ------------------------------------------------------------------ 覆盖率
def combo_index(db: Session) -> dict[tuple[str, str], dict]:
    """产品×件名 → 历史工时汇总。

    价值口径：value = Σ(unit_hours × samples)，即该组合历史上累计投入工时的
    近似量。没有件名的组合构不成路线，直接丢弃。

    树接口与覆盖率接口共用这一份口径，避免两边各算一套对不上。
    """
    out: dict[tuple[str, str], dict] = {}
    for h in db.scalars(select(StandardHour).where(StandardHour.active)).all():
        key = _key_of(h.product, h.part)
        if not key[1]:
            continue
        bucket = out.setdefault(key, {"value": 0.0, "samples": 0, "processes": {}})
        bucket["value"] += float(h.unit_hours or 0.0) * int(h.samples or 0)
        bucket["samples"] += int(h.samples or 0)
        bucket["processes"][h.process] = float(h.unit_hours or 0.0)
    return out


def coverage(db: Session, limit: int = 200) -> dict:
    """按价值排序的「待建清单」。

    价值口径：value = Σ(unit_hours × samples)，即该「产品×件名」历史上
    累计投入工时的近似量。按这个排序，先建前 50~80 条就能覆盖大部分工时。
    """
    value: dict[tuple[str, str], dict] = {}
    for key, bucket in combo_index(db).items():
        value[key] = {
            "product": key[0],
            "part": key[1],
            "value": bucket["value"],
            "processes": bucket["processes"],
            "samples": bucket["samples"],
        }

    routes = db.scalars(
        select(ProcessRoute)
        .where(ProcessRoute.kind == "route")
        .execution_options(populate_existing=True)
    ).all()
    by_key: dict[tuple[str, str], list[ProcessRoute]] = {}
    for r in routes:
        by_key.setdefault(_key_of(r.product, r.part), []).append(r)

    total_value = sum(v["value"] for v in value.values()) or 0.0
    items: list[dict] = []
    covered_value = 0.0
    covered_count = 0
    draft_count = 0
    for key, bucket in value.items():
        rs = by_key.get(key, [])
        released = [r for r in rs if r.status == "released"]
        drafts = [r for r in rs if r.status == "draft"]
        state = "released" if released else ("draft" if drafts else "none")
        if state == "released":
            covered_value += bucket["value"]
            covered_count += 1
        elif state == "draft":
            draft_count += 1
        items.append(
            {
                "product": key[0],
                "part": key[1],
                "value": round(bucket["value"], 2),
                "samples": bucket["samples"],
                "process_count": len(bucket["processes"]),
                "processes": bucket["processes"],
                "state": state,
                "route_id": released[0].id if released else (drafts[0].id if drafts else None),
                "version": (released[0].version if released else (drafts[0].version if drafts else None)),
            }
        )

    items.sort(key=lambda x: -x["value"])

    # 按产品聚合：看板要看的是「哪个产品还没建路线」，
    # 而不是逐个组合看（996 个组合没法一眼看完）
    by_product: dict[str, dict] = {}
    for x in items:
        p = x["product"] or ""
        a = by_product.setdefault(
            p,
            {
                "product": p,
                "value": 0.0,
                "count": 0,
                "covered": 0,
                "draft": 0,
                "covered_value": 0.0,
            },
        )
        a["value"] += x["value"]
        a["count"] += 1
        if x["state"] == "released":
            a["covered"] += 1
            a["covered_value"] += x["value"]
        elif x["state"] == "draft":
            a["draft"] += 1
    products = sorted(by_product.values(), key=lambda a: -a["value"])
    for a in products:
        a["value"] = round(a["value"], 2)
        a["pct"] = round(a["value"] / total_value * 100, 2) if total_value else 0.0
        # 覆盖率保留 2 位小数：单建一条低价值路线时 1 位小数会显示成 0.0%，
        # 用户看不出任何变化，会以为没生效
        a["coverage_pct"] = (
            round(a["covered_value"] / a["value"] * 100, 2) if a["value"] else 0.0
        )
        a["unbuilt"] = a["count"] - a["covered"] - a["draft"]
        a["covered_value"] = round(a["covered_value"], 2)

    return {
        "total": len(items),
        "total_value": round(total_value, 2),
        "covered_value": round(covered_value, 2),
        # 覆盖率保留 2 位小数：单建一条低价值路线时 1 位小数会显示成 0.0%，
        # 用户看不出任何变化，会以为没生效
        "coverage_pct": round(covered_value / total_value * 100, 2) if total_value else 0.0,
        # 组合数是即时反馈：建一条就有 +1，不用等工时占比涨到看得见
        "covered_count": covered_count,
        "draft_count": draft_count,
        "unbuilt_count": len(items) - covered_count - draft_count,
        "products": products,
        "items": items[: max(1, min(limit, 1000))],
    }


# ------------------------------------------------------------------ 任务工艺快照
# 基线是主数据，任务工艺是快照。任务建立时把当时生效的基线复制一份：
# 之后改基线不影响已开工任务，改任务也不污染基线，「标准 vs 实际」才有意义。
def pick_baseline(db: Session, product: str, part: str) -> ProcessRoute | None:
    """选当前生效的基线：**只认已发布**。

    以前这里对草稿做了妥协（没有已发布版就退而取最新草稿），理由是首批 85 条
    基线全是草稿。但那等于把「未定稿的东西」当基准用：任务引用它、单据按它核对，
    一旦有人改草稿，历史工时就被悄悄重新定性了。

    现在改成严格只认 released，配套做两件事：
      1. 任务引用时如果只有草稿，直接报错并提示去发布（不静默降级）；
      2. 需要「新品第一次投产」时由调用方自动建基线并**立即发布**，
         这样引用的一定是定稿版本。
    """
    product, part = _key_of(product, part)
    # 停用（归档）的组合不算有生效基线：路线可能还停在 released —— 停用是
    # 「下架」不撤线，免得在制任务失去基准 —— 但新任务不该再挂到它上面。
    combo = db.scalar(
        select(WorkCombo).where(WorkCombo.product == product, WorkCombo.part == part)
    )
    if combo is not None and combo.status == "archived":
        return None
    return db.scalars(
        select(ProcessRoute)
        .where(
            ProcessRoute.kind == "route",
            ProcessRoute.product == product,
            ProcessRoute.part == part,
            ProcessRoute.status == "released",
        )
        .order_by(ProcessRoute.version.desc())
    ).first()


def latest_draft(db: Session, product: str, part: str) -> ProcessRoute | None:
    """该组合最新的草稿（用于「有草稿但没发布」这种提示）。"""
    product, part = _key_of(product, part)
    return db.scalars(
        select(ProcessRoute)
        .where(
            ProcessRoute.kind == "route",
            ProcessRoute.product == product,
            ProcessRoute.part == part,
            ProcessRoute.status == "draft",
        )
        .order_by(ProcessRoute.version.desc())
    ).first()


# 快照 / fork 已迁到 services/tasks.fork_route：
# 默认引用基线复用，只有确实要为本单改工艺时才拆副本，语义放在任务层更清楚。


def _steps_sig(steps) -> list[tuple]:
    """工序内容指纹：只比业务字段，用来判断「副本到底改没改」。"""
    rows = []
    for s in steps:
        rows.append(
            (
                int(getattr(s, "seq", 0) or 0),
                (getattr(s, "process_code", "") or "").strip(),
                (getattr(s, "process_name", "") or "").strip(),
                (getattr(s, "op_type", "") or "").strip(),
                (getattr(s, "category", "") or "").strip(),
                (getattr(s, "work_center", "") or "").strip(),
                round(float(getattr(s, "setup_hours", 0) or 0), 4),
                round(float(getattr(s, "unit_hours", 0) or 0), 4),
                bool(getattr(s, "is_key", False)),
                bool(getattr(s, "inspect_required", False)),
                (getattr(s, "sop", "") or "").strip(),
                (getattr(s, "remark", "") or "").strip(),
            )
        )
    rows.sort(key=lambda r: r[0])
    return rows


def purge_task_copy(db: Session, copy: ProcessRoute) -> int:
    """彻底删掉一份任务副本（含残留的 task_route 行与工序行）。

    引用必须先清、再删工序、最后删路线：ORM 的删除顺序按 mapper 依赖排，
    不显式 flush 就会撞 `FOREIGN KEY constraint failed`。
    """
    copy_id = copy.id
    db.query(TaskRoute).filter(TaskRoute.route_id == copy_id).delete(synchronize_session=False)
    db.flush()
    db.query(ProcessRouteStep).filter(ProcessRouteStep.route_id == copy_id).delete(
        synchronize_session=False
    )
    db.flush()
    db.delete(copy)
    db.flush()
    return copy_id


def promote_copy(
    db: Session,
    copy: ProcessRoute,
    *,
    task: WorkTask | None = None,
    created_by: str = "",
) -> dict:
    """把一份「本单副本」进阶为该产品×件名的**基线新版**。

    副本是「那一单跟标准不一样」的临时产物。当这张单的做法被证明是对的
    （质量、工时都站得住），就该升格成全厂标准 —— 否则别的单永远学不到它。
    这就是副本的唯一出口，没有别的路可走：副本不能直接改成基线，
    也不能靠手工复制粘贴一份，那样没有版本延续、也没有发布留痕。

    两种结局：

    * **merged**：副本内容与现行基线一模一样。没什么可发布的，
      直接把任务改回「引用基线」并删掉副本 —— 留一份冗余副本只会让人以为
      这单有特殊工艺，实际没有。
    * **promoted**：以副本工序为准升版并发布基线新版，旧版归档；
      所有引用旧版的在制任务自动切到新版，本任务也从副本改成引用。

    不管哪种，副本都会被删除：它的内容已经进了基线，留着就是双份真相。
    """
    if copy.kind != "task":
        raise ValueError("只有任务下的「本单副本」可以进阶为基线")
    if not copy.steps:
        raise ValueError("这份副本没有工序，不能进阶为基线；空工艺不能当标准")

    product, part = _key_of(copy.product, copy.part)
    if not product or not part:
        raise ValueError("副本缺产品/件名，无法对应到基线；请先补全再进阶")

    links = db.scalars(
        select(TaskRoute).where(
            TaskRoute.route_id == copy.id, TaskRoute.status == "active"
        )
    ).all()
    base = pick_baseline(db, product, part)
    actor = (created_by or copy.created_by or "operator").strip()

    # ---- 情形一：与现行基线完全一致 → 合并回引用，不白造一版
    if base is not None and _steps_sig(base.steps) == _steps_sig(copy.steps):
        for tr in links:
            tr.route_id = base.id
            tr.origin = "baseline"
            tr.seen_route_id = base.id  # 内容一致，等于已认可这一版
            tr.seen_at = dt.datetime.now()
            tr.seen_by = actor
        db.flush()
        deleted = purge_task_copy(db, copy)
        return {
            "action": "merged",
            "message": f"副本与现行基线 v{base.version} 完全一致，已合并回引用，无需发布新版",
            "baseline_id": base.id,
            "baseline_version": base.version,
            "synced_tasks": len(links),
            "deleted_copy": deleted,
        }

    # ---- 情形二：升版并发布
    steps = [step_out(s) for s in copy.steps]
    replaced_id: int | None = None
    if base is not None:
        replaced_id = base.id
        draft = revision(db, base)
        save_steps(db, draft, steps)
        new_base = publish(
            db,
            draft,
            published_by=actor,
            action="promote",
            note=(
                f"由任务 {task.task_no} 的本单副本进阶" if task is not None else f"由副本 #{copy.id} 进阶"
            ),
        )
    else:
        # 还没有任何基线（新品先投产、后来才定标准）
        new_base = create_draft(
            db, product, part, steps=steps, source="promote", created_by=actor
        )
        publish(db, new_base, published_by=actor, action="promote")

    # 留痕：新版从哪来。只记 version 说不清「这版是谁定的、依据是什么」。
    origin_note = (
        f"由任务 {task.task_no} 的本单副本 v{copy.version} 进阶"
        if task is not None
        else f"由副本 #{copy.id} 进阶"
    )
    new_base.source = "promote"
    new_base.remark = f"{new_base.remark} · {origin_note}" if new_base.remark else origin_note
    new_base.updated_at = dt.datetime.now()
    db.add(new_base)
    db.flush()

    synced = 0
    affected: list[dict] = []
    if replaced_id is not None:
        # 除了本任务，还有谁在旧版上：进阶之后它们会亮起「升版待确认」
        affected = [
            t
            for t in ref_tasks(db, replaced_id)
            if task is None or t["task_id"] != task.id
        ]
        # 所有引用旧版的任务一起切到新版 —— 这正是「引用」该有的行为
        synced = db.execute(
            update(TaskRoute)
            .where(TaskRoute.route_id == replaced_id, TaskRoute.status == "active")
            .values(route_id=new_base.id)
        ).rowcount or 0
    for tr in links:
        tr.route_id = new_base.id
        tr.origin = "baseline"
        # 是由本任务把副本进阶上去的，等于本任务已认可这一版
        tr.seen_route_id = new_base.id
        tr.seen_at = dt.datetime.now()
        tr.seen_by = actor
    db.flush()
    deleted = purge_task_copy(db, copy)

    return {
        "action": "promoted",
        "message": (
            f"已进阶为基线 v{new_base.version}，{int(synced) + len(links)} 个任务切到新版"
        ),
        "baseline_id": new_base.id,
        "baseline_version": new_base.version,
        "replaced_id": replaced_id,
        "synced_tasks": int(synced) + len(links),
        "affected_tasks": affected,
        "affected_count": len(affected),
        "deleted_copy": deleted,
    }


def publish_drafts(
    db: Session,
    *,
    product: str = "",
    part: str = "",
    limit: int = 500,
    published_by: str = "system",
    dry_run: bool = True,
) -> dict:
    """把「从来没有发布过」的草稿基线批量发布。

    为什么需要：只有已发布的路线才能被任务引用。首期 85 条基线全是草稿时，
    系统等于一个可用基准都没有 —— 但让用户逐条点 85 次「发布」也不现实。

    两条安全边界，宁可少发也不多发：

    * **该组合已有已发布版时跳过**。那条草稿属于「升版未发布」，
      批量发布它会顶掉当前生效基准，属于必须由人逐条确认的决策。
    * **没有工序的草稿跳过**。空路线发布出去就是个假基准，
      核对时会把所有工序都判成「多做的」。

    `dry_run=True` 只算不改，界面先预演再确认。
    """
    stmt = select(ProcessRoute).where(
        ProcessRoute.kind == "route", ProcessRoute.status == "draft"
    )
    if product:
        stmt = stmt.where(ProcessRoute.product == product)
    if part:
        stmt = stmt.where(ProcessRoute.part == part)
    drafts = db.scalars(
        stmt.order_by(ProcessRoute.product, ProcessRoute.part, ProcessRoute.version.desc())
    ).all()

    # 每个组合只处理版本号最大的一条草稿，更早的草稿是历史残留，不发布
    newest: dict[tuple[str, str], ProcessRoute] = {}
    for r in drafts:
        newest.setdefault((r.product, r.part), r)

    published, skipped = [], []
    for (prod, prt), r in newest.items():
        if len(published) >= max(1, limit):
            break
        if pick_baseline(db, prod, prt) is not None:
            skipped.append(
                {
                    "id": r.id,
                    "product": prod,
                    "part": prt,
                    "version": r.version,
                    "reason": "该组合已有已发布版本，需人工逐条升版发布",
                }
            )
            continue
        if not r.steps:
            skipped.append(
                {
                    "id": r.id,
                    "product": prod,
                    "part": prt,
                    "version": r.version,
                    "reason": "没有工序，空路线不能当基准",
                }
            )
            continue
        published.append(
            {
                "id": r.id,
                "product": prod,
                "part": prt,
                "version": r.version,
                "step_count": len(r.steps),
            }
        )
        if not dry_run:
            publish(db, r, published_by=published_by, action="batch")

    if not dry_run:
        db.flush()
    return {
        "dry_run": dry_run,
        "published_count": len(published),
        "skipped_count": len(skipped),
        "items": published,
        "skipped": skipped[:100],
    }


def copies_list(
    db: Session, *, q: str = "", task_id: int | None = None, limit: int = 300
) -> dict:
    """全部「本单副本」清单 —— 副本在基线清单里是看不到的，得有独立入口。

    每行都要回答三件事：属于哪个订单、从哪版基线分出来的、跟现行基线差多少。
    少了任何一个，人都没法判断「这份副本还该不该留着」。
    """
    stmt = select(ProcessRoute).where(ProcessRoute.kind == "task")
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                ProcessRoute.product.like(like),
                ProcessRoute.part.like(like),
                ProcessRoute.title.like(like),
            )
        )
    if task_id is not None:
        stmt = stmt.where(ProcessRoute.task_id == task_id)
    rows = db.scalars(stmt.order_by(ProcessRoute.updated_at.desc())).all()[:limit]

    items = []
    for r in rows:
        task = db.get(WorkTask, r.task_id) if r.task_id else None
        source = db.get(ProcessRoute, r.snapshot_of_id) if r.snapshot_of_id else None
        current = pick_baseline(db, r.product, r.part)
        diff = diff_routes(db, current or source, r)
        steps = list(r.steps)
        unit_sum = round(sum(float(s.unit_hours or 0) for s in steps), 4)
        setup_sum = round(sum(float(s.setup_hours or 0) for s in steps), 4)
        link = db.scalars(
            select(TaskRoute)
            .where(TaskRoute.route_id == r.id, TaskRoute.status == "active")
            .order_by(TaskRoute.id)
        ).first()
        items.append(
            {
                "id": r.id,
                "route_id": r.id,
                # 操作（编辑/进阶）走任务接口，必须带 task_route_id
                "task_route_id": link.id if link else None,
                "product": r.product,
                "part": r.part,
                "title": r.title,
                "version": r.version,
                "status": r.status,
                "step_count": len(steps),
                "unit_hours": unit_sum,
                "setup_hours": setup_sum,
                "per_unit_hours": unit_sum
                + (setup_sum / (r.batch_qty or 0) if (r.batch_qty or 0) > 0 else 0.0),
                "task_id": r.task_id,
                "task_no": task.task_no if task else "",
                "task_title": task.title if task else "",
                "task_status": task.status if task else "",
                # 来源基线（拆副本那一刻的版本）与现行基线（现在生效的版本）分开给：
                # 来源旧了不代表有问题，但一定要看得见
                "source_route_id": r.snapshot_of_id,
                "source_version": r.snapshot_version,
                "source_status": r.snapshot_status,
                "baseline_id": current.id if current else None,
                "baseline_version": current.version if current else None,
                "has_baseline": current is not None,
                "identical_to_baseline": bool(current is not None and diff["identical"]),
                "delta_total": diff["delta_total"],
                "diff": {
                    "only_in_baseline": diff["only_in_baseline"],
                    "only_in_task": diff["only_in_task"],
                    "hours_changed": diff["hours_changed"],
                    "order_changed": diff["order_changed"],
                    "identical": diff["identical"],
                    "delta_total": diff["delta_total"],
                },
                "ref_count": active_ref_count(db, r.id),
                "created_by": r.created_by,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
        )
    return {
        "total": len(items),
        "identical_count": sum(1 for x in items if x["identical_to_baseline"]),
        "items": items,
    }


def _step_key(s: ProcessRouteStep) -> str:
    """工序对齐键：有编码用编码，没有就用工序名。"""
    return (s.process_code or "").strip() or (s.process_name or "").strip()


def diff_routes(db: Session, baseline: ProcessRoute | None, task_route: ProcessRoute | None) -> dict:
    """基线 vs 任务工艺的工序级差异。

    三件事各自说清楚，不合并成一个「差异数」：
      - 任务少做的工序（基线有、任务没有）
      - 任务多做的工序（任务有、基线没有）
      - 都做但定额不同
    另外单独判断顺序：集合相同不等于工艺相同，先车后铣和先铣后车是两回事。
    """
    def steps_of(route):
        if route is None:
            return []
        return db.scalars(
            select(ProcessRouteStep)
            .where(ProcessRouteStep.route_id == route.id)
            .order_by(ProcessRouteStep.seq)
        ).all()

    b_steps, t_steps = steps_of(baseline), steps_of(task_route)
    b_map = {_step_key(s): s for s in b_steps}
    t_map = {_step_key(s): s for s in t_steps}

    only_baseline = [
        {"process": k, "unit_hours": float(b_map[k].unit_hours or 0)}
        for k in b_map
        if k not in t_map
    ]
    only_task = [
        {"process": k, "unit_hours": float(t_map[k].unit_hours or 0)}
        for k in t_map
        if k not in b_map
    ]
    changed = []
    for k in b_map:
        if k not in t_map:
            continue
        bv = float(b_map[k].unit_hours or 0)
        tv = float(t_map[k].unit_hours or 0)
        if abs(bv - tv) > 1e-6:
            changed.append(
                {
                    "process": k,
                    "baseline_hours": round(bv, 4),
                    "task_hours": round(tv, 4),
                    "delta": round(tv - bv, 4),
                }
            )

    b_order = [_step_key(s) for s in b_steps if _step_key(s) in t_map]
    t_order = [_step_key(s) for s in t_steps if _step_key(s) in b_map]
    order_changed = b_order != t_order

    b_total = round(sum(float(s.unit_hours or 0) for s in b_steps), 4)
    t_total = round(sum(float(s.unit_hours or 0) for s in t_steps), 4)
    return {
        "baseline": {
            "id": baseline.id,
            "version": baseline.version,
            "status": baseline.status,
        }
        if baseline
        else None,
        "task_route": {
            "id": task_route.id,
            "version": task_route.version,
            "status": task_route.status,
            "snapshot_version": task_route.snapshot_version,
            "snapshot_status": task_route.snapshot_status,
        }
        if task_route
        else None,
        "only_in_baseline": only_baseline,
        "only_in_task": only_task,
        "hours_changed": changed,
        "order_changed": order_changed,
        "baseline_order": b_order,
        "task_order": t_order,
        "baseline_total": b_total,
        "task_total": t_total,
        "delta_total": round(t_total - b_total, 4),
        "identical": not (only_baseline or only_task or changed or order_changed),
    }
