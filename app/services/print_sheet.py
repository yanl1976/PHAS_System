"""预印《加工路线单》与扫码报工。

## 这条链要解决什么

原来唯一的入口是「别人手写 → 拍照 → 识别」。识别注定只能是概率的，
而工资不允许概率。所以改成两条通道并存，但**主次分明**：

    系统印单（数据已经是结构化的）
        → 工人加工、在纸上签字确认
        → 计划员收单，扫码
        → 屏幕上是这一张单的全部工序，数量按纸上填的微调
        → 提交，直接进 work_item（source='scan'）

识别通道保留（`source='ocr'`）：历史补录、外协件、断网现场、外包队仍用纸。

## 为什么扫码的是计划员、不是工人

扫码报工失败的项目，多数死在「要求每个工人在工位上掏手机扫」：
换机型、没网、手上有机油、一天扫二十次。而计划员本来就要收单、本来就要
把纸上的数字敲进电脑 —— 他只是把「打字」换成「扫一下 + 改两个数」，
工作量下降，抵触自然小。纸质签字仍然留在纸上，责任链没断。

## 幂等：这是整条链最危险的地方

同一批工时可能从两条通道进来：计划员扫了码，文员又把那张纸拍照上传识别了。
**重复入账 = 多发一份工资**。所以 `print_sheet.form_no`（印刷体单号）是幂等键：
提交前先查这个号报过没有，识别侧命中已报工的单号也只告警不重复入账。
"""
from __future__ import annotations

import datetime as dt
import io

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..domain import qr
from ..models import (
    AuditLog,
    Period,
    PrintSheet,
    PrintSheetLine,
    Sheet,
    Worker,
    WorkItem,
)
from ..render import qr_stamp
from ..render import route_form as form
from . import card_print as CP
from . import process_card as PC

# 二维码位置与绘制都在 app/render/qr_stamp.py —— 工艺卡打印与预印单打印共用一份，
# 两份版式各贴一遍的后果是改了一个忘另一个，纸上的码跑进表格里，识别侧当场列错位。
QR_X = qr_stamp.QR_X
QR_Y = qr_stamp.QR_Y
QR_SIZE = qr_stamp.QR_SIZE


def _k(dpi: int) -> float:
    return dpi / 25.4


def _next_form_no(db: Session, today: dt.date | None = None, skip: int = 0) -> str:
    """RG2609-00001。按年月分段流水，人一眼能看出是哪个月印的。

    为什么不用纯自增 id：单号是印在纸上的，纸要归档几年。`RG2609-00001`
    扫不出来时，人还能照着号在系统里搜；纯 id 就只是一串没有含义的数字。

    `skip` 是给并发撞号用的：两个计划员同时打印时，各自算出的「最大号 +1」
    可能相同，唯一索引会挡下后一个。此时换一个号重试（跳过几个号不影响什么，
    纸面上的号本来就不会连得那么整齐），而不是抛 500 让人以为打印机坏了。
    """
    today = today or dt.date.today()
    head = f"RG{today:%y%m}-"
    last = db.scalar(
        select(func.max(PrintSheet.form_no)).where(PrintSheet.form_no.like(f"{head}%"))
    )
    seq = 1
    if last:
        try:
            seq = int(last.split("-")[-1]) + 1
        except ValueError:
            seq = 1
    return f"{head}{seq + max(0, skip):05d}"


def _insert_form(
    db: Session, *, task_id: int | None, card: dict, qty: float, by: str
) -> PrintSheet:
    """建一张预印单 + 它的工序快照；并发撞号时换号重试。

    为什么要 SAVEPOINT 而不是直接 rollback：整本卡是**一次调用里连建的**，
    真 rollback 会把这一批已经建好的单一起退掉（还会留下一批失效对象），
    表现就成了「打印 10 张只出来 3 张」。SAVEPOINT 只回滚失败的那一次插入。
    """
    fields = {
        "task_id": task_id,
        "product_name": card.get("product") or "",
        "batch_no": (card.get("task") or {}).get("batch_no") or "",
        "part_name": card.get("part") or "",
        "part_no": card.get("code") or "",
        "route_id": card.get("route_id"),
        "route_version": int(card.get("version") or 1),
        "qty_plan": float((card.get("task") or {}).get("qty") or 0),
        "part_qty": qty,
        "total_hours": float(card.get("batch_hours") or 0),
        "status": "printed",
        "printed_by": by,
    }
    last_err: Exception | None = None
    for attempt in range(8):
        form_no = _next_form_no(db, skip=attempt)
        sp = db.begin_nested()
        fs = PrintSheet(form_no=form_no, token=qr.new_token(), **fields)
        try:
            db.add(fs)
            db.flush()
        except IntegrityError as exc:  # pragma: no cover - 只在并发打印时触发
            sp.rollback()
            last_err = exc
            continue
        for idx, s in enumerate(card.get("steps") or [], start=1):
            unit = float(s.get("unit_hours") or 0)
            db.add(
                PrintSheetLine(
                    form_id=fs.id,
                    row_no=idx,
                    seq=int(s.get("seq") or idx * 10),
                    process_code=s.get("process_code") or "",
                    process_name=(s.get("process_name") or "").strip(),
                    category=s.get("category") or "",
                    work_center=s.get("work_center") or "",
                    unit_hours=unit,
                    qty_plan=qty,
                    total_hours=round(unit * qty, 4),
                    is_key=bool(s.get("is_key")),
                )
            )
        return fs
    raise RuntimeError(f"单号分配失败（{fields['part_name']}）：{last_err}")


