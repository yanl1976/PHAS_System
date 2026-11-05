"""工艺卡接口。

读接口 + 打印接口。卡片是「已发布组合 + 当前生效路线 + 部件层级」的投影，
没有自己的表，所以没有写入口 —— 要改卡上的内容，去「工艺及工时编排」改路线、
或去「工时组合」发/撤发布，卡跟着变，不存在「卡上写了一套、库里是另一套」。

**打印出来的就是《加工路线单》**（`app/render/route_form.py`，全项目唯一版式）。
工艺卡是这张纸「还没干活」的样子：工序与定额预印，操作者/加工结果/检查员留白；
干完活录回来，同一个「产品×件名」就变成已经存在的加工路线单（`sheet`）。
所以 `/api/cards/print.pdf` 与 `/api/sheets/{id}/print.pdf` 用的是同一个渲染器，
区别只在喂给它的数据：一个来自工艺编排，一个来自识别结果。
"""
from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import WorkCombo
from ..security import current_operator
from ..services import card_print as CP
from ..services import print_sheet as PS
from ..services import process_card as PC

router = APIRouter(prefix="/api/cards", tags=["cards"])


def _ensure_forms(
    db, task_id: int | None, cards: list[dict], *, create: bool = True
) -> dict[str, tuple[str, str]]:
    """为本批卡确保「预印单号 + 码」存在，返回 `{件名: (单号, token)}`。

    **必须复用而不是每次新建**：工艺卡页打印、扫码报工页打印、页内预览，
    三个入口印的是同一张纸，各建一套单号的话，工位上就会有 N 张写着不同号
    的纸，扫码时只认其中一张，另外几张永远报不了工。

    `create=False` 用于**页面预览**：看一眼不该产生业务数据。
    预览会把已存在的单号与码显示出来（方便确认印出来长什么样），
    但没有就是没有 —— 单号只在真正打印时生成。

    没有任务（按产品印标准工艺）时不建单：那种纸是「工艺标准」不是
    「某批活的执行凭证」，印上码反而会让人拿去报一个不存在的工。
    """
    if not task_id or not cards:
        return {}
    if create:
        res = PS.create_from_cards(db, task_id, cards, by=current_operator(""))
        return res.get("qr_map") or {}
    # 只查不建：预览用
    from sqlalchemy import select

    from ..models import PrintSheet

    parts = {c.get("part") for c in cards if c.get("part")}
    rows = db.scalars(
        select(PrintSheet).where(
            PrintSheet.task_id == task_id,
            # 已报工的单也要显示码：屏幕上看到的就是那张纸，
            # 它扫进来只会被「已报工」拦住，但号必须一致
            PrintSheet.status.in_(("printed", "reported")),
            PrintSheet.part_name.in_(parts),
        )
    )
    return {f.part_name: (f.form_no, f.token) for f in rows}


@router.get("")
def cards(
    product: str = "",
    part: str = "",
    task_id: int | None = None,
    include_children: bool = True,
    limit: int = Query(400, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """一整本工艺卡：卡片本体 + 左栏层级树 + 装订清单 + 产品/任务清单。

    一次返回这几样东西，因为它们是同一份数据的几种看法 —— 分开取会出现
    「左栏有这一件、右边没这张卡」的自相矛盾。

    `task_id` 与 `product` 二选一：给了任务就是「这一单要做哪几张卡」，
    表头印投产台数、定额合计按本单件数算；只给产品就是工艺科视角，按单台印。
    """
    return PC.build_cards(
        db,
        product=product,
        part=part,
        task_id=task_id,
        include_children=include_children,
        limit=limit,
        printed_by=current_operator(""),
    )


@router.get("/products")
def card_products(db: Session = Depends(get_db)):
    """产品清单（已发布条数、可打印工时）。给产品下拉用，不带工序明细。"""
    items = PC.products(db)
    return {
        "total": len(items),
        "released": sum(x["released"] for x in items),
        "products": items,
    }


@router.get("/tasks")
def card_tasks(db: Session = Depends(get_db)):
    """生产任务清单（能印几张卡 / 几件缺工艺），给任务下拉用。"""
    items = PC.tasks(db)
    return {"total": len(items), "tasks": items}


def _one_card(db: Session, *, combo_id: int, task_id: int | None) -> dict:
    """按组合 id 取回「本子里那张卡」——必须走整本重算，不能单造一张。

    编号是相对整本排出来的（01 → 01-01），单造一张会得到 `01`，
    印出来跟装订清单上的号对不上，车间拿着找不到是哪一件。
    """
    combo = db.get(WorkCombo, combo_id)
    if combo is None:
        raise HTTPException(404, "没有这条工时组合")
    res = PC.build_cards(db, product=combo.product or "", task_id=task_id)
    card = next((c for c in res["cards"] if c["combo_id"] == combo_id), None)
    if card is None and task_id:
        # 这张卡不在该任务里（任务只印本单要做的那几张）→ 按产品口径再找一次
        res = PC.build_cards(db, product=combo.product or "")
        card = next((c for c in res["cards"] if c["combo_id"] == combo_id), None)
    if card is None:
        raise HTTPException(
            404,
            f"「{combo.part}」当前不可打印："
            + ("工艺未发布" if combo.status != "released" else "没有生效的工艺路线"),
        )
    return card


@router.get("/print.png")
def print_png(
    combo_id: int,
    task_id: int | None = None,
    page: int = Query(1, ge=1),
    dpi: int = Query(150, ge=72, le=300),
    db: Session = Depends(get_db),
):
    """单张卡的一页，按加工路线单版式渲染成图片（页面预览用）。"""
    card = _one_card(db, combo_id=combo_id, task_id=task_id)
    # 预览必须带**真实**单号与二维码：页面上的「打印」就是把这些图直接送去打印，
    # 预览没码 = 纸上没码。按单张卡建单（不是整本），一次预览最多产生一个号，
    # 且同一张卡再预览会复用，不会越点越多。
    qr_map = _ensure_forms(db, task_id, [card]) if task_id else {}
    png = CP.render_png(card, task=card.get("task"), dpi=dpi, page=page, qr_map=qr_map)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/print.pdf")
def print_pdf(
    combo_id: int | None = None,
    product: str = "",
    part: str = "",
    task_id: int | None = None,
    include_children: bool = True,
    dpi: int = Query(300, ge=72, le=400),
    db: Session = Depends(get_db),
):
    """整本工艺卡 → 一个 PDF：一卡一页，工序多的卡自己续页。

    给了 `combo_id` 只印那一张（车间掉了哪张补哪张），否则按产品/任务整本印。
    """
    if combo_id:
        card = _one_card(db, combo_id=combo_id, task_id=task_id)
        cards = [card]
        task = card.get("task")
        name = card["part"]
    else:
        res = PC.build_cards(db, product=product, part=part, task_id=task_id,
                             include_children=include_children)
        cards = res["cards"]
        task = res.get("task")
        if not cards:
            raise HTTPException(404, "没有可打印的工艺卡：这些件名的工艺还没有发布")
        name = (task or {}).get("task_no") or res["product"] or "全部"

    pdf = CP.render_pdf(cards, task=task, dpi=dpi, qr_map=_ensure_forms(db, task_id, cards))
    # 文件名要能直接看懂是哪一单哪一件：下载下来一堆 print(3).pdf 没人分得清
    safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|').strip() or "工艺卡"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f"inline; filename*=UTF-8''{quote(f'工艺卡_{safe}.pdf')}"
            ),
            "Cache-Control": "no-store",
        },
    )
