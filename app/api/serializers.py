"""ORM 对象序列化。"""
from __future__ import annotations

import json

from ..models import Image, Sheet, WorkItem


def _loads(text: str, default):
    try:
        return json.loads(text) if text else default
    except Exception:
        return default


def item_to_dict(it: WorkItem) -> dict:
    return {
        "id": it.id,
        "sheet_id": it.sheet_id,
        "seq": it.seq,
        "product_name": it.product_name,
        "batch_no": it.batch_no,
        "part_name": it.part_name,
        "material": it.material,
        "process": it.process,
        "process_raw": getattr(it, "process_raw", "") or "",
        "process_std": getattr(it, "process_std", "") or "",
        "process_code": it.process_code,
        "qualified_qty": it.qualified_qty,
        "rework_qty": it.rework_qty,
        "scrap_qty": it.scrap_qty,
        "unit_hours": it.unit_hours,
        "total_hours": it.total_hours,
        "finish_date": it.finish_date.isoformat() if it.finish_date else None,
        "finish_date_raw": getattr(it, "finish_date_raw", "") or "",
        "inspector": it.inspector,
        "inspector_raw": getattr(it, "inspector_raw", "") or "",
        "handover": it.handover,
        "worker_id": it.worker_id,
        "worker_name": it.worker.name if it.worker else "",
        "worker_raw": it.worker_raw,
        "conf": _loads(it.conf_json, {}),
        "flags": _loads(it.flags_json, []),
        "bbox": _loads(it.bbox_json, {}),
        "row_state": it.row_state,
        "remark": it.remark,
    }


def sheet_to_dict(sheet: Sheet, with_items: bool = True) -> dict:
    from pathlib import Path

    img: Image | None = sheet.image
    stem = Path(img.processed_path).stem if (img and img.processed_path) else ""
    data = {
        "id": sheet.id,
        "image_id": sheet.image_id,
        "period_id": sheet.period_id,
        "form_type": sheet.form_type,
        "status": sheet.status,
        # 这张纸如果是系统预印的，这里是它的印刷体单号：台账/核对页要能看出
        # 「这张纸和扫码报工的那张是同一张」
        "print_form_no": sheet.print_form_no or "",
        "header": _loads(sheet.header_json, {}),
        "total_hours": sheet.total_hours,
        "flag_count": sheet.flag_count,
        "item_count": len(sheet.items),
        "page_w": sheet.page_w,
        "page_h": sheet.page_h,
        "verify_user": sheet.verify_user,
        "verify_at": sheet.verify_at.isoformat() if sheet.verify_at else None,
        "reject_reason": sheet.reject_reason,
        "note": sheet.note,
        "created_at": sheet.created_at.isoformat() if sheet.created_at else None,
        "image": {
            "id": img.id if img else None,
            "filename": img.filename if img else "",
            "status": img.status if img else "",
            "error": img.error if img else "",
            "width": img.width if img else 0,
            "height": img.height if img else 0,
            "skew_angle": img.skew_angle if img else 0,
            "red_ratio": img.red_ratio if img else 0,
            "original_url": f"/media/raw/{Path(img.path).name}" if img else "",
            "processed_url": f"/media/processed/{stem}.png" if stem else "",
            "thumb_url": f"/media/thumbs/{stem}.jpg" if stem else "",
            "mask_url": f"/media/processed/{stem}_red.png" if stem else "",
        },
        "layout": _loads(sheet.layout_json, {}),
    }
    if with_items:
        data["items"] = [item_to_dict(it) for it in sheet.items]
    return data


def image_to_dict(img: Image) -> dict:
    from pathlib import Path

    stem = Path(img.processed_path).stem if img.processed_path else ""
    return {
        "id": img.id,
        "batch_id": img.batch_id,
        "filename": img.filename,
        "status": img.status,
        "error": img.error,
        "width": img.width,
        "height": img.height,
        "skew_angle": img.skew_angle,
        "red_ratio": img.red_ratio,
        "sheet_id": img.sheet.id if img.sheet else None,
        "thumb_url": f"/media/thumbs/{stem}.jpg" if stem else "",
        "processed_url": f"/media/processed/{stem}.png" if stem else "",
    }