# ---------------------------------------------------------------- 两条通道互认
def bind_print_form(db: Session, sheet: "Sheet", lines: list[dict]) -> dict:
    """把识别单和它对应的预印单绑起来，并回答「这张纸的现状」。

    这是整条链最要紧的一步：**同一张纸只能算一次工时**。扫码报工和拍照识别
    是两个入口，纸只有一张 —— 计划员扫完码，文员又把同一张纸拍了一遍，
    识别流水线照常建单、照常入账，工资就发两遍。

    识别侧能读到的唯一线索是纸面右上角那个人可读的印刷体单号。所以：
    `qr.find_form_nos()` 提候选 → 查 `print_sheet` → **命中才算数**。

    返回 `state`：
      none      纸上没读到单号（老单、外协单、手写单都走这条路）
      unknown   读到了，但库里没有这个号（不是我们印的，或读错了）
      ambiguous 一页上出现多个有效单号 —— 不猜，交人工
      printed   我们印的、还没报工
      reported  已经扫码报工 → **识别结果只能留档，不能再入账**
      void      已作废（重印替换/手工作废）→ 同样不能再入账
    """
    cands = qr.find_form_nos(lines)
    if not cands:
        return {"state": "none", "form_no": "", "candidates": []}
    forms = list(db.scalars(select(PrintSheet).where(PrintSheet.form_no.in_(cands))))
    if not forms:
        return {"state": "unknown", "form_no": "", "candidates": cands}
    if len(forms) > 1:
        # 一页纸只能是一张单。读出一个以上有效单号，说明至少有一个是错的，
        # 挑错就是把工时记到别人头上 —— 不认，留给人看。
        return {
            "state": "ambiguous",
            "form_no": "",
            "candidates": sorted(f.form_no for f in forms),
        }
    fs = forms[0]
    return {"state": fs.status, "form_no": fs.form_no, "candidates": cands, "form": fs}


def _period_for(db: Session, day: dt.date) -> Period | None:
    """按报工日期找核算期（找不到就用最近一个未锁定的）。"""
    ym = f"{day:%Y-%m}"
    period = db.scalar(select(Period).where(Period.ym == ym))
    if period is not None:
        return period
    return db.scalar(
        select(Period).where(Period.status == "open").order_by(Period.ym.desc()).limit(1)
    )


def worker_index(db: Session) -> dict[str, Worker]:
    """姓名/别名/工号 → 人员。扫码报工的名字是计划员从下拉选的，

    但下拉允许手输（纸上有新来的临时工）。手输错一个字，这笔工时就会
    掉进「未匹配」，工资发不出去还没人发现 —— 所以这里把别名和工号也认上，
    能对上一个人就对上，实在对不上才留空并告警。
    """
    out: dict[str, Worker] = {}
    for w in db.scalars(select(Worker)):
        if w.name:
            out[w.name.strip()] = w
    for w in db.scalars(select(Worker)):
        for alias in (w.aliases or "").split(","):
            alias = alias.strip()
            if alias:
                out.setdefault(alias, w)
        if (w.employee_no or "").strip():
            out.setdefault(w.employee_no.strip(), w)
    return out


# ---------------------------------------------------------------- 生成预印单
def generate(
    db: Session,
    task_id: int,
    *,
    parts: list[str] | None = None,
    force: bool = False,
    by: str = "",
) -> dict:
    """给一个生产任务印一套路线单：**一个件名一张纸**。

    颗粒度必须跟着纸走：车间是拿着一张纸去领一件活、干完签字、随件流转的。
    把整个任务印成一本、工人签字时找不到自己那一格，这套纸就没人用了。

    `force=False` 时对已打印未报工的件名直接复用（重印会作废旧纸，
    工人手里那张就不是「有效凭证」了，所以默认不重印）。
    """
    built = PC.build_cards(db, task_id=task_id)
    cards = [c for c in (built.get("cards") or []) if c.get("steps")]
    return create_from_cards(db, task_id, cards, parts=parts, force=force, by=by)


