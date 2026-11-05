"""API 请求/响应模型。"""
from __future__ import annotations

import datetime as dt
from typing import Any

from pydantic import BaseModel, Field


class PeriodCreate(BaseModel):
    ym: str = Field(pattern=r"^\d{4}-\d{2}$")
    title: str = ""


class BatchCreate(BaseModel):
    period_id: int
    name: str = ""
    source: str = "photo"
    created_by: str = ""


class ItemUpdate(BaseModel):
    id: int | None = None
    seq: int | None = None
    product_name: str | None = None
    batch_no: str | None = None
    part_name: str | None = None
    material: str | None = None
    process: str | None = None
    qualified_qty: float | None = None
    rework_qty: float | None = None
    scrap_qty: float | None = None
    unit_hours: float | None = None
    finish_date: dt.date | None = None
    inspector: str | None = None
    handover: str | None = None
    worker_id: int | None = None
    worker_raw: str | None = None
    remark: str | None = None


class SheetUpdate(BaseModel):
    header: dict[str, Any] | None = None
    items: list[ItemUpdate] | None = None
    note: str | None = None
    user: str = "operator"


class VerifyRequest(BaseModel):
    user: str = "operator"
    force: bool = False  # 有 block 级告警时是否强制确认
    # 完工日期不在本核算期的单据：确认会把工时记进本月，必须显式点头。
    # 不设这个开关的话，一张 7 月的单混在 9 月账里，谁都不知道。
    confirm_out_of_period: bool = False


class BatchVerifyRequest(BaseModel):
    sheet_ids: list[int]
    user: str = "operator"
    confirm_out_of_period: bool = False


class RejectRequest(BaseModel):
    reason: str = ""
    user: str = "operator"


class SheetDimsUpdate(BaseModel):
    """已确认单据的维度补录。

    产品 / 件名 / 工序为空时，工时算得出来，但成本与工艺分析挂不上去。
    已确认单据为了补这三个值去「撤销确认 → 改 → 再确认」，会把复核人的
    确认时间戳抹掉，代价太大。维度不影响工时金额，所以单开一条补录通道：
    只改这三类字段、必须留痕、必须写原因，改动后重算告警与月份汇总。
    """

    items: list[dict[str, Any]] = Field(default_factory=list)  # [{id, product_name?, part_name?, process?}]
    reason: str = ""
    user: str = "operator"


class SheetTaskMove(BaseModel):
    """把单据改挂到另一个订单任务。task_id 为空 = 从任务上摘下来。"""

    task_id: int | None = None
    user: str = "operator"
    # 新订单里还没有该件名的引用时，顺手补挂（引用已发布基线，不复制）
    attach_routes: bool = False
    # 跨期改挂必须显式确认：两个月的汇总都会变
    force: bool = False


class SheetBatchTaskMove(SheetTaskMove):
    sheet_ids: list[int] = Field(default_factory=list)


class ReOcrRequest(BaseModel):
    deskew: bool = True
