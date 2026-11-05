"""扫码报工接口：印单 → 扫码 → 提交。

三个动词，对应车间真实动作：

    POST /forms        计划员说「这单活开工了，把纸印出来」
    GET  /scan         拿着签好字的纸扫一下
    POST /submit       对着屏幕核完数量，提交

外加两个纠错动作（recent / undo）——现实里点错数量、扫错单是常事，
不给撤销就会演变成「让 IT 去数据库里删」。
"""
from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..domain.sys_roles import DUTY_REPORT
from .auth import require_perm
from ..models import PrintSheet
from ..security import current_operator
from ..services import print_sheet as PS

router = APIRouter(prefix="/api/report", tags=["report"])


class GenerateIn(BaseModel):
    task_id: int
    parts: list[str] | None = None
    force: bool = False


@router.post("/forms")
def create_forms(data: GenerateIn, db: Session = Depends(get_db)):
    """按任务生成预印单（一个件名一张纸）。"""
    require_perm(db, DUTY_REPORT)
    return PS.generate(
        db, data.task_id, parts=data.parts, force=data.force, by=current_operator("")
    )


@router.get("/forms")
def list_forms(
    task_id: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
):
    return PS.recent(db, limit=limit, task_id=task_id)


@router.get("/forms/pdf")
def forms_pdf(
    task_id: int | None = None,
    form_nos: str = "",
    status: str = "printed",
    db: Session = Depends(get_db),
):
    """打印。默认只印「已打印未报工」的，避免把作废单也印出来。"""
    stmt = select(PrintSheet).order_by(PrintSheet.form_no)
    if form_nos.strip():
        nos = [x.strip() for x in form_nos.split(",") if x.strip()]
        stmt = select(PrintSheet).where(PrintSheet.form_no.in_(nos)).order_by(PrintSheet.form_no)
    elif task_id:
        stmt = stmt.where(PrintSheet.task_id == task_id)
        if status:
            stmt = stmt.where(PrintSheet.status == status)
    else:
        # 「打印当前全部待报工单」：没给任务也没给单号，就是全厂还没报工的纸。
        # 以前这里直接 400，页脚那个链接点了必定报错。
        if status:
            stmt = stmt.where(PrintSheet.status == status)
        stmt = stmt.limit(300)
    forms = list(db.scalars(stmt))
    if not forms:
        raise HTTPException(404, "没有可打印的预印单")
    pdf = PS.render_pdf(db, forms)
    return Response(
        pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="route-forms-{dt.date.today():%Y%m%d}.pdf"'
        },
    )


@router.get("/scan")
def scan(code: str, db: Session = Depends(get_db)):
    """扫码解析。码里只有单号和行号，其余一律回后端查。"""
    res = PS.scan(db, code)
    if not res.get("ok"):
        # 用 200 返回「语义失败」：扫码枪/前端要拿到 code 来判断是
        # 「已报过工」还是「码不对」，两者处理方式完全不同。
        return res
    return res


class RowIn(BaseModel):
    row_no: int
    qty_done: float | None = None
    qualified: float = 0
    rework: float = 0
    scrap: float = 0
    worker_name: str = ""
    inspector: str = ""
    actual_hours: float | None = None
    adjust_reason: str = ""


class SubmitIn(BaseModel):
    form_no: str
    rows: list[RowIn]
    sign_verified: bool = False
    report_date: str = ""
    note: str = ""


@router.post("/submit")
def submit(data: SubmitIn, db: Session = Depends(get_db)):
    """提交报工。重复提交会被 form_no 拦掉 —— 那是重复发工资的口子。"""
    require_perm(db, DUTY_REPORT)
    day = None
    if data.report_date.strip():
        try:
            day = dt.date.fromisoformat(data.report_date.strip())
        except ValueError:
            raise HTTPException(400, "报工日期格式应为 YYYY-MM-DD")
    res = PS.submit(
        db,
        data.form_no.strip(),
        [r.model_dump() for r in data.rows],
        by=current_operator(""),
        sign_verified=data.sign_verified,
        report_date=day,
        note=data.note,
    )
    if not res.get("ok"):
        return res
    return res


@router.post("/undo")
def undo(form_no: str, reason: str = "", db: Session = Depends(get_db)):
    """撤销报工。**必须写原因** —— 撤销是删钱的动作，只留一条 undo 等于没留痕。"""
    require_perm(db, DUTY_REPORT)
    return PS.undo(db, form_no.strip(), by=current_operator(""), reason=reason)


class VoidIn(BaseModel):
    form_no: str
    reason: str = ""


@router.post("/void")
def void_form(data: VoidIn, db: Session = Depends(get_db)):
    """手工作废一张预印单：零件不做了、单子印错了，纸上的码立刻失效。

    已报工的单不能一步作废（那会连带抹掉工时），必须先撤销。
    """
    require_perm(db, DUTY_REPORT)
    return PS.void_form(
        db, data.form_no.strip(), by=current_operator(""), reason=data.reason
    )


@router.get("/ledger")
def ledger(
    ym: str = "",
    keyword: str = "",
    status: str = "",
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """报工台账的「扫码报工」一侧（以预印单为主键）。

    月底核对要在一处看到「这个月谁报了什么」，不能识别单一套台账、
    扫码单又只在扫码页里翻 —— 那等于两套账，少看一块就会以为漏账。
    """
    return PS.ledger(db, ym=ym, keyword=keyword, status=status, page=page, size=size)


@router.get("/ledger/{form_no}")
def ledger_detail(form_no: str, db: Session = Depends(get_db)):
    """一张预印单的只读明细：纸上印了什么 + 实际报了谁多少工时。"""
    return PS.form_detail(db, form_no)