def create_from_cards(
    db: Session,
    task_id: int | None,
    cards: list[dict],
    *,
    parts: list[str] | None = None,
    force: bool = False,
    by: str = "",
) -> dict:
    """按**已构建好的卡片**建预印单，返回 `{created, reused, qr_map}`。

    为什么要把这一步单独拆出来：工艺卡页面打印时手上已经有一整本卡片了，
    再让 `generate` 重新 `build_cards` 一次既慢又可能拿到不一样的编号；
    更要紧的是**两条打印入口必须共用同一批单号** —— 各建一套的话，
    同一张纸会有两个号，扫码和识别就对不上了。

    `qr_map` 是 `{件名: (单号, token)}`，直接喂给 `card_print.render_pdf`。
    """
    cards = [c for c in cards if c.get("steps")]
    if parts:
        want = {p.strip() for p in parts if p and p.strip()}
        cards = [c for c in cards if c.get("part") in want]
    if not cards:
        return {"created": [], "reused": [], "qr_map": {}, "message": "没有可打印的件名"}

    stmt = select(PrintSheet).where(PrintSheet.status.in_(("printed", "reported")))
    if task_id:
        stmt = stmt.where(PrintSheet.task_id == task_id)
    else:
        stmt = stmt.where(PrintSheet.task_id.is_(None))
    # 一个件名可能同时存在多张单（历史重印留下的）。不能按查询顺序随便挑一张：
    # 挑到「已报工」的才是对的，挑到「待报工」的会让同一件活被报第二遍。
    # 同时把混在里面的待报工重复单作废 —— 它们是能扫进来的第二张纸。
    all_forms = list(db.scalars(stmt))
    existing: dict[str, PrintSheet] = {}
    dup_printed: dict[str, list[PrintSheet]] = {}
    for f in all_forms:
        cur = existing.get(f.part_name)
        if cur is None or (cur.status != "reported" and f.status == "reported"):
            existing[f.part_name] = f
        if f.status == "printed":
            dup_printed.setdefault(f.part_name, []).append(f)

    created: list[dict] = []
    reused: list[dict] = []
    already: list[dict] = []
    qr_map: dict[str, tuple[str, str]] = {}
    for card in cards:
        part = card.get("part") or ""
        old = existing.get(part)
        if old is not None and old.status == "reported":
            # 这一件已经报过工了。绝不能再发一个新号：
            # 新号能扫、能提交，同一批活就被算第二遍工资。
            # 只把旧号还回去 —— 重印出来的纸上印的还是那个号，
            # 谁拿它去扫都会被「已报工」拦住。
            for d in dup_printed.get(part, []):
                if d.id != old.id:
                    d.status = "void"
                    d.note = (d.note + " / 同件已报工，自动作废").strip(" /")
            already.append({"form_no": old.form_no, "part": part})
            qr_map[part] = (old.form_no, old.token)
            continue
        if old is not None and not force:
            reused.append({"form_no": old.form_no, "part": part})
            qr_map[part] = (old.form_no, old.token)
            continue
        if old is not None and force:
            # 重印即作废旧纸：否则工位上会同时存在两张都能报工的纸
            old.status = "void"
            old.note = (old.note + " / 重印作废").strip(" /")

        qty = float(card.get("print_qty") or 0) or float(card.get("qty_per_unit") or 1)
        fs = _insert_form(db, task_id=task_id, card=card, qty=qty, by=by)
        qr_map[part] = (fs.form_no, fs.token)
        created.append(
            {"form_no": fs.form_no, "part": part, "part_no": fs.part_no,
             "lines": len(card.get("steps") or [])}
        )
    db.commit()
    msg = f"新建 {len(created)} 张，复用 {len(reused)} 张"
    if already:
        msg += f"，{len(already)} 张已报工（沿用原单号，不能重复报）"
    return {
        "created": created,
        "reused": reused,
        "already_reported": already,
        "qr_map": qr_map,
        "task_id": task_id,
        "message": msg,
    }


# ---------------------------------------------------------------- 打印
def render_pdf(db: Session, forms: list[PrintSheet], *, dpi: int = 300) -> bytes:
    """把若干张预印单合成一个 PDF（一单一份，工序多的单自己续页）。"""
    pages = []
    for fs in forms:
        card = {
            "product": fs.product_name,
            "part": fs.part_name,
            "code": fs.part_no,
            "material": "",
            "level_name": "",
            "parent_code": "",
            "qty_per_unit": 1,
            "print_qty": fs.part_qty,
            "published_by": fs.printed_by,
            "steps": [
                {
                    "process_name": ln.process_name,
                    "unit_hours": ln.unit_hours,
                    "is_key": ln.is_key,
                    "seq": ln.seq,
                }
                for ln in fs.lines
            ],
            "setup_hours": 0.0,
        }
        values = CP.card_values(
            card,
            task={
                "qty": fs.qty_plan,
                "batch_no": fs.batch_no,
                "dispatcher": "",
            },
            print_date=fs.printed_at.date() if fs.printed_at else None,
        )
        rendered = form.render_pages(values, dpi=dpi)
        for page in rendered:
            # 每页都贴：续页也可能被单独拿到工位上，扫哪页都能报同一张单
            qr_stamp.stamp(page, fs.form_no, fs.token, dpi)
            pages.append(page)
    if not pages:
        pages = [form.render(blank=True, dpi=dpi)]
    buf = io.BytesIO()
    pages[0].save(buf, "PDF", resolution=float(dpi), save_all=True, append_images=pages[1:])
    return buf.getvalue()


