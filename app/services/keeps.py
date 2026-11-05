"""重新识别前的人工修改清单。

重新识别的目的是修正识别错的地方，不该顺手推翻人刚核对过的值：
这里从留痕里还原「人改过哪些字段」，作为 keeps 传给识别流水线，
由 pipeline._apply_keeps 按行号 + 字段名盖回新结果上。

之所以单独成模块：核对页（/api/sheets/{id}/reocr）和导入页
（/api/images/{id}/reocr）是两个入口，漏掉任何一个都会让人白改一遍
——手工选的操作者、改过的工时会被整行重建覆盖掉。
"""
from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Sheet

# 这些字段即便有留痕也不该当人工值回填：
# created/deleted/overwritten 是流水标记，worker_raw/worker_name 是随 worker_id
# 派生的展示值，total_hours 是算出来的。
_SKIP = {"created", "deleted", "overwritten", "worker_raw", "worker_name", "total_hours"}


def manual_keeps(db: Session, sheet: Sheet) -> dict:
    """从留痕里还原「人改过哪些字段」。

    返回 {"header": {key: value}, "items": {seq: {field: value}}}，
    直接可以塞给 pipeline.enqueue/recognize_image。
    """
    seq_of = {it.id: it.seq for it in sheet.items}
    items: dict[int, dict[str, str]] = {}
    header: dict[str, str] = {}
    last_edit: dict[int, str] = {}
    last_delete: dict[int, str] = {}

    rows = db.scalars(
        select(AuditLog)
        .where(
            AuditLog.sheet_id == sheet.id,
            AuditLog.field.like("item:%")
            | (AuditLog.field == "header")
            | (AuditLog.field == "header:discard"),
        )
        .order_by(AuditLog.ts)
    ).all()
    for a in rows:
        if a.field == "header:discard":
            header.clear()
            continue
        if a.field == "header":
            try:
                old_h = json.loads(a.old_value or "{}")
                new_h = json.loads(a.new_value or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(old_h, dict) or not isinstance(new_h, dict):
                continue
            for key, val in new_h.items():
                if str(old_h.get(key) or "") != str(val or "") and str(val or "").strip():
                    header[key] = val
            continue

        field = a.field.split(":", 1)[1]
        if field == "deleted":
            # 行被删掉过：重建时不该把删掉的那行的值再按序号塞回来
            try:
                snap = json.loads(a.old_value or "{}")
            except (TypeError, ValueError):
                continue
            seq = snap.get("seq") if isinstance(snap, dict) else None
            if isinstance(seq, (int, float)):
                last_delete[int(seq)] = a.ts.isoformat()
            continue
        if field in _SKIP:
            continue
        seq = a.seq if a.seq is not None else seq_of.get(a.entity_id)
        if field == "discard":
            # 上一次「重新识别」时选了「按识别结果覆盖」：此前的人工值到此作废，
            # 否则再点一次重新识别又会把它捞回来
            if seq:
                items.pop(seq, None)
                last_edit.pop(seq, None)
            continue
        if not seq:
            continue
        if a.new_value is None or str(a.new_value).strip() in ("", "None", "null"):
            continue
        items.setdefault(seq, {})[field] = a.new_value
        last_edit[seq] = a.ts.isoformat()

    # 先改后删的行，删掉就是最终意图，别把改动又捞回来
    for seq, ts in last_delete.items():
        if seq in items and ts >= last_edit.get(seq, ""):
            items.pop(seq, None)

    return {"header": header, "items": items}


def count_fields(keeps: dict) -> tuple[int, int]:
    """(字段数, 行数)，给接口回显「会保留 N 处人工修改」。"""
    items = (keeps or {}).get("items") or {}
    return sum(len(v) for v in items.values()) + len((keeps or {}).get("header") or {}), len(items)
