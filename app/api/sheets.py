"""单据接口：查询、编辑、确认、驳回、重识别。"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..domain.sys_roles import DUTY_REPORT
from .auth import require_perm
from ..domain.rules import (
    check_route,
    check_std_hours,
    compute_total_hours,
    has_blocking,
    period_bounds,
    summarize_flags,
    validate_item,
)
from ..models import AuditLog, Batch, Image, Period, Sheet, TaskRoute, WorkItem, Worker, WorkTask
from ..ocr.normalize import match_entry, resolve_process
from ..schemas import (
    BatchVerifyRequest,
    RejectRequest,
    SheetBatchTaskMove,
    SheetDimsUpdate,
    SheetTaskMove,
    SheetUpdate,
    VerifyRequest,
)
from ..security import current_operator
from ..services.dicts import dict_bundle
from ..services import learn
from ..services import print_form
from ..services import tasks as task_service
from ..services.keeps import manual_keeps
from ..services.pipeline import enqueue
from .serializers import item_to_dict, sheet_to_dict

router = APIRouter(prefix="/api", tags=["sheets"])


def _load_sheet(db: Session, sheet_id: int) -> Sheet:
    sheet = db.scalar(
        select(Sheet)
        .where(Sheet.id == sheet_id)
        .options(selectinload(Sheet.items).selectinload(WorkItem.worker), selectinload(Sheet.image))
    )
    if not sheet:
        raise HTTPException(404, "单据不存在")
    return sheet


def _period_ym(db: Session, sheet: Sheet) -> str:
    p = db.get(Period, sheet.period_id)
    return p.ym if p else ""


# ---------------------------------------------------------------- 留痕中文
# 「修改留痕」面向车间和财务，不能把内部键名、状态码、外键 id 直接摊在界面上。
# 这里把字段与取值统一翻成中文，库里的原始记录保持不变（改文案不影响历史数据）。
_FIELD_LABELS = {
    "status": "单据状态",
    "note": "单据备注",
    "header": "表头信息",
    "header:discard": "放弃表头修改",
    "item:created": "新增明细行",
    "item:deleted": "删除明细行",
    "item:overwritten": "重新识别覆盖",
    "item:discard": "放弃人工修改",
    "item:product_name": "产品名称",
    "item:batch_no": "批次号",
    "item:part_name": "件名",
    "item:material": "材料",
    "item:process": "工序",
    "item:inspector": "检验员",
    "item:handover": "接交人",
    "item:worker_id": "工作者",
    "item:worker_name": "工作者",
    "item:worker_raw": "工作者（原文）",
    "item:remark": "明细备注",
    "item:qualified_qty": "合格件数",
    "item:rework_qty": "返修件数",
    "item:scrap_qty": "废品件数",
    "item:unit_hours": "单件工时",
    "item:total_hours": "合计工时",
    "item:finish_date": "完工日期",
}

_STATUS_LABELS = {"recognized": "待核对", "verified": "已确认", "rejected": "已作废"}

# 表头 JSON 的键 → 中文（与核对界面上的表头字段一一对应，用词与标准表印刷体一致）
_HEADER_LABELS = {
    "product_name": "产品名称",
    "product_code": "产品代号",
    "batch_no": "产品批号",
    "part_no": "件号",
    "part_name": "件名",
    "material": "材料",
    "category": "类别",
    "machine_count": "投产台数",
    "part_count": "加工件数",
    "dispatcher": "计调员",
    "picker": "材料员",
    "worker_hint": "右上角姓名",
    "sheet_no": "单号",
    "workshop": "车间",
}

# 这些字段是数量/工时，去掉无意义的 .0（1.0 → 1，比「1.0」更像人写的）
_QTY_FIELDS = {
    "item:qualified_qty",
    "item:rework_qty",
    "item:scrap_qty",
    "item:unit_hours",
    "item:total_hours",
}
_EMPTY_TEXT = {"", "none", "null"}


def _field_label(field: str) -> str:
    if not field:
        return ""
    return _FIELD_LABELS.get(field) or field


def _fmt_scalar(db: Session, field: str, value) -> str:
    """把单个值翻成界面可读的中文；空值统一显示为空串（而不是 None）。"""
    text = "" if value is None else str(value)
    if text.strip().lower() in _EMPTY_TEXT:
        return ""
    if field == "status":
        return _STATUS_LABELS.get(text, text)
    if field == "item:worker_id":
        # 存的是人员 id，界面上要看到「马永刚」而不是「13」
        if db is None:
            return text
        try:
            wid = int(float(text))
        except (TypeError, ValueError):
            return text
        w = db.get(Worker, wid)
        return w.name if w else f"（已删除人员 #{wid}）"
    if field in _QTY_FIELDS:
        try:
            num = float(text)
        except (TypeError, ValueError):
            return text
        return str(int(num)) if num == int(num) else f"{num:g}"
    return text


def _fmt_header(old_raw, new_raw) -> tuple[str, str]:
    """表头存的是整段 JSON，逐行对比后只列出真正改动的那几个字段。"""
    def _load(raw) -> dict:
        if isinstance(raw, dict):
            return raw
        try:
            data = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    old, new = _load(old_raw), _load(new_raw)
    keys = list(dict.fromkeys([*old.keys(), *new.keys()]))
    # 表头里还混着识别阶段写入的内部键（category / form_title / company…），
    # 界面只关心人填过的那几项，其余一律不展示，免得留痕变成一串英文键名。
    changed = [
        k
        for k in keys
        if k in _HEADER_LABELS and str(old.get(k) or "") != str(new.get(k) or "")
    ]
    if not changed:
        # 改的都是内部键时，至少别让这条留痕变成空白
        if any(str(old.get(k) or "") != str(new.get(k) or "") for k in keys):
            return str(old_raw or ""), str(new_raw or "")
        return "", ""
    old_parts = [f"{_HEADER_LABELS[k]}：{old.get(k) or '空'}" for k in changed]
    new_parts = [f"{_HEADER_LABELS[k]}：{new.get(k) or '空'}" for k in changed]
    return "；".join(old_parts), "；".join(new_parts)


def _fmt_item_json(raw, keep_raw: bool = True) -> str:
    """新增/删除明细行存的是整行 JSON，摘出关键几项，别把一串英文键甩给用户。

    keep_raw=False：解析不出 JSON 时返回空串，交给调用方用旧格式兜底。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if text.lower() in _EMPTY_TEXT:
            return ""
        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            return text if keep_raw else ""
    if not isinstance(raw, dict):
        return str(raw or "") if keep_raw else ""

    order = [
        "part_name", "process", "product_name", "worker_name", "worker_raw", "qualified_qty",
        "rework_qty", "scrap_qty", "unit_hours", "total_hours", "finish_date", "material", "remark",
    ]
    parts = []
    has_name = bool(str(raw.get("worker_name") or "").strip())
    for key in order:
        if key not in raw:
            continue
        # 已经能对上人名时，手写原文（常带 OCR 噪声）就不必再占一行
        if key == "worker_raw" and has_name:
            continue
        val = raw.get(key)
        if val is None or str(val).strip().lower() in _EMPTY_TEXT:
            continue
        label = _FIELD_LABELS.get(f"item:{key}", key)
        text = _fmt_scalar(None, f"item:{key}", val)
        parts.append(f"{label}：{text}")
    return "；".join(parts)