# ---------------------------------------------------------------- 扫码
def scan(db: Session, code: str) -> dict:
    """扫一个码 → 返回这张单可以填什么。

    只回答「哪张单、哪一行」，定额与件数一律从**快照**取（不是当前工艺）：
    纸已经发出去了，按新工艺算出来的工时跟工人手里那张纸对不上，
    当场就会吵起来。
    """
    parsed = qr.parse(code)
    if not parsed.get("ok"):
        return {"ok": False, "reason": parsed.get("reason") or "码无效"}

    fs = db.scalar(select(PrintSheet).where(PrintSheet.form_no == parsed["form_no"]))
    if fs is None:
        return {"ok": False, "reason": f"找不到单号 {parsed['form_no']}，可能是别的车间的单"}
    if fs.token and parsed.get("token") and fs.token != parsed["token"]:
        return {"ok": False, "reason": "码与单号不匹配（这张纸可能被复印或改动过）"}
    if fs.status == "void":
        return {"ok": False, "reason": f"单号 {fs.form_no} 已作废，请用新印的单据报工"}
    if fs.status == "reported":
        return {
            "ok": False,
            "reason": f"单号 {fs.form_no} 已在 {fs.reported_at:%m-%d %H:%M} 由 {fs.reported_by or '他人'} 报工",
            "code": "ALREADY_REPORTED",
            "form_no": fs.form_no,
        }

    lines = [
        {
            "row_no": ln.row_no,
            "seq": ln.seq,
            "process_name": ln.process_name,
            "process_code": ln.process_code,
            "category": ln.category,
            "work_center": ln.work_center,
            "unit_hours": ln.unit_hours,
            "qty_plan": ln.qty_plan,
            "total_hours": ln.total_hours,
            "is_key": ln.is_key,
            "worker_name": ln.worker_name,
        }
        for ln in fs.lines
    ]
    focus = parsed.get("row_no")
    return {
        "ok": True,
        "form": {
            "form_no": fs.form_no,
            "task_id": fs.task_id,
            "product_name": fs.product_name,
            "batch_no": fs.batch_no,
            "part_name": fs.part_name,
            "part_no": fs.part_no,
            "qty_plan": fs.qty_plan,
            "part_qty": fs.part_qty,
            "total_hours": fs.total_hours,
            "printed_at": fs.printed_at.isoformat(timespec="minutes") if fs.printed_at else None,
            "printed_by": fs.printed_by,
        },
        "lines": lines,
        # 扫行码时把这一行顶到最前面：计划员是「拿着纸对着屏幕核一行」，
        # 不该让他自己在一屏里找
        "focus_row": focus,
    }


