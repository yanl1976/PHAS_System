"""导入接口：核算期、批次、图片上传与识别触发。"""
from __future__ import annotations

import json
import datetime as dt

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..security import current_operator
from ..models import AuditLog, Batch, Image, Period, Sheet
from ..schemas import BatchCreate, PeriodCreate, ReOcrRequest
from ..services.pipeline import DuplicateImage, enqueue, save_upload
from ..services.keeps import count_fields, manual_keeps
from .serializers import image_to_dict

router = APIRouter(prefix="/api", tags=["intake"])


# ------------------------------------------------------------------ 核算期
@router.get("/periods")
def list_periods(db: Session = Depends(get_db)):
    rows = db.scalars(select(Period).order_by(Period.ym.desc())).all()
    out = []
    for p in rows:
        sheets = db.scalar(select(Sheet.id).where(Sheet.period_id == p.id).limit(1))
        out.append(
            {
                "id": p.id,
                "ym": p.ym,
                "title": p.title or p.ym,
                "status": p.status,
                "has_data": sheets is not None,
                "locked_at": p.locked_at.isoformat() if p.locked_at else None,
            }
        )
    return out


@router.post("/periods")
def create_period(data: PeriodCreate, db: Session = Depends(get_db)):
    if db.scalar(select(Period).where(Period.ym == data.ym)):
        raise HTTPException(400, "核算期已存在")
    p = Period(ym=data.ym, title=data.title or f"{data.ym[:4]}年{int(data.ym[5:7])}月")
    db.add(p)
    db.commit()
    db.refresh(p)
    return {"id": p.id, "ym": p.ym}


@router.post("/periods/{period_id}/lock")
def lock_period(period_id: int, user: str = "", db: Session = Depends(get_db)):
    user = current_operator(user or "operator")
    p = db.get(Period, period_id)
    if not p:
        raise HTTPException(404, "核算期不存在")
    # 只拦「还等着人去核对」的单据。被作废（rejected）的单据是已经处理完的结果，
    # 它不入账、也不会再被确认 —— 若把它也算作未完成，这个月永远锁不上，
    # 错一张单就整月结不了账。
    pending = db.scalar(
        select(Sheet.id).where(
            Sheet.period_id == p.id,
            Sheet.status.notin_(("verified", "rejected")),
        ).limit(1)
    )
    if pending:
        raise HTTPException(400, "存在未核对完成的单据，无法锁定")
    old = p.status
    p.status = "locked"
    p.locked_at = dt.datetime.now()
    db.add(
        AuditLog(
            entity="period",
            entity_id=p.id,
            field="status",
            old_value=old,
            new_value="locked",
            user=user or "operator",
        )
    )
    db.commit()
    return {"ok": True, "status": p.status}


@router.post("/periods/{period_id}/unlock")
def unlock_period(period_id: int, user: str = "", reason: str = "", db: Session = Depends(get_db)):
    user = current_operator(user or "operator")
    p = db.get(Period, period_id)
    if not p:
        raise HTTPException(404, "核算期不存在")
    old = p.status
    p.status = "open"
    p.locked_at = None
    # 解锁等于「把已经出过的账重新打开」，属于最需要追责的动作：
    # 谁解的、为什么解，必须留下来，否则月底对不上账时无从查起。
    db.add(
        AuditLog(
            entity="period",
            entity_id=p.id,
            field="status",
            old_value=old,
            new_value="open",
            user=user or "operator",
        )
    )
    if reason.strip():
        db.add(
            AuditLog(
                entity="period",
                entity_id=p.id,
                field="unlock_reason",
                old_value="",
                new_value=reason.strip(),
                user=user or "operator",
            )
        )
    db.commit()
    return {"ok": True, "status": p.status}


# ------------------------------------------------------------------ 批次
@router.get("/batches")
def list_batches(period_id: int | None = None, db: Session = Depends(get_db)):
    stmt = select(Batch).order_by(Batch.id.desc())
    if period_id:
        stmt = stmt.where(Batch.period_id == period_id)
    rows = db.scalars(stmt).all()
    out = []
    for b in rows:
        total = len(b.images)
        done = sum(1 for i in b.images if i.status == "done")
        failed = sum(1 for i in b.images if i.status == "failed")
        out.append(
            {
                "id": b.id,
                "period_id": b.period_id,
                "name": b.name,
                "source": b.source,
                "created_at": b.created_at.isoformat(),
                "images": total,
                "done": done,
                "failed": failed,
                "pending": total - done - failed,
            }
        )
    return out


