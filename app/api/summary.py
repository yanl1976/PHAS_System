"""汇总、勾稽与导出接口。"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import EXPORT_DIR
from ..db import get_db
from ..domain.aggregation import monthly_summary, reconcile
from ..domain.export_excel import build_workbook
from ..models import Period

router = APIRouter(prefix="/api", tags=["summary"])


@router.get("/summary")
def summary(ym: str, include_unverified: bool = False, db: Session = Depends(get_db)):
    if not db.scalar(select(Period).where(Period.ym == ym)):
        raise HTTPException(404, f"核算期 {ym} 不存在")
    data = monthly_summary(db, ym, include_unverified=include_unverified)
    period = db.scalar(select(Period).where(Period.ym == ym))
    data["period_status"] = period.status
    return data


@router.get("/summary/matrix")
def summary_matrix(ym: str, include_unverified: bool = False, db: Session = Depends(get_db)):
    """人员 x 工序 交叉表，供前端渲染矩阵。"""
    data = monthly_summary(db, ym, include_unverified=include_unverified)
    processes: list[str] = []
    for p in data["persons"]:
        for proc in p["processes"]:
            if proc["process"] not in processes:
                processes.append(proc["process"])
    rows = []
    for p in data["persons"]:
        hours_map = {x["process"]: x["hours"] for x in p["processes"]}
        rows.append(
            {
                "worker_name": p["worker_name"],
                "total": p["hours"],
                "cells": [hours_map.get(proc, 0.0) for proc in processes],
            }
        )
    columns_total = [round(sum(r["cells"][i] for r in rows), 4) for i in range(len(processes))]
    return {
        "ym": ym,
        "processes": processes,
        "rows": rows,
        "columns_total": columns_total,
        "grand_total": round(sum(r["total"] for r in rows), 4),
    }


@router.get("/reconcile")
def reconcile_api(ym: str, db: Session = Depends(get_db)):
    if not db.scalar(select(Period).where(Period.ym == ym)):
        raise HTTPException(404, f"核算期 {ym} 不存在")
    return reconcile(db, ym)


@router.get("/export")
def export(ym: str, preview: bool = False, db: Session = Depends(get_db)):
    period = db.scalar(select(Period).where(Period.ym == ym))
    if not period:
        raise HTTPException(404, f"核算期 {ym} 不存在")
    if not preview:
        report = reconcile(db, ym)
        if not report["balanced"]:
            raise HTTPException(400, "勾稽校验未通过，请先处理错误项后导出")
    path = build_workbook(db, ym, include_unverified=preview)
    return FileResponse(
        str(path),
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@router.get("/export/history")
def export_history(limit: int = 20):
    files = sorted(Path(EXPORT_DIR).glob("*.xlsx"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [
        {
            "name": f.name,
            "size": f.stat().st_size,
            "created_at": dt.datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
            "url": f"/media/exports/{f.name}",
        }
        for f in files
    ]