# ---------------------------------------------------------------- 提交
def submit(
    db: Session,
    form_no: str,
    rows: list[dict],
    *,
    by: str = "",
    sign_verified: bool = False,
    report_date: dt.date | None = None,
    note: str = "",
) -> dict:
    """提交报工：把这张单的数量写成 work_item。

    `rows` 每项：row_no / qty_done / qualified / rework / scrap / worker_name /
    actual_hours / adjust_reason
    """
    fs = db.scalar(select(PrintSheet).where(PrintSheet.form_no == form_no))
    if fs is None:
        return {"ok": False, "reason": f"找不到单号 {form_no}"}
    if fs.status == "void":
        return {"ok": False, "reason": "这张单已作废"}
    if fs.status == "reported":
        return {"ok": False, "reason": f"{form_no} 已报过工，不能重复提交"}
    if not sign_verified:
        # 纸质签字是这笔工时唯一的凭证。界面上那个勾不是装饰：
        # 没勾就入账，等于系统自己承认「没看纸就发钱」，出事时无据可查。
        return {"ok": False, "reason": "请先确认纸单上操作者已签字核对"}

    if fs.task_id:
        # 已结案/已取消的订单不再收报工：这时工资和任务实报都已经结了，
        # 再进一笔，账就与已经发出去的钱对不上。要补必须先改任务状态。
        from ..models import WorkTask

        task = db.get(WorkTask, fs.task_id)
        if task is not None and task.status in ("closed", "cancelled"):
            label = "已结案" if task.status == "closed" else "已取消"
            return {
                "ok": False,
                "reason": f"生产任务 {task.task_no} {label}，不能再报工；请先恢复任务状态",
            }

    # 同一张纸的另一条通道：如果这张纸已经被识别入账，扫码再报就是第二份工资。
    # 识别侧会把预印单号写进 sheet.print_form_no（见 bind_print_form），
    # 所以这里查得到就是真撞车，查不到说明这张纸没走过识别。
    twin = db.scalar(
        select(Sheet).where(
            Sheet.print_form_no == fs.form_no,
            Sheet.status.in_(("verified", "recognized")),
        )
    )
    if twin is not None:
        if twin.status == "verified":
            return {
                "ok": False,
                "reason": f"这张纸（{fs.form_no}）已经通过识别入账，不能再扫码报工",
            }
        return {
            "ok": False,
            "reason": f"这张纸（{fs.form_no}）的识别结果还在待核对，请先去核对页处理，避免两边各算一次",
        }

    by_row = {ln.row_no: ln for ln in fs.lines}
    today = report_date or dt.date.today()
    period = _period_for(db, today)
    if period is None:
        return {"ok": False, "reason": "没有可用的核算期，请先建立本月核算期"}

    # 报工月份的核算期不存在时会退回「最近一个未锁定期间」。这个兜底是必要的
    # （月初还没建新期，不能让人卡住干不了活），但绝不能悄悄发生 ——
    # 12 月的活记到 11 月，两个月的工资都是错的，而且没人会想到去查。
    warnings: list[str] = []
    want_ym = f"{today:%Y-%m}"
    if period.ym != want_ym:
        month = db.scalar(select(Period).where(Period.ym == want_ym))
        if month is not None and month.status == "locked":
            return {
                "ok": False,
                "reason": f"{want_ym} 的核算期已锁账，不能再报工；请解锁或把报工日期改到未锁月份",
            }
        warnings.append(
            f"报工日期属于 {want_ym}，但该月还没有核算期，本次工时记到了 {period.ym}"
        )
    if period.status == "locked":
        return {"ok": False, "reason": f"{period.ym} 已锁账，不能再报工"}
    if today > dt.date.today():
        # 不阻断（补录、时区、跨零点都可能合法），但一定要说一声：
        # 年份敲错一位，整月工资就会记到另一年，事后没人想得到去查
        warnings.append(f"报工日期 {today} 在今天之后，请确认不是敲错了年份/月份")

    workers = worker_index(db)
    created_ids: list[int] = []
    total = 0.0

    # 先做一遍校验再写库：不能「前两行已经入库、第三行才报错」，
    # 那样一次提交只写进去一半，账面上多出几张没人认领的工时。
    plan: list[dict] = []
    errors: list[str] = []
    for row in rows:
        row_no = int(row.get("row_no") or 0)
        ln = by_row.get(row_no)
        if ln is None:
            continue
        qualified = float(row.get("qualified") or 0)
        rework = float(row.get("rework") or 0)
        scrap = float(row.get("scrap") or 0)
        done = row.get("qty_done")
        done = float(done) if done not in (None, "") else qualified + rework + scrap
        if qualified < 0 or rework < 0 or scrap < 0 or done < 0:
            errors.append(f"第 {row_no} 行：数量不能为负数")
            continue
        if qualified <= 0 and rework <= 0 and scrap <= 0 and done <= 0:
            # 整行没填 = 这道工序本批没干，跳过；报工单上十行里空着几行是常态
            continue
        total_qty = qualified + rework + scrap
        # done=0 也不能放过：完成数 0、合格 3 是自相矛盾的一张单
        if total_qty > done + 0.001:
            errors.append(
                f"第 {row_no} 行：合格+返修+废品（{total_qty:g}）超过完成数（{done:g}）"
            )
        if done and total_qty < done - 0.001:
            warnings.append(
                f"第 {row_no} 行：完成数 {done:g}，但合格+返修+废品只有 {total_qty:g}，"
                f"有 {done - total_qty:g} 件去向没写"
            )
        if ln.qty_plan and qualified > float(ln.qty_plan) + 0.001:
            warnings.append(
                f"第 {row_no} 行：合格数 {qualified:g} 超过计划件数 {float(ln.qty_plan):g}，请确认"
            )
        # 返修/报废是「和计划不一样」的地方，必须留下原因；否则质量分析永远没有依据
        if (rework > 0 or scrap > 0) and not (row.get("adjust_reason") or "").strip():
            errors.append(f"第 {row_no} 行：有返修/报废，必须填写调整原因")
        worker_name = (row.get("worker_name") or ln.worker_name or "").strip()
        if not worker_name:
            # 没有人的工时算不出工资 —— 这里不拒绝（历史补录可能先记账后补人），
            # 但必须说清白算了一笔：界面会把它显示成「未匹配」，
            # 工资表上那个人拿到的就是 0 元，事后极难追。
            warnings.append(f"第 {row_no} 行：没填操作者，这部分工时进不了任何人的工资")
        elif worker_name not in workers:
            warnings.append(
                f"第 {row_no} 行：操作者「{worker_name}」不在人员名单里，"
                f"这笔工时不会算到任何人头上"
            )
        plan.append(
            {
                "row_no": row_no,
                "ln": ln,
                "qualified": qualified,
                "rework": rework,
                "scrap": scrap,
                "done": done,
                "worker_name": worker_name,
                "row": row,
            }
        )

    if errors:
        return {"ok": False, "reason": "；".join(errors[:4]), "errors": errors}
    if not plan:
        return {"ok": False, "reason": "一行数量都没填，没有可报工的内容"}

    # 先原子地把单子从 printed 翻到 reported，再写明细。
    # 两个计划员同时扫同一张纸（或一人点了两次）时，只有第一条 UPDATE 能命中；
    # 另一条拿到 rowcount=0 直接退出 —— 否则两边各写一份工时，工资就发两遍。
    now = dt.datetime.now()
    flipped = db.execute(
        update(PrintSheet)
        .where(PrintSheet.id == fs.id, PrintSheet.status == "printed")
        .values(status="reported", reported_at=now, reported_by=by)
    )
    if flipped.rowcount != 1:
        db.rollback()
        return {"ok": False, "reason": f"{form_no} 已被其他人报工，请刷新后核对"}

    for p in plan:
        ln = p["ln"]
        hours = round(p["qualified"] * float(ln.unit_hours or 0), 4)
        worker_name = p["worker_name"]
        worker = workers.get(worker_name) if worker_name else None
        item = WorkItem(
            sheet_id=None,
            seq=p["row_no"],
            product_name=fs.product_name,
            batch_no=fs.batch_no,
            part_name=fs.part_name,
            process=ln.process_name,
            process_std=ln.process_name,
            process_code=ln.process_code,
            qualified_qty=p["qualified"],
            rework_qty=p["rework"],
            scrap_qty=p["scrap"],
            unit_hours=ln.unit_hours,
            total_hours=hours,
            finish_date=today,
            inspector=(p["row"].get("inspector") or "").strip()[:32],
            worker_id=worker.id if worker else None,
            worker_raw=worker_name,
            source="scan",
            print_form_no=fs.form_no,
            period_id=period.id,
            task_id=fs.task_id,
            qty_done=p["done"] or None,
            reported_at=dt.datetime.now(),
            reported_by=by,
            hours_actual=(
                float(p["row"]["actual_hours"])
                if p["row"].get("actual_hours") not in (None, "")
                else None
            ),
            sign_verified=bool(sign_verified),
            adjust_reason=(p["row"].get("adjust_reason") or "").strip()[:64],
            remark=(note or "")[:255],
        )
        db.add(item)
        db.flush()
        created_ids.append(item.id)
        total += hours

    if not created_ids:
        return {"ok": False, "reason": "一行数量都没填，没有可报工的内容"}

    # 状态已经在上面原子翻过了，这里只补备注
    if note:
        fs.note = (fs.note + " / " + note).strip(" /")
    db.add(
        AuditLog(
            entity="print_sheet",
            entity_id=fs.id,
            sheet_id=None,
            seq=None,
            field="report",
            old_value="printed",
            new_value=f"reported {len(created_ids)} 行 / {round(total, 4)}h",
            user=by or "system",
        )
    )
    db.commit()

    # 任务上的「实报工时」是物化字段（任务列表要用，不能每行实时算）。
    # 扫码报工改了工时，就必须顺手把它重算一次 —— 否则任务页停在报工前的数，
    # 车间会以为这单还没干活。
    if fs.task_id:
        try:
            from ..models import WorkTask
            from .tasks import refresh_task

            task = db.get(WorkTask, fs.task_id)
            if task is not None:
                refresh_task(db, task)
                db.commit()
        except Exception:  # pragma: no cover - 刷新失败不该让报工回滚
            db.rollback()
            warnings.append("任务实报工时未能刷新，下次在任务页刷新即可")

    return {
        "ok": True,
        "form_no": fs.form_no,
        "items": len(created_ids),
        "hours": round(total, 4),
        "period": period.ym,
        "warnings": warnings,
    }