@router.post("/batches")
def create_batch(data: BatchCreate, db: Session = Depends(get_db)):
    if not db.get(Period, data.period_id):
        raise HTTPException(404, "核算期不存在")
    count = db.scalar(select(Batch.id).where(Batch.period_id == data.period_id)) or 0
    b = Batch(
        period_id=data.period_id,
        name=data.name or f"批次{dt.datetime.now():%m%d-%H%M}",
        source=data.source,
        created_by=data.created_by,
    )
    db.add(b)
    db.commit()
    db.refresh(b)
    return {"id": b.id, "name": b.name, "seq": bool(count)}


@router.get("/batches/{batch_id}/images")
def batch_images(batch_id: int, db: Session = Depends(get_db)):
    b = db.get(Batch, batch_id)
    if not b:
        raise HTTPException(404, "批次不存在")
    rows = db.scalars(select(Image).where(Image.batch_id == batch_id).order_by(Image.id)).all()
    return [image_to_dict(i) for i in rows]


# ------------------------------------------------------------------ 上传
@router.post("/batches/{batch_id}/images")
async def upload_images(
    batch_id: int,
    files: list[UploadFile] = File(...),
    auto_recognize: bool = True,
    db: Session = Depends(get_db),
):
    b = db.get(Batch, batch_id)
    if not b:
        raise HTTPException(404, "批次不存在")
    period = db.get(Period, b.period_id)
    if period and period.status == "locked":
        raise HTTPException(400, "核算期已锁定，无法导入")

    results = []
    for f in files:
        data = await f.read()
        try:
            img = save_upload(db, batch_id, f.filename or "unnamed.jpg", data)
        except DuplicateImage as dup:
            results.append({"filename": f.filename, "ok": False, "duplicate": True, "image_id": dup.existing_id, "message": "图片已存在，已跳过"})
            continue
        except ValueError as exc:
            results.append({"filename": f.filename, "ok": False, "message": str(exc)})
            continue
        if auto_recognize:
            enqueue(img.id)
        results.append({"filename": f.filename, "ok": True, "image_id": img.id, "message": "已入队识别"})

    return {"batch_id": batch_id, "results": results}


@router.get("/images/{image_id}")
def get_image(image_id: int, db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    data = image_to_dict(img)
    if img.sheet:
        data["sheet"] = {
            "id": img.sheet.id,
            "status": img.sheet.status,
            "items": len(img.sheet.items),
            "total_hours": img.sheet.total_hours,
            "flag_count": img.sheet.flag_count,
        }
    return data


@router.post("/images/{image_id}/reocr")
def reocr(image_id: int, _data: ReOcrRequest | None = None, db: Session = Depends(get_db)):
    img = db.get(Image, image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    period = db.get(Period, db.get(Batch, img.batch_id).period_id)
    if period and period.status == "locked":
        raise HTTPException(400, "核算期已锁定")
    # 已经核对过的单据：先取出「人工改过哪些字段」，重新识别时盖回去。
    # 以前这里直接 enqueue，不带 keeps——从导入页点一次「重新识别」，
    # 核对页里选好的人（草书签名认不出来的那几个）、改过的工时就被整行重建覆盖了。
    keeps: dict = {"header": {}, "items": {}}
    if img.sheet:
        keeps = manual_keeps(db, img.sheet)
    img.status = "pending"
    img.error = ""
    db.commit()
    enqueue(image_id, keeps)
    fields, rows = count_fields(keeps)
    return {"ok": True, "kept_fields": fields, "kept_rows": rows}


@router.post("/batches/{batch_id}/retry-failed")
def retry_failed(batch_id: int, db: Session = Depends(get_db)):
    rows = db.scalars(
        select(Image).where(Image.batch_id == batch_id, Image.status.in_(["failed", "pending"]))
    ).all()
    for img in rows:
        img.status = "pending"
        img.error = ""
    db.commit()
    for img in rows:
        # 与单张重识别同一口径：核对过的人工值不能因为「重试失败」被清掉
        enqueue(img.id, manual_keeps(db, img.sheet) if img.sheet else None)
    return {"ok": True, "count": len(rows)}