def _fmt_deleted_legacy(raw) -> str:
    """旧版删除记录存的是「件名/工序/合计工时」纯文本。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    parts = text.split("/")
    if len(parts) != 3:
        return text
    part, process, hours = (p.strip() for p in parts)
    items = []
    if part:
        items.append(f"件名：{part}")
    if process:
        items.append(f"工序：{process}")
    if hours and hours.lower() not in _EMPTY_TEXT:
        items.append(f"合计工时：{_fmt_scalar(None, 'item:unit_hours', hours)}")
    return "；".join(items)


def _target_label(a: AuditLog, seq_of: dict[int, int | None]) -> str:
    """定位这次改动落在第几行明细。行被删掉时只能从当年记下的 JSON 里取序号。"""
    if a.entity != "work_item":
        return ""
    seq = seq_of.get(a.entity_id)
    if seq is None:
        for raw in (a.new_value, a.old_value):
            try:
                data = json.loads(raw or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(data, dict) and isinstance(data.get("seq"), (int, float)):
                seq = int(data["seq"])
                break
    return f"第{seq}行" if seq else ""


def _audit(
    db: Session,
    entity_id: int,
    field: str,
    old,
    new,
    user: str,
    sheet_id: int | None = None,
    seq: int | None = None,
) -> None:
    if str(old) == str(new):
        return
    is_item = field.startswith("item:")
    db.add(
        AuditLog(
            entity="work_item" if is_item else "sheet",
            entity_id=entity_id,
            sheet_id=sheet_id if is_item else entity_id,
            # 明细行号也记一份：重新识别会整批重建明细（id 全变），
            # 只靠 entity_id 会让「人改过哪些字段」在第二次重新识别时丢失
            seq=seq if is_item else None,
            field=field,
            old_value=str(old)[:500],
            new_value=str(new)[:500],
            user=user,
        )
    )


def _learn_signature(db: Session, it: WorkItem, old_value: str, new_value: str, user: str) -> None:
    """人工把检验员改成名册里的人时，把签名格原文记成该人的别名。

    优先级：识别时存下的原文 > 被改掉的那个旧值（老单据没存原文，旧值就是原文）。
    只记一次，写不进（原文被别人占用/太短）就静静放过——别名是"顺手挣到的"，不是必须。
    """
    raw = (getattr(it, "inspector_raw", "") or old_value or "").strip()
    if not raw or raw == (new_value or "").strip():
        return
    learned = learn.remember(db, raw, new_value, user)
    if learned:
        _audit(db, it.id, "item:inspector_alias", "", learned, user, it.sheet_id, it.seq)


def _apply_process(it: WorkItem, raw: str, bundle: dict) -> None:
    """人工改/新增工序后重算字典归属。

    显示值保留人所选（或单据所写）的写法——「包板折弯」是「折弯」在字典里登记的
    别名，就该显示「包板折弯」；code 与标准名同步更新，否则改了工序 code 还是旧的，
    汇总和标准工时都会串到别的工序上。
    """
    code, std, _score, disp, exact = resolve_process(raw, bundle["processes"])
    it.process = (disp if (exact and disp) else (raw or disp)) or ""
    it.process_std = std or ""
    it.process_code = code or ""


def _revalidate(db: Session, sheet: Sheet) -> None:
    ym = _period_ym(db, sheet)
    bundle = dict_bundle(db)
    try:
        columns = json.loads(sheet.layout_json or "{}").get("columns") or []
    except (TypeError, ValueError):
        columns = []
    present = {str(c.get("key")) for c in columns}
    has_date_col = ("finish_date" in present) if columns else True
    for it in sheet.items:
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
            # 用当前名册实时判定是否入档：在系统管理补完字典后，重新校验即可清掉告警
            "product_matched": _in_dict(it.product_name, bundle["products"]),
            "material_matched": _in_dict(it.material, bundle["materials"]),
            "inspector_matched": _in_dict(it.inspector, bundle["inspectors"]),
            "handover_matched": _in_dict(it.handover, bundle["receivers"]),
            "conf": json.loads(it.conf_json or "{}"),
        }
        flags = validate_item(payload, ym, has_date_col=has_date_col)
        flags.extend(check_std_hours(payload, bundle.get("std_hours") or {}))
        flags.extend(check_route(payload, bundle.get("routes") or {}))
        it.flags_json = json.dumps(flags, ensure_ascii=False)
        it.total_hours = compute_total_hours(it.qualified_qty, it.unit_hours, it.rework_qty, it.scrap_qty)
    sheet.total_hours = round(sum(i.total_hours for i in sheet.items), 4)
    sheet.flag_count = summarize_flags(
        [{"flags": json.loads(i.flags_json or "[]")} for i in sheet.items]
    )["total"]


def _in_dict(raw: str, entries: list) -> bool:
    """名册为空视为「该字段未启用」，不做告警；否则要求能收敛到字典内的标准名。"""
    if not raw or not entries:
        return True
    _code, name, score = match_entry(raw, entries)
    return bool(name) and score >= 0.55


# ------------------------------------------------------------------ 查询
@router.get("/sheets")
def list_sheets(
    period_id: int | None = None,
    ym: str | None = None,
    status: str | None = None,
    batch_id: int | None = None,
    keyword: str = "",
    only_flagged: bool = False,
    page: int = 1,
    size: int = 20,
    db: Session = Depends(get_db),
):
    stmt = select(Sheet).options(selectinload(Sheet.image), selectinload(Sheet.items))
    if ym:
        p = db.scalar(select(Period).where(Period.ym == ym))
        if not p:
            return {"total": 0, "items": [], "page": page, "size": size}
        stmt = stmt.where(Sheet.period_id == p.id)
    elif period_id:
        stmt = stmt.where(Sheet.period_id == period_id)
    if status:
        stmt = stmt.where(Sheet.status.in_(status.split(",")))
    if batch_id:
        stmt = stmt.join(Image, Sheet.image_id == Image.id).where(Image.batch_id == batch_id)
    if only_flagged:
        stmt = stmt.where(Sheet.flag_count > 0)
    if keyword:
        like = f"%{keyword}%"
        sub = select(WorkItem.sheet_id).where(
            or_(
                WorkItem.part_name.like(like),
                WorkItem.product_name.like(like),
                WorkItem.worker_raw.like(like),
                WorkItem.process.like(like),
            )
        )
        stmt = stmt.where(Sheet.id.in_(sub))

    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Sheet.id.desc()).offset((page - 1) * size).limit(size)).all()

    # 任务号：台账上要能一眼看出这张单对应哪个生产任务。
    # 一次批量查，别在循环里逐条查（20 条就是 20 次往返）。
    tasks: dict[int, dict] = {}
    task_ids = {s.task_id for s in rows if s.task_id}
    if task_ids:
        for t in db.scalars(select(WorkTask).where(WorkTask.id.in_(task_ids))).all():
            tasks[t.id] = {"task_id": t.id, "task_no": t.task_no, "task_title": t.title}

    items = []
    # 升版提醒：台账上也要能看出「这张单归属的订单还没认新版工艺」，
    # 否则在台账里翻到一张已确认的单，完全看不出它的定额基准刚被人改过。
    notice_by_task: dict[int, int] = {}
    if task_ids:
        trs = db.scalars(
            select(TaskRoute).where(
                TaskRoute.task_id.in_(task_ids), TaskRoute.status == "active"
            )
        ).all()
        states = task_service.ref_states(db, list(trs))
        for tr in trs:
            if tr.id in states:
                notice_by_task[tr.task_id] = notice_by_task.get(tr.task_id, 0) + 1

    for s in rows:
        d = sheet_to_dict(s, with_items=False)
        # 注意：键是任务的 id，取值要用单据上的 task_id，不是单据自己的 id
        d.update(tasks.get(s.task_id) or {"task_id": None, "task_no": "", "task_title": ""})
        d["task_notice_count"] = notice_by_task.get(s.task_id, 0)
        items.append(d)
    return {"total": total, "page": page, "size": size, "items": items}


@router.get("/sheets/{sheet_id}/items/{item_id}/crop")
def item_crop(
    sheet_id: int,
    item_id: int,
    field: str = "inspector",
    pad: int = 10,
    zoom: int = 3,
    db: Session = Depends(get_db),
):
    """裁出某个明细格子的图，给核对页放大看。

    检验员那列是手写签名，模型认不准是常态（「杜文杰」读成「定生」）。与其让人对着
    整张照片找那一小格，不如直接把签名裁出来放大三倍——人眼一秒就认出来了。
    """
    sheet = _load_sheet(db, sheet_id)
    it = next((x for x in sheet.items if x.id == item_id), None)
    if not it:
        raise HTTPException(404, "明细行不存在")
    try:
        bbox = json.loads(it.bbox_json or "{}").get(field)
    except (TypeError, ValueError):
        bbox = None
    if not bbox:
        raise HTTPException(404, "该字段没有位置信息，重新识别一次即可生成")
    img = sheet.image
    src = (img.processed_path if img and img.processed_path else "") or (img.path if img else "")
    if not src:
        raise HTTPException(404, "原始图片不存在")

    from io import BytesIO

    from PIL import Image as PILImage

    pad = max(0, min(int(pad), 60))
    zoom = max(1, min(int(zoom), 6))
    with PILImage.open(src) as im:
        w, h = im.size
        x0, y0, x1, y1 = (int(float(v)) for v in bbox)
        box = (max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad))
        if box[2] - box[0] < 4 or box[3] - box[1] < 4:
            raise HTTPException(404, "格子太小，无法裁图")
        crop = im.convert("L").crop(box)
        if zoom > 1:
            crop = crop.resize((crop.width * zoom, crop.height * zoom), PILImage.LANCZOS)
        buf = BytesIO()
        crop.save(buf, format="PNG")
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.get("/sheets/{sheet_id}")
def get_sheet(sheet_id: int, db: Session = Depends(get_db)):
    sheet = _load_sheet(db, sheet_id)
    data = sheet_to_dict(sheet, with_items=True)
    data["ym"] = _period_ym(db, sheet)
    period = db.get(Period, sheet.period_id)
    data["period_status"] = period.status if period else "open"
    data["task"] = _task_brief(db, sheet)
    return data


def _task_brief(db: Session, sheet: Sheet) -> dict | None:
    """单据归属的任务，以及该任务有没有「基线升版待确认」。

    单据复核的人看的是这道工序的定额对不对。基准刚升过版、任务还没认，
    这张单按哪一版核的就说不清了 —— 必须在这里就亮出来，而不是等他确认完
    再去任务页发现。
    """
    if not sheet.task_id:
        return None
    task = db.get(WorkTask, sheet.task_id)
    if task is None:
        return None
    alerts = task_service.version_alerts(db, task_id=task.id)
    return {
        "id": task.id,
        "task_no": task.task_no,
        "title": task.title,
        "status": task.status,
        "notice_count": len(alerts),
        "notices": alerts,
    }


@router.get("/sheets/{sheet_id}/print.pdf")
def print_sheet(
    sheet_id: int,
    dpi: int = Query(300, ge=120, le=400),
    download: bool = False,
    db: Session = Depends(get_db),
):
    """按原版式打印这张加工路线单。

    版式来自 app/render/route_form.py —— 与识别用的空白模板是同一份毫米坐标，
    所以打出来就是车间那张表，只是格子里填了字（单件仍写成 2ʰ / 30'）。
    """
    sheet = _load_sheet(db, sheet_id)
    pdf = print_form.render_pdf(sheet, dpi=dpi)
    no = sheet.items[0].batch_no if sheet.items and sheet.items[0].batch_no else ""
    ym = _period_ym(db, sheet) or ""
    name = f"加工路线单_{ym}_{no or sheet.id}.pdf".replace(" ", "")
    disp = "attachment" if download else "inline"
    quoted = urllib.parse.quote(name)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"{disp}; filename=\"route-sheet-{sheet.id}.pdf\"; filename*=UTF-8''{quoted}",
            "Cache-Control": "no-store",
        },
    )


@router.get("/sheets/{sheet_id}/audit")
def sheet_audit(sheet_id: int, db: Session = Depends(get_db)):
    sheet = _load_sheet(db, sheet_id)
    item_ids = [i.id for i in sheet.items]
    rows = db.scalars(
        select(AuditLog)
        .where(
            or_(
                # 新记录直接挂单据 id：明细行被「重新识别」整批重建后仍能查到
                AuditLog.sheet_id == sheet_id,
                # 兼容历史记录（没有 sheet_id），按当前明细行 id 关联
                (AuditLog.entity == "sheet") & (AuditLog.entity_id == sheet_id),
                (AuditLog.entity == "work_item") & (AuditLog.entity_id.in_(item_ids or [0])),
            )
        )
        .order_by(AuditLog.ts.desc())
        .limit(200)
    ).all()
    # 明细行改动要能定位到具体哪一行（序号），否则多行单据的留痕分不清对象
    seq_of = {it.id: it.seq for it in sheet.items}
    out = []
    for a in rows:
        field = a.field or ""
        old_text, new_text = a.old_value, a.new_value
        if field == "header":
            old_text, new_text = _fmt_header(a.old_value, a.new_value)
        elif field == "item:created":
            old_text, new_text = "", _fmt_item_json(a.new_value)
        elif field == "item:deleted":
            # 老记录是「件名/工序/工时」三段，新记录是整行 JSON，两种都还原成中文摘要
            old_text = _fmt_item_json(a.old_value, keep_raw=False) or _fmt_deleted_legacy(a.old_value)
            new_text = ""
        elif field == "item:overwritten":
            # 重新识别把人工行整行覆盖掉，这里还原被丢掉的那一行
            old_text = _fmt_item_json(a.old_value, keep_raw=False)
            new_text = "已按新识别结果重建"
        else:
            old_text = _fmt_scalar(db, field, a.old_value)
            new_text = _fmt_scalar(db, field, a.new_value)
        out.append(
            {
                "id": a.id,
                "field": field,
                "field_label": _field_label(field),
                "target": _target_label(a, seq_of),
                "old": old_text,
                "new": new_text,
                "user": a.user,
                "ts": a.ts.strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    return out


# ------------------------------------------------------------------ 编辑
@router.patch("/sheets/{sheet_id}")
def update_sheet(sheet_id: int, data: SheetUpdate, db: Session = Depends(get_db)):
    require_perm(db, DUTY_REPORT)
    sheet = _load_sheet(db, sheet_id)
    period = db.get(Period, sheet.period_id)
    if period and period.status == "locked":
        raise HTTPException(400, "核算期已锁定，只读")
    if sheet.status == "verified":
        raise HTTPException(400, "单据已确认，请先撤销确认再修改；只补产品/件名/工序可走「补录维度」")

    user = current_operator(data.user or "operator")

    if data.header is not None:
        old_header = json.loads(sheet.header_json or "{}")
        _audit(db, sheet.id, "header", json.dumps(old_header, ensure_ascii=False), json.dumps(data.header, ensure_ascii=False), user)
        sheet.header_json = json.dumps(data.header, ensure_ascii=False)
    if data.note is not None:
        _audit(db, sheet.id, "note", sheet.note, data.note, user)
        sheet.note = data.note

    if data.items is not None:
        bundle = dict_bundle(db)
        existing = {it.id: it for it in sheet.items}
        keep_ids: set[int] = set()
        for idx, payload in enumerate(data.items, start=1):
            patch = payload.model_dump(exclude_none=True)
            if payload.id and payload.id in existing:
                it = existing[payload.id]
                for field in (
                    "product_name",
                    "batch_no",
                    "part_name",
                    "material",
                    "process",
                    "inspector",
                    "handover",
                    "worker_raw",
                    "remark",
                ):
                    if field in patch and getattr(it, field) != patch[field]:
                        _audit(db, it.id, f"item:{field}", getattr(it, field), patch[field], user, sheet.id, it.seq)
                        old_value = getattr(it, field)
                        setattr(it, field, patch[field])
                        if field == "inspector":
                            _learn_signature(db, it, old_value, patch[field], user)
                        elif field == "process":
                            _apply_process(it, patch[field], bundle)
                for field in ("qualified_qty", "rework_qty", "scrap_qty", "unit_hours"):
                    if field in patch and getattr(it, field) != patch[field]:
                        _audit(db, it.id, f"item:{field}", getattr(it, field), patch[field], user, sheet.id, it.seq)
                        setattr(it, field, patch[field])
                        if field in ("qualified_qty", "unit_hours"):
                            # 数值被人工修正后，原单手写合计不再作为比对基准，
                            # 否则「合计与手写不符」会永远挂着消不掉。
                            it.declared_total = None
                if "finish_date" in patch and it.finish_date != patch["finish_date"]:
                    _audit(db, it.id, "item:finish_date", it.finish_date, patch["finish_date"], user, sheet.id, it.seq)
                    it.finish_date = patch["finish_date"]
                if "worker_id" in patch and it.worker_id != patch["worker_id"]:
                    _audit(db, it.id, "item:worker_id", it.worker_id, patch["worker_id"], user, sheet.id, it.seq)
                    it.worker_id = patch["worker_id"]
                    w = db.get(Worker, patch["worker_id"]) if patch["worker_id"] else None
                    if w and it.worker_raw and it.worker_raw != w.name:
                        # 手写签名照样认不准，人工选定后同样记别名（与检验员同一套机制）
                        learned = learn.remember(db, it.worker_raw, w.name, user)
                        if learned:
                            _audit(db, it.id, "item:worker_alias", "", learned, user, sheet.id, it.seq)
                    it.worker_raw = w.name if w else it.worker_raw
                it.row_state = "edited"
                it.seq = patch.get("seq", idx)
                keep_ids.add(it.id)
            else:
                it = WorkItem(
                    sheet_id=sheet.id,
                    seq=patch.get("seq", idx),
                    product_name=patch.get("product_name", ""),
                    batch_no=patch.get("batch_no", ""),
                    part_name=patch.get("part_name", ""),
                    material=patch.get("material", ""),
                    process=patch.get("process", ""),
                    qualified_qty=patch.get("qualified_qty"),
                    rework_qty=patch.get("rework_qty"),
                    scrap_qty=patch.get("scrap_qty"),
                    unit_hours=patch.get("unit_hours"),
                    finish_date=patch.get("finish_date"),
                    inspector=patch.get("inspector", ""),
                    handover=patch.get("handover", ""),
                    worker_id=patch.get("worker_id"),
                    worker_raw=patch.get("worker_raw", ""),
                    row_state="manual",
                )
                db.add(it)
                db.flush()
                _apply_process(it, patch.get("process", ""), bundle)
                keep_ids.add(it.id)
                _audit(db, it.id, "item:created", "", json.dumps({"seq": it.seq, **patch}, ensure_ascii=False, default=str), user, sheet.id)

        for it_id, it in existing.items():
            if it_id not in keep_ids:
                # 存整行快照（含序号），删掉的行走不了姓名/工序回查，留痕里还要能看清删了什么
                snapshot = {
                    "seq": it.seq,
                    "part_name": it.part_name,
                    "process": it.process,
                    "product_name": it.product_name,
                    "worker_raw": it.worker_raw,
                    "worker_name": (it.worker.name if it.worker else "") or "",
                    "qualified_qty": it.qualified_qty,
                    "rework_qty": it.rework_qty,
                    "scrap_qty": it.scrap_qty,
                    "unit_hours": it.unit_hours,
                    "finish_date": it.finish_date,
                    "material": it.material,
                    "remark": it.remark,
                }
                _audit(db, it_id, "item:deleted", json.dumps(snapshot, ensure_ascii=False, default=str), "", user, sheet.id)
                db.delete(it)
        db.flush()
        db.refresh(sheet)

    _revalidate(db, sheet)
    db.commit()
    return get_sheet(sheet_id, db)


# ------------------------------------------------------------------ 改挂任务
# 归集是自动的（同产品+同批次+同期间），自动就会错。没有这个入口时，
# 「这张单归错了订单」只能改库 —— 归属一旦能被随手改库，账就不可信了。
@router.post("/sheets/{sheet_id}/task")
def move_sheet_task(sheet_id: int, data: SheetTaskMove, db: Session = Depends(get_db)):
    require_perm(db, DUTY_REPORT)
    sheet = _load_sheet(db, sheet_id)
    user = current_operator(data.user or "operator")
    before = db.get(WorkTask, sheet.task_id) if sheet.task_id else None
    try:
        out = task_service.move_sheet_task(
            db,
            sheet,
            data.task_id,
            user=user,
            attach_routes=data.attach_routes,
            force=data.force,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    after = db.get(WorkTask, sheet.task_id) if sheet.task_id else None
    # 归属变更必须留痕：谁把哪张单从哪个订单挪到哪个订单，是账务动作
    if not out.get("unchanged"):
        _audit(
            db,
            sheet.id,
            "task_id",
            before.task_no if before else "",
            after.task_no if after else "",
            user,
            sheet.id,
        )
    db.commit()
    return out


@router.post("/sheets/batch-task")
def batch_move_sheet_task(data: SheetBatchTaskMove, db: Session = Depends(get_db)):
    require_perm(db, DUTY_REPORT)
    if not data.sheet_ids:
        raise HTTPException(400, "请先选择要改挂的单据")
    user = current_operator(data.user or "operator")
    before = {}
    for sid in data.sheet_ids:
        s = db.get(Sheet, sid)
        if s is not None and s.task_id:
            t = db.get(WorkTask, s.task_id)
            before[sid] = t.task_no if t else ""
    out = task_service.move_sheets_task(
        db,
        data.sheet_ids,
        data.task_id,
        user=user,
        attach_routes=data.attach_routes,
        force=data.force,
    )
    for r in out["results"]:
        after = (r.get("to_task") or {}).get("task_no", "")
        _audit(db, r["sheet_id"], "task_id", before.get(r["sheet_id"], ""), after, user, r["sheet_id"])
    db.commit()
    return out


# ------------------------------------------------------------------ 确认
def _cross_period_rows(db: Session, sheet: Sheet) -> list[dict]:
    """完工日期不在本核算期的明细行。

    这类单大多是「上个月干完、这个月才交单」，也有拍错月份混进来的。
    两者的处理完全不同，机器分不出来，所以只能把行摆出来让人点头。
    """
    period = db.get(Period, sheet.period_id)
    if period is None or not period.ym:
        return []
    start, end = period_bounds(period.ym)
    out = []
    for it in sheet.items:
        if it.finish_date and not (start <= it.finish_date <= end):
            out.append(
                {
                    "item_id": it.id,
                    "seq": it.seq,
                    "date": str(it.finish_date),
                    "worker": (it.worker.name if it.worker else "") or it.worker_raw or "",
                    "hours": round(float(it.total_hours or 0), 4),
                }
            )
    return out


@router.post("/sheets/{sheet_id}/verify")
def verify_sheet(sheet_id: int, data: VerifyRequest, db: Session = Depends(get_db)):
    require_perm(db, DUTY_REPORT)
    sheet = _load_sheet(db, sheet_id)
    period = db.get(Period, sheet.period_id)
    if period and period.status == "locked":
        raise HTTPException(400, "核算期已锁定")

    user = current_operator(data.user or "operator")

    _revalidate(db, sheet)
    if not sheet.items:
        raise HTTPException(400, "无明细行，不能确认")

    blocking = []
    for it in sheet.items:
        flags = json.loads(it.flags_json or "[]")
        if has_blocking(flags):
            blocking.append({"item_id": it.id, "flags": [f for f in flags if f.get("level") == "block"]})
    if blocking and not data.force:
        raise HTTPException(400, f"存在 {len(blocking)} 行阻断级告警，需处理或强制确认")

    # 跨期：不拦死，但必须显式点头。默认静默通过的话，一张 7 月的单
    # 就悄悄进了 9 月工资表，两个月的数同时错，且事后无从发现。
    cross = _cross_period_rows(db, sheet)
    if cross and not data.confirm_out_of_period:
        raise HTTPException(
            409,
            detail={
                "code": "OUT_OF_PERIOD",
                "message": (
                    f"有 {len(cross)} 行完工日期不在本期"
                    f"（{ '、'.join(str(x['date']) for x in cross[:3]) }），"
                    f"确认后这些工时会记进本期账"
                ),
                "items": cross,
            },
        )

    old_status = sheet.status
    sheet.status = "verified"
    sheet.verify_user = user
    sheet.verify_at = dt.datetime.now()
    sheet.reject_reason = ""
    db.add(AuditLog(entity="sheet", entity_id=sheet.id, sheet_id=sheet.id, field="status", old_value=old_status, new_value="verified", user=user))
    # 确认即归集：这张单归到「同产品同批次同期间」的订单任务上（没有才新建）。
    # 只挂路线引用，不复制工艺 —— 复用是默认，需要偏离标准时在任务里 fork。
    before_task_id = sheet.task_id
    task, warnings = task_service.sync_sheet_task(db, sheet, created_by=user or "system")
    # 自动归集也必须留痕：事后问「这张单为什么算在这个订单上」，
    # 只有库里的 task_id 而没有操作人和时间，是查不清的。
    if task is not None and sheet.task_id != before_task_id:
        db.add(
            AuditLog(
                entity="sheet",
                entity_id=sheet.id,
                sheet_id=sheet.id,
                field="task_id",
                old_value=str(before_task_id or ""),
                new_value=str(task.id),
                user=user or "system",
            )
        )
    notices = task_service.version_alerts(db, task_id=task.id) if task else []

    # 归集维度缺失不会让确认失败（工资按人×工时仍算得出来），
    # 但汇总里会多出一列「未填」产品/工序，成本与工艺分析从此查不到这几行。
    # 确认这一刻是唯一能拦住的机会，所以放进回执里让人当场看见。
    lack_rows = []
    for it in sheet.items:
        lack = []
        if not (it.product_name or "").strip():
            lack.append("产品")
        if not (it.part_name or "").strip():
            lack.append("件名")
        if not ((it.process_std or it.process or "").strip()):
            lack.append("工序")
        if lack:
            lack_rows.append(f"第 {it.seq} 行缺{ '、'.join(lack) }")
    if lack_rows:
        warnings.append(
            f"有 {len(lack_rows)} 行归集维度不全（{ '；'.join(lack_rows[:5]) }），"
            f"本单工时会计入「未填」列，可在单据详情「补录维度」补齐"
        )

    db.commit()
    return {
        "ok": True,
        "sheet_id": sheet.id,
        "total_hours": sheet.total_hours,
        "forced": bool(blocking),
        "out_of_period": cross,
        "task_id": task.id if task else None,
        "task_no": task.task_no if task else None,
        "task_route_count": task_service.task_detail(db, task)["summary"]["route_count"] if task else 0,
        # 确认成功，但归属订单的工艺基准升过版还没人认 —— 复核人必须知道
        "notices": notices,
        "notice_count": len(notices),
        # 件名没有已发布工艺时不会静默放过：确认成功，但必须让人看见缺口
        "warnings": warnings,
    }


@router.post("/sheets/batch-verify")
def batch_verify(data: BatchVerifyRequest, db: Session = Depends(get_db)):
    require_perm(db, DUTY_REPORT)
    ok, skipped, warn = [], [], []
    user = current_operator(data.user or "operator")
    for sid in data.sheet_ids:
        try:
            sheet = _load_sheet(db, sid)
            # 批量入口也必须查锁定：单张确认查了、批量没查，等于给锁定期开了后门
            period = db.get(Period, sheet.period_id)
            if period and period.status == "locked":
                skipped.append({"sheet_id": sid, "reason": "核算期已锁定"})
                continue
            _revalidate(db, sheet)
            blocking = [
                it for it in sheet.items if has_blocking(json.loads(it.flags_json or "[]"))
            ]
            has_low_conf = any(
                json.loads(it.flags_json or "[]") and any(f["code"] == "LOW_CONF" for f in json.loads(it.flags_json))
                for it in sheet.items
            )
            if blocking or has_low_conf or not sheet.items:
                skipped.append({"sheet_id": sid, "reason": "存在阻断告警/低置信度/无明细"})
                continue
            # 批量是最容易「一路回车」的入口，跨期单据一律挑出来单张处理
            cross = _cross_period_rows(db, sheet)
            if cross and not data.confirm_out_of_period:
                skipped.append(
                    {
                        "sheet_id": sid,
                        "reason": f"有 {len(cross)} 行完工日期不在本期，需单张确认（勾选允许跨期）",
                    }
                )
                continue
            prev_status = sheet.status
            sheet.status = "verified"
            sheet.verify_user = user
            sheet.verify_at = dt.datetime.now()
            # 单张确认有留痕、批量确认没有 —— 事后查「谁批的」会查不到，这里补齐
            db.add(
                AuditLog(
                    entity="sheet",
                    entity_id=sheet.id,
                    sheet_id=sheet.id,
                    field="status",
                    old_value=prev_status,
                    new_value="verified",
                    user=user,
                )
            )
            before_task_id = sheet.task_id
            task, warns = task_service.sync_sheet_task(db, sheet, created_by=user or "system")
            if task is not None and sheet.task_id != before_task_id:
                db.add(
                    AuditLog(
                        entity="sheet",
                        entity_id=sheet.id,
                        sheet_id=sheet.id,
                        field="task_id",
                        old_value=str(before_task_id or ""),
                        new_value=str(task.id),
                        user=user or "system",
                    )
                )
            ok.append(sid)
            if warns:
                warn.append({"sheet_id": sid, "warnings": warns})
        except HTTPException as exc:
            skipped.append({"sheet_id": sid, "reason": exc.detail})
    db.commit()
    return {"verified": ok, "skipped": skipped, "warnings": warn}


@router.post("/sheets/{sheet_id}/reject")
def reject_sheet(sheet_id: int, data: RejectRequest, db: Session = Depends(get_db)):
    require_perm(db, DUTY_REPORT)
    sheet = _load_sheet(db, sheet_id)
    period = db.get(Period, sheet.period_id)
    if period and period.status == "locked":
        raise HTTPException(400, "核算期已锁定")
    user = current_operator(data.user or "operator")
    if sheet.status == "rejected":
        raise HTTPException(400, "这张单已经是作废状态")
    if sheet.status == "verified":
        # 已确认的工时已经进了账和任务实报。直接作废等于让账凭空少一笔，
        # 必须走「撤销确认」这一步，撤销本身会留痕、会重算任务。
        raise HTTPException(400, "单据已确认，请先撤销确认再作废")
    old_status = sheet.status
    sheet.status = "rejected"
    sheet.reject_reason = data.reason
    db.add(AuditLog(entity="sheet", entity_id=sheet.id, sheet_id=sheet.id, field="status", old_value=old_status, new_value="rejected", user=user))
    # 驳回会把这单的工时从任务实报里摘掉，任务上的物化数字必须跟着重算，
    # 否则任务列表还挂着一个「已被驳回的单据凑出来的工时」。
    # 注意：Session 是 autoflush=False，不先 flush 的话 refresh_task 里的 SELECT
    # 还看不到刚才把状态改成 rejected，算出来仍是旧值，等于白重算。
    db.flush()
    task = db.get(WorkTask, sheet.task_id) if sheet.task_id else None
    if task is not None:
        task_service.refresh_task(db, task)
    db.commit()
    return {"ok": True, "status": sheet.status}


@router.post("/sheets/{sheet_id}/reopen")
def reopen_sheet(sheet_id: int, user: str = "", db: Session = Depends(get_db)):
    require_perm(db, DUTY_REPORT)
    sheet = _load_sheet(db, sheet_id)
    # 锁定＝这个月的账已经出了，重开会让「已出的工资」和「库里的数据」对不上。
    # 单张确认/驳回都查了锁定，重开不查等于留了个绕过口子。
    period = db.get(Period, sheet.period_id)
    if period and period.status == "locked":
        raise HTTPException(400, "核算期已锁定，如需更正请先解锁或在下月调整")
    if sheet.status != "verified":
        raise HTTPException(400, "只有已确认的单据可以撤销确认；作废单请用「恢复」")
    operator = current_operator(user or "operator")
    sheet.status = "recognized"
    sheet.verify_user = ""
    sheet.verify_at = None
    db.add(AuditLog(entity="sheet", entity_id=sheet.id, sheet_id=sheet.id, field="status", old_value="verified", new_value="recognized", user=operator))
    db.flush()
    task = db.get(WorkTask, sheet.task_id) if sheet.task_id else None
    if task is not None:
        task_service.refresh_task(db, task)
    db.commit()
    return {"ok": True, "status": sheet.status}


@router.post("/sheets/{sheet_id}/restore")
def restore_sheet(sheet_id: int, user: str = "", db: Session = Depends(get_db)):
    """作废错了要能改回来。

    以前作废是单向的：点错一次这张单就永远躺在台账里既不入账也删不掉，
    只能改库。作废本身是业务动作，可逆才算闭环。
    """
    require_perm(db, DUTY_REPORT)
    sheet = _load_sheet(db, sheet_id)
    period = db.get(Period, sheet.period_id)
    if period and period.status == "locked":
        raise HTTPException(400, "核算期已锁定")
    if sheet.status != "rejected":
        raise HTTPException(400, "只有作废单据可以恢复")
    operator = current_operator(user or "operator")
    sheet.status = "recognized"
    sheet.verify_user = ""
    sheet.verify_at = None
    db.add(
        AuditLog(
            entity="sheet",
            entity_id=sheet.id,
            sheet_id=sheet.id,
            field="status",
            old_value="rejected",
            new_value="recognized",
            user=operator,
        )
    )
    db.flush()
    task = db.get(WorkTask, sheet.task_id) if sheet.task_id else None
    if task is not None:
        task_service.refresh_task(db, task)
    db.commit()
    return {"ok": True, "status": sheet.status}


@router.delete("/sheets/{sheet_id}")
def delete_sheet(
    sheet_id: int,
    reason: str = Query("", description="删除原因（会写进留痕）"),
    user: str = Query(""),
    db: Session = Depends(get_db),
):
    """彻底删除一张单据（连同明细与原图）。

    错拍、重复上传的单据只作废的话，会一直占着台账、占着批次计数、占着硬盘；
    但删除不可逆，所以设三道闸：
      1. 已确认的不能删（先撤销确认 → 作废）；
      2. 核算期锁定的不能删（账已出，凭证必须留）；
      3. 删除前把整张单快照写进留痕，事后仍能查到删了什么。
    """
    require_perm(db, DUTY_REPORT)
    sheet = _load_sheet(db, sheet_id)
    period = db.get(Period, sheet.period_id)
    if period and period.status == "locked":
        raise HTTPException(400, "核算期已锁定，凭证不能删除")
    if sheet.status == "verified":
        raise HTTPException(400, "已确认单据不能删除，请先撤销确认并作废")
    operator = current_operator(user or "operator")
    img = db.get(Image, sheet.image_id)
    task_id = sheet.task_id
    snapshot = {
        "sheet_id": sheet.id,
        "status": sheet.status,
        "total_hours": sheet.total_hours,
        "header": json.loads(sheet.header_json or "{}"),
        "image": {
            "id": img.id if img else None,
            "filename": img.filename if img else "",
            "batch_id": img.batch_id if img else None,
        },
        "items": [
            {
                "seq": it.seq,
                "product_name": it.product_name,
                "part_name": it.part_name,
                "process": it.process,
                "worker": (it.worker.name if it.worker else "") or it.worker_raw,
                "qualified_qty": it.qualified_qty,
                "unit_hours": it.unit_hours,
                "total_hours": it.total_hours,
                "finish_date": str(it.finish_date) if it.finish_date else "",
            }
            for it in sheet.items
        ],
    }
    # 留痕挂在单据 id 上（单据记录会消失，但这行审计保留），
    # 谁说删了哪张单一目了然。
    db.add(
        AuditLog(
            entity="sheet",
            entity_id=sheet.id,
            sheet_id=None,
            field="deleted",
            old_value=json.dumps(snapshot, ensure_ascii=False),
            new_value=reason or "未填原因",
            user=operator,
        )
    )
    paths = []
    if img is not None:
        paths = [p for p in (img.path, img.processed_path, img.thumb_path) if p]
    for it in list(sheet.items):
        db.delete(it)
    db.delete(sheet)
    if img is not None:
        db.delete(img)
    db.flush()
    task = db.get(WorkTask, task_id) if task_id else None
    if task is not None:
        task_service.refresh_task(db, task)
    db.commit()
    # 文件删除放在提交之后：先删文件再回滚事务，库里会留下指向不存在文件的记录
    removed = []
    for p in paths:
        try:
            from pathlib import Path as _Path

            _Path(p).unlink(missing_ok=True)
            removed.append(p)
        except OSError:
            pass
    return {"ok": True, "deleted": sheet_id, "files_removed": len(removed), "snapshot_items": len(snapshot["items"])}


@router.patch("/sheets/{sheet_id}/dims")
def update_sheet_dims(sheet_id: int, data: SheetDimsUpdate, db: Session = Depends(get_db)):
    """已确认单据的维度补录（产品 / 件名 / 工序）。

    这三项不影响工时金额，但决定工时能不能挂到成本与工艺上。已确认的单据
    为了补一个件名去撤销确认，会抹掉复核人的确认时间戳；不补，汇总里就永远
    有一列「未填」。所以单开一条窄通道：只改这三项、必须写原因、逐项留痕。
    """
    require_perm(db, DUTY_REPORT)
    sheet = _load_sheet(db, sheet_id)
    period = db.get(Period, sheet.period_id)
    if period and period.status == "locked":
        raise HTTPException(400, "核算期已锁定")
    operator = current_operator(data.user or "operator")
    bundle = dict_bundle(db)
    by_id = {it.id: it for it in sheet.items}
    changed: list[dict] = []
    allowed = ("product_name", "part_name", "process")
    for row in data.items:
        it = by_id.get(int(row.get("id") or 0))
        if it is None:
            continue
        for field in allowed:
            if field not in row or row[field] is None:
                continue
            new_value = str(row[field]).strip()
            old_value = getattr(it, field) or ""
            if new_value == old_value:
                continue
            if not new_value:
                # 补录是「补」，不是「清空」：清空一个已入账行的工序，
                # 等于把它重新变成「未填」，比不补还糟。
                raise HTTPException(400, f"第 {it.seq} 行的{ {'product_name': '产品', 'part_name': '件名', 'process': '工序'}[field] }不能清空")
            _audit(db, it.id, f"item:{field}", old_value, new_value, operator, sheet.id, it.seq)
            setattr(it, field, new_value)
            if field == "process":
                _apply_process(it, new_value, bundle)
            changed.append({"item_id": it.id, "seq": it.seq, "field": field, "from": old_value, "to": new_value})
        it.row_state = "edited"
    if not changed:
        return {"ok": True, "changed": [], "message": "没有需要更新的字段"}
    db.add(
        AuditLog(
            entity="sheet",
            entity_id=sheet.id,
            sheet_id=sheet.id,
            field="dims:batch",
            old_value="",
            new_value=json.dumps({"reason": data.reason or "", "changed": changed}, ensure_ascii=False),
            user=operator,
        )
    )
    _revalidate(db, sheet)
    db.flush()
    task = db.get(WorkTask, sheet.task_id) if sheet.task_id else None
    if task is not None:
        task_service.refresh_task(db, task)
    db.commit()

    warnings = []
    if sheet.status == "verified":
        # 已确认单的维度改了，归属订单可能是按旧维度自动归的，得提醒一句
        warnings.append("该单已确认入账，维度变化不会改动工时金额，但归集订单可能需要复核")
    blocks = [
        it.seq
        for it in sheet.items
        if any(f.get("level") == "block" for f in json.loads(it.flags_json or "[]"))
    ]
    if blocks:
        warnings.append(f"第 { '、'.join(str(x) for x in blocks[:5]) } 行仍有阻断级问题，请核对")
    return {"ok": True, "changed": changed, "warnings": warnings, "status": sheet.status}


def _manual_keeps(db: Session, sheet: Sheet) -> dict:
    """重新识别时保留人工修改的清单。

    实现在 services/keeps.py——导入页的重识别走同一个函数，
    两个入口口径必须一致，否则从哪个页面点「重新识别」会给出不同结果。
    """
    return manual_keeps(db, sheet)


def _keeps_preview(db: Session, sheet: Sheet, keeps: dict) -> dict:
    """把「重新识别后会保留的人工修改」整理成界面可读的清单。

    重新识别前先把它摆出来，让人自己决定是保留还是让识别结果覆盖，
    避免识别完了才发现数字没变（或反过来被默默改掉）。
    """
    seq_of = {it.seq: it for it in sheet.items}
    rows = []
    for seq in sorted(keeps["items"]):
        it = seq_of.get(seq)
        fields = [
            {
                "key": key,
                "label": _field_label(f"item:{key}"),
                "value": _fmt_scalar(db, f"item:{key}", val),
            }
            for key, val in keeps["items"][seq].items()
        ]
        rows.append(
            {
                "seq": seq,
                "item_id": it.id if it else None,
                "process": it.process if it else "",
                "fields": fields,
            }
        )
    header = [
        {"key": key, "label": _HEADER_LABELS.get(key, key), "value": _fmt_scalar(db, key, val)}
        for key, val in keeps["header"].items()
    ]
    return {
        "rows": rows,
        "header": header,
        "total_rows": len(rows),
        "total_fields": sum(len(r["fields"]) for r in rows) + len(header),
    }


@router.get("/sheets/{sheet_id}/keeps")
def sheet_keeps(sheet_id: int, db: Session = Depends(get_db)):
    """重新识别前预览：哪些人工改过的字段会被保留。"""
    sheet = _load_sheet(db, sheet_id)
    return _keeps_preview(db, sheet, _manual_keeps(db, sheet))


@router.post("/sheets/{sheet_id}/reocr")
def reocr_sheet(sheet_id: int, keep_manual: bool = True, db: Session = Depends(get_db)):
    require_perm(db, DUTY_REPORT)
    sheet = _load_sheet(db, sheet_id)
    period = db.get(Period, sheet.period_id)
    if period and period.status == "locked":
        raise HTTPException(400, "核算期已锁定")
    if sheet.status == "verified":
        # 重新识别会把单据打回待核对并整行重建明细，等于撤销人工成果，
        # 必须先「撤销确认」，别让一次点击悄无声息地覆盖掉已确认的数据。
        raise HTTPException(400, "单据已确认，请先撤销确认，再重新识别")
    img = db.get(Image, sheet.image_id)
    if not img:
        raise HTTPException(404, "原图缺失")
    keeps = _manual_keeps(db, sheet)
    if not keep_manual:
        # 明确要求「按识别结果覆盖」：把此前的人工值记一笔作废，
        # 否则下一轮重新识别又会把它们当「人工修改」保留回来
        seq_to_item = {it.seq: it for it in sheet.items}
        for seq, fields in keeps["items"].items():
            it = seq_to_item.get(seq)
            for key, val in fields.items():
                _audit(
                    db, it.id if it else 0, "item:discard", _fmt_scalar(db, f"item:{key}", val), "", "operator", sheet.id, seq
                )
        for key, val in keeps["header"].items():
            _audit(db, sheet.id, "header:discard", _fmt_scalar(db, key, val), "", "operator", sheet.id)
        keeps = {"header": {}, "items": {}}
    img.status = "pending"
    img.error = ""
    db.commit()
    enqueue(img.id, keeps)
    return {
        "ok": True,
        "keep_manual": keep_manual,
        "kept_fields": sum(len(v) for v in keeps["items"].values()) + len(keeps["header"]),
        "kept_rows": len(keeps["items"]),
    }


@router.get("/work-queue")
def work_queue(ym: str | None = None, limit: int = Query(5, le=50), db: Session = Depends(get_db)):
    """待核对队列。优先返回告警多的单据。"""
    stmt = select(Sheet).where(Sheet.status == "recognized").options(selectinload(Sheet.image))
    if ym:
        p = db.scalar(select(Period).where(Period.ym == ym))
        if p:
            stmt = stmt.where(Sheet.period_id == p.id)
    rows = db.scalars(stmt.order_by(Sheet.flag_count.desc(), Sheet.id).limit(limit)).all()
    from pathlib import Path

    out = []
    for s in rows:
        stem = Path(s.image.processed_path).stem if (s.image and s.image.processed_path) else ""
        out.append(
            {
                "id": s.id,
                "flag_count": s.flag_count,
                "total_hours": s.total_hours,
                "thumb_url": f"/media/thumbs/{stem}.jpg" if stem else "",
            }
        )
    return out