def undo(db: Session, form_no: str, *, by: str = "", reason: str = "") -> dict:
    """撤销报工：删掉这张单产生的明细，单据退回「待报工」。

    为什么要能撤：计划员点错数量、扫错单是常事。不能撤的结果是
    「将错就错」或者「偷偷让他手工删数据库」，两种都比有撤销危险。

    为什么必须写原因：撤销是**删钱**的动作，账单上会凭空少几百小时。
    只留一条「undo」的审计等于什么都没留 —— 一个月后没人说得清
    这笔工资为什么少了，是不是有人动过。
    """
    fs = db.scalar(select(PrintSheet).where(PrintSheet.form_no == form_no))
    if fs is None:
        return {"ok": False, "reason": f"找不到单号 {form_no}"}
    if fs.status != "reported":
        return {"ok": False, "reason": f"{form_no} 当前不是已报工状态"}
    reason = (reason or "").strip()
    if len(reason) < 2:
        return {"ok": False, "reason": "请填写撤销原因（至少 2 个字），否则事后查不出这笔工资为什么退回去了"}
    items = list(db.scalars(select(WorkItem).where(WorkItem.print_form_no == form_no)))
    # 锁账之后不能再撤：工资表已经按这个月的工时发下去了，
    # 悄悄删掉几行，工资表和账就再也对不上。要改先解锁。
    locked = sorted(
        {
            p.ym
            for p in db.scalars(
                select(Period).where(
                    Period.id.in_({it.period_id for it in items if it.period_id}),
                    Period.status == "locked",
                )
            )
        }
    )
    if locked:
        return {
            "ok": False,
            "reason": f"报工记在已锁账的 {'、'.join(locked)}，不能撤销；请先解锁核算期",
        }
    for it in items:
        db.delete(it)
    fs.status = "printed"
    fs.reported_at = None
    fs.reported_by = ""
    fs.note = (fs.note + f" / 撤销：{reason}").strip(" /")[:255]
    # 撤掉多少工时先算好再写审计：行已经删了，回头再算就是 0
    removed_hours = round(sum(float(i.total_hours or 0) for i in items), 4)
    db.add(
        AuditLog(
            entity="print_sheet",
            entity_id=fs.id,
            sheet_id=None,
            seq=None,
            field="undo",
            old_value=f"reported（{len(items)} 行 / {removed_hours}h）",
            new_value=f"printed / 原因：{reason}"[:255],
            user=by or "system",
        )
    )
    db.commit()
    # 撤掉的工时也要从任务实报里退出去，口径与报工同一条路
    if fs.task_id:
        try:
            from ..models import WorkTask
            from .tasks import refresh_task

            task = db.get(WorkTask, fs.task_id)
            if task is not None:
                refresh_task(db, task)
                db.commit()
        except Exception:  # pragma: no cover
            db.rollback()
    return {"ok": True, "form_no": form_no, "removed": len(items)}


def void_form(db: Session, form_no: str, *, by: str = "", reason: str = "") -> dict:
    """手工作废一张预印单（`status=void`），纸上的码立刻失效。

    为什么必须有这个入口：零件报废不做了、单子印错件名了，那张纸还在工人手里。
    没有作废，它会永远停在「待报工」—— 每次「打印全部待报工单」都被重新印出来，
    月底对账时还得有人一条条确认「这些是真没干还是漏了」。

    已报工的单**不能一步作废**：作废会连带把工时抹掉，那必须走撤销
    （撤销要写原因、要过锁账），两道动作分开，账才查得清。
    """
    fs = db.scalar(select(PrintSheet).where(PrintSheet.form_no == form_no))
    if fs is None:
        return {"ok": False, "reason": f"找不到单号 {form_no}"}
    if fs.status == "void":
        return {"ok": False, "reason": f"{form_no} 已经是作废状态"}
    if fs.status == "reported":
        return {
            "ok": False,
            "reason": f"{form_no} 已报工，不能直接作废；请先撤销报工（撤销后纸单回到待报工），再作废",
        }
    reason = (reason or "").strip()
    if len(reason) < 2:
        return {"ok": False, "reason": "请填写作废原因（至少 2 个字）"}
    fs.status = "void"
    fs.note = (fs.note + f" / 作废：{reason}").strip(" /")[:255]
    db.add(
        AuditLog(
            entity="print_sheet",
            entity_id=fs.id,
            sheet_id=None,
            seq=None,
            field="void",
            old_value="printed",
            new_value=f"void / 原因：{reason}"[:255],
            user=by or "system",
        )
    )
    db.commit()
    return {"ok": True, "form_no": form_no}


def recent(db: Session, *, limit: int = 30, task_id: int | None = None) -> dict:
    stmt = select(PrintSheet).order_by(PrintSheet.printed_at.desc()).limit(limit)
    if task_id:
        stmt = stmt.where(PrintSheet.task_id == task_id)
    forms = list(db.scalars(stmt))
    return {
        "items": [
            {
                "id": f.id,
                "form_no": f.form_no,
                "task_id": f.task_id,
                "product_name": f.product_name,
                "part_name": f.part_name,
                "part_no": f.part_no,
                "qty_plan": f.qty_plan,
                "part_qty": f.part_qty,
                "total_hours": f.total_hours,
                "status": f.status,
                "note": f.note or "",
                "printed_at": f.printed_at.isoformat(timespec="minutes") if f.printed_at else None,
                "printed_by": f.printed_by,
                "reported_at": f.reported_at.isoformat(timespec="minutes") if f.reported_at else None,
                "reported_by": f.reported_by,
            }
            for f in forms
        ]
    }


def ledger(
    db: Session,
    *,
    ym: str = "",
    keyword: str = "",
    status: str = "",
    page: int = 1,
    size: int = 20,
) -> dict:
    """报工台账的「扫码报工」一侧：**以预印单为主键**列出。

    为什么不把扫码单硬塞进 `sheet` 表：两者没有任何共同的可编辑内容
    （识别单有照片、OCR 原文、逐格复核；扫码单是工序快照 + 签字核对）。
    合表只会让两套流程互相拖累。台账层面合起来就够了 ——
    月底核对要回答的是「这个月谁报了什么」，不是「它存在哪张表」。

    口径：报工过的按**明细所属核算期**归类；还没报工的按**打印月份**列出来，
    否则「这个月印了但一直没报」的单在台账上完全看不见。
    """
    stmt = select(PrintSheet)
    if ym:
        p = db.scalar(select(Period).where(Period.ym == ym))
        if p is None:
            return {"total": 0, "page": page, "size": size, "items": []}
        reported_in = select(WorkItem.print_form_no).where(WorkItem.period_id == p.id)
        printed_in = func.strftime("%Y-%m", PrintSheet.printed_at) == ym
        stmt = stmt.where(or_(PrintSheet.form_no.in_(reported_in), printed_in))
    if status:
        stmt = stmt.where(PrintSheet.status.in_([s for s in status.split(",") if s]))
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                PrintSheet.form_no.like(like),
                PrintSheet.part_name.like(like),
                PrintSheet.part_no.like(like),
                PrintSheet.product_name.like(like),
                PrintSheet.reported_by.like(like),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    forms = list(
        db.scalars(
            stmt.order_by(PrintSheet.printed_at.desc()).offset((page - 1) * size).limit(size)
        )
    )
    nos = [f.form_no for f in forms]
    stats: dict[str, dict] = {}
    if nos:
        rows = db.execute(
            select(
                WorkItem.print_form_no,
                func.count().label("n"),
                func.sum(WorkItem.total_hours).label("h"),
            )
            .where(WorkItem.print_form_no.in_(nos))
            .group_by(WorkItem.print_form_no)
        ).all()
        stats = {r[0]: {"items": int(r[1] or 0), "hours": round(float(r[2] or 0), 4)} for r in rows}
    tasks: dict[int, str] = {}
    task_ids = {f.task_id for f in forms if f.task_id}
    if task_ids:
        from ..models import WorkTask

        for t in db.scalars(select(WorkTask).where(WorkTask.id.in_(task_ids))):
            tasks[t.id] = t.task_no
    items = []
    for f in forms:
        st = stats.get(f.form_no) or {"items": 0, "hours": 0.0}
        items.append(
            {
                "form_no": f.form_no,
                "task_id": f.task_id,
                "task_no": tasks.get(f.task_id or 0, ""),
                "product_name": f.product_name,
                "part_name": f.part_name,
                "part_no": f.part_no,
                "batch_no": f.batch_no,
                "qty_plan": f.qty_plan,
                "part_qty": f.part_qty,
                # 报过工看实报；没报工看快照定额（「这张纸值多少工时」）
                "hours": st["hours"] if st["items"] else round(float(f.total_hours or 0), 4),
                "item_count": st["items"],
                "status": f.status,
                "note": f.note or "",
                "printed_at": f.printed_at.isoformat(timespec="minutes") if f.printed_at else None,
                "printed_by": f.printed_by,
                "reported_at": f.reported_at.isoformat(timespec="minutes") if f.reported_at else None,
                "reported_by": f.reported_by,
            }
        )
    return {"total": total, "page": page, "size": size, "items": items}


def form_detail(db: Session, form_no: str) -> dict:
    """一张预印单的明细（台账只读页用）：纸上有什么 + 实际报了什么人多少工时。"""
    fs = db.scalar(select(PrintSheet).where(PrintSheet.form_no == form_no))
    if fs is None:
        return {"ok": False, "reason": f"找不到单号 {form_no}"}
    items = list(
        db.scalars(
            select(WorkItem)
            .where(WorkItem.print_form_no == form_no)
            .order_by(WorkItem.seq)
        )
    )
    return {
        "ok": True,
        "form": {
            "form_no": fs.form_no,
            "task_id": fs.task_id,
            "product_name": fs.product_name,
            "batch_no": fs.batch_no,
            "part_name": fs.part_name,
            "part_no": fs.part_no,
            "qty_plan": fs.qty_plan,
            "part_qty": fs.part_qty,
            "total_hours": fs.total_hours,
            "status": fs.status,
            "note": fs.note or "",
            "printed_at": fs.printed_at.isoformat(timespec="minutes") if fs.printed_at else None,
            "printed_by": fs.printed_by,
            "reported_at": fs.reported_at.isoformat(timespec="minutes") if fs.reported_at else None,
            "reported_by": fs.reported_by,
        },
        "lines": [
            {
                "row_no": ln.row_no,
                "process_name": ln.process_name,
                "unit_hours": ln.unit_hours,
                "qty_plan": ln.qty_plan,
                "total_hours": ln.total_hours,
                "is_key": ln.is_key,
            }
            for ln in fs.lines
        ],
        "items": [
            {
                "seq": it.seq,
                "process": it.process,
                "worker_name": it.worker_raw,
                "qualified_qty": it.qualified_qty,
                "rework_qty": it.rework_qty,
                "scrap_qty": it.scrap_qty,
                "qty_done": it.qty_done,
                "unit_hours": it.unit_hours,
                "total_hours": it.total_hours,
                "adjust_reason": it.adjust_reason,
                "inspector": it.inspector,
                "finish_date": it.finish_date.isoformat() if it.finish_date else None,
                "sign_verified": it.sign_verified,
            }
            for it in items
        ],
    }
