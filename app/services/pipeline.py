"""入库与识别流水线：上传 -> 预处理 -> OCR -> 结构定位 -> 抽取 -> 校验 -> 落库。

C1 之后这里的职责分成了两半，**别再把它们合回一个进程**：

* `save_upload` / `enqueue` 在 **Web 进程**里跑。它们只碰数据库，不加载
  图像栈，所以 Web 才能开多进程（每进程 ~120MB，而不是 400MB）。
* `recognize_image` 在**独立的 OCR worker 进程**里跑（`scripts/ocr_worker.py`）。
  PaddleOCR 只在这个进程里加载一份。

队列就是 `image.status`：pending → processing → done/failed。
`claim_next()` 是唯一的领取入口，多进程下靠它保证同一张图不会被跑两遍。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import ALLOWED_EXT, OCR_MAX_ATTEMPTS, PROCESSED_DIR, THUMB_DIR, UPLOAD_DIR
from ..db import SessionLocal
from ..models import AuditLog, Batch, Image, Period, Sheet, WorkItem
from ..domain.rules import compute_total_hours, summarize_flags, validate_sheet

log = logging.getLogger("worktime.pipeline")

# 一张图最多被领取几次。毒图（超大图、引擎段错误）每跑一次就可能把整个进程带走，
# 没有上限时会陷入「启动 → 捞回来 → 崩 → 启动」，服务永远起不来。
MAX_ATTEMPTS = OCR_MAX_ATTEMPTS


class DuplicateImage(ValueError):
    def __init__(self, existing_id: int):
        super().__init__(f"图片已存在（id={existing_id}）")
        self.existing_id = existing_id


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save_upload(db: Session, batch_id: int, filename: str, data: bytes) -> Image:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_EXT:
        raise ValueError(f"不支持的图片格式：{ext}")

    digest = _sha256(data)
    existing = db.scalar(select(Image).where(Image.sha256 == digest))
    if existing:
        raise DuplicateImage(existing.id)

    safe_name = f"{digest[:16]}{ext}"
    path = Path(UPLOAD_DIR) / safe_name
    path.write_bytes(data)

    img = Image(
        batch_id=batch_id,
        filename=filename,
        path=str(path),
        sha256=digest,
        size_bytes=len(data),
        status="pending",
    )
    try:
        # 延迟导入：PIL/numpy/cv2 只有在真正读图时才需要。放在模块顶部会让
        # 每个 Web 进程白白多背一份图像栈（多进程部署时是 ×N）。
        from PIL import Image as PILImage

        with PILImage.open(path) as im:
            img.width, img.height = im.size
    except Exception:
        pass

    db.add(img)
    db.commit()
    db.refresh(img)
    return img


def _dict_bundle(db: Session) -> dict:
    from .dicts import dict_bundle

    return dict_bundle(db)


def recognize_image(image_id: int, keeps: dict | None = None) -> None:
    """识别一张图并落库。**只在 OCR worker 进程里调用**（见 scripts/ocr_worker.py）。

    自带独立会话，避免跨线程/跨任务共享 SQLAlchemy Session。
    keeps：重新识别时要保留的人工修改（表头 / 各明细行的字段值）；
    不传时从 `image.keeps_json` 读 —— 发起重识别的是 Web 进程，执行的是
    OCR 进程，参数只能通过数据库传递。
    """
    db = SessionLocal()
    try:
        img = db.get(Image, image_id)
        if not img:
            return
        if img.status == "done" and keeps is None and not (img.keeps_json or "").strip():
            return
        if keeps is None:
            keeps = _load_keeps(img)

        # 已被 claim_next() 领取过的（status=processing 且本次就是它）不再重复计数；
        # 脚本/测试直接调用时补上领取动作，保证 attempts 口径一致。
        if img.status != "processing":
            img.status = "processing"
            img.attempts = int(img.attempts or 0) + 1
            img.claimed_at = dt.datetime.now()
        img.error = ""
        db.commit()

        # 图像栈延迟到真正要用时再导入：Web 进程只是 enqueue，不需要 numpy/cv2。
        import numpy as np
        from PIL import Image as PILImage

        from ..ocr import engine as ocr_engine
        from ..ocr import preprocess
        from ..ocr.extract import extract_sheet

        batch = db.get(Batch, img.batch_id)
        period = db.get(Period, batch.period_id) if batch else None
        ym = period.ym if period else ""

        stem = Path(img.path).stem
        processed_path = Path(PROCESSED_DIR) / f"{stem}.png"
        thumb_path = Path(THUMB_DIR) / f"{stem}.jpg"
        mask_path = Path(PROCESSED_DIR) / f"{stem}_red.png"

        meta = preprocess.process(
            img.path,
            processed_path=processed_path,
            thumb_path=thumb_path,
            mask_path=mask_path,
        )
        img.processed_path = str(processed_path)
        img.thumb_path = str(thumb_path)
        img.skew_angle = meta["skew_angle"]
        img.red_ratio = meta["red_ratio"]
        db.commit()

        with PILImage.open(processed_path) as pim:
            page_w, page_h = pim.size
            gray = np.asarray(pim.convert("L"), dtype=np.uint8)

        lines = ocr_engine.recognize(str(processed_path), page_w, page_h)
        bundle = _dict_bundle(db)
        extracted = extract_sheet(
            lines,
            page_w,
            page_h,
            ym,
            bundle,
            gray=gray,
        )
        items = extracted["items"]
        # 人工改过的字段先盖回去，再走校验，否则告警会按识别值来算
        items, kept = _apply_keeps(items, keeps)
        header = _apply_header_keeps(extracted["header"], (keeps or {}).get("header"))
        items, sheet_codes = validate_sheet(
            items,
            ym,
            columns=extracted["layout"].get("columns"),
            std_hours=bundle.get("std_hours"),
            routes=bundle.get("routes"),
        )

        sheet = db.scalar(select(Sheet).where(Sheet.image_id == img.id))
        if sheet is None:
            sheet = Sheet(image_id=img.id, period_id=period.id if period else 0)
            db.add(sheet)
            db.flush()

        sheet.header_json = json.dumps(header, ensure_ascii=False)
        sheet.ocr_raw_json = json.dumps(lines, ensure_ascii=False)
        sheet.layout_json = json.dumps(
            {
                "rows_count": extracted["layout"]["rows_count"],
                "header_index": extracted["layout"]["header_index"],
                "columns": extracted["layout"]["columns"],
                "probe_count": extracted["layout"].get("probe_count", 0),
            },
            ensure_ascii=False,
        )
        sheet.page_w, sheet.page_h = page_w, page_h
        sheet.form_type = "加工路线单"
        flag_total = summarize_flags(items)["total"]
        # 重新识别会把单据打回待核对：这一步必须留痕，否则「谁把已确认的单据退回去了」无从追查
        if sheet.id and sheet.status not in (None, "", "recognized"):
            db.add(
                AuditLog(
                    entity="sheet",
                    entity_id=sheet.id,
                    sheet_id=sheet.id,
                    field="status",
                    old_value=sheet.status or "",
                    new_value="recognized",
                    user="system",
                )
            )
        sheet.status = "recognized"
        sheet.flag_count = flag_total

        # ---- 两条通道互认：这张纸是我们印的吗？
        # 纸面右上角印着人可读的单号，识别读它 → 反查预印单。
        # 命中「已扫码报工」或「已作废」的纸，识别结果**只能留档**：
        # 再入一次账就是同一张纸发两遍工资，而且事后极难发现。
        from .print_sheet import bind_print_form

        bound = bind_print_form(db, sheet, lines)
        # 重识别时读不到单号就保留原来的绑定：互认关系不能因为一次没读清就丢
        if bound.get("form_no"):
            sheet.print_form_no = bound["form_no"]
        if bound["state"] in ("reported", "void"):
            label = (
                "已扫码报工"
                if bound["state"] == "reported"
                else "已作废（重印替换或手工作废）"
            )
            sheet.status = "rejected"
            sheet.reject_reason = (
                f"预印单 {bound['form_no']} {label}，本次识别只留档、不计工时"
            )
            db.add(
                AuditLog(
                    entity="sheet",
                    entity_id=sheet.id,
                    sheet_id=sheet.id,
                    field="print_form_no",
                    old_value="",
                    new_value=bound["form_no"],
                    user="system",
                )
            )
        elif bound["state"] == "ambiguous":
            # 一页纸上读出多个有效单号 = 至少有一个是错的。挑错就是把工时
            # 记到别人头上，所以不挑，写成提示让人看。
            sheet.note = (
                (sheet.note or "") + " / 纸面读到多个预印单号：" + "、".join(bound["candidates"])
            ).strip(" /")[:255]

        # 人工改过的行会被整行重建，删之前先拍快照；真正被丢掉的那些稍后写进留痕
        snapshots: list[dict] = []
        for old in list(sheet.items):
            if old.row_state in ("edited", "manual", "locked"):
                snapshots.append({**_item_snapshot(old), "_entity_id": old.id})
            db.delete(old)
        db.flush()

        total = 0.0
        for idx, it in enumerate(items, start=1):
            row = WorkItem(
                sheet_id=sheet.id,
                seq=idx,
                product_name=it.get("product_name", ""),
                batch_no=it.get("batch_no", ""),
                part_name=it.get("part_name", ""),
                material=it.get("material", ""),
                process=it.get("process", ""),
                process_raw=it.get("process_raw", ""),
                process_std=it.get("process_std", ""),
                process_code=it.get("process_code", ""),
                qualified_qty=it.get("qualified_qty"),
                rework_qty=it.get("rework_qty"),
                scrap_qty=it.get("scrap_qty"),
                unit_hours=it.get("unit_hours"),
                declared_total=it.get("declared_total"),
                total_hours=compute_total_hours(
                    it.get("qualified_qty"), it.get("unit_hours"), it.get("rework_qty"), it.get("scrap_qty")
                ),
                finish_date=_as_date(it.get("finish_date")),
                finish_date_raw=it.get("finish_date_raw", "") or "",
                inspector=it.get("inspector", ""),
                inspector_raw=it.get("inspector_raw", ""),
                handover=it.get("handover", ""),
                worker_id=it.get("worker_id"),
                worker_raw=it.get("worker_raw", ""),
                conf_json=json.dumps(it.get("conf", {}), ensure_ascii=False),
                flags_json=json.dumps(it.get("flags", []), ensure_ascii=False),
                bbox_json=json.dumps(it.get("bbox", {}), ensure_ascii=False),
                # 保留过人工值的行标成 edited，界面上一眼能看出「这行不是纯识别结果」
                row_state="edited" if it.get("_manual") else "auto",
            )
            db.add(row)
            total += row.total_hours

        sheet.total_hours = round(total, 4)

        # 只有「人工值没被保住」的行才需要提示被覆盖
        kept_seqs = {i for i, it in enumerate(items, start=1) if it.get("_manual")}
        for snap in snapshots:
            if snap.get("seq") in kept_seqs:
                continue
            item_id = int(snap.get("_entity_id") or 0)
            db.add(
                AuditLog(
                    entity="work_item",
                    entity_id=item_id,
                    sheet_id=sheet.id,
                    field="item:overwritten",
                    old_value=json.dumps(snap, ensure_ascii=False, default=str),
                    new_value="",
                    user="system",
                )
            )

        img.status = "done"
        img.claimed_at = None
        # keeps 是**这一次**重识别的参数，用掉就清掉：留着会让下一轮
        # （比如三个月后又点了重识别）莫名其妙地沿用今天的人工值。
        img.keeps_json = ""
        db.commit()
        log.info(
            "识别完成 image=%s sheet=%s items=%d 保留人工字段=%d", img.id, sheet.id, len(items), kept
        )
        # 新单可能带出从未见过的「产品 × 件名」。以前靠「打开组合台账就顺手同步」，
        # 现在同步已从 GET 里移除，改在这里触发 —— 时机更准（真有新数据才登记），
        # 而且跑在单实例的 OCR 进程里，不会有多进程抢写锁的问题。
        _register_new_combos(db)

    except Exception as exc:  # pragma: no cover - 运行期异常兜底
        log.exception("识别失败 image=%s", image_id)
        db.rollback()
        img = db.get(Image, image_id)
        if img:
            # 明确标 failed 而不是退回 pending：能捕获的异常是确定性失败
            # （文件坏了、格式不支持），自动重试只会把同一份错误重放三遍。
            # 人修好文件后点「重新识别」会重置 attempts 再入队。
            img.status = "failed"
            img.claimed_at = None
            img.error = str(exc)[:500]
            db.commit()
    finally:
        db.close()


def _as_date(value) -> object:
    import datetime as dt

    if not value:
        return None
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except Exception:
        return None


_NUM_ITEM_FIELDS = {"qualified_qty", "rework_qty", "scrap_qty", "unit_hours"}


def _cast_field(field: str, value):
    """留痕里存的是文本，回填时要还回原本的类型，否则算工时会炸。"""
    if value is None or str(value).strip() in ("", "None"):
        return None
    if field in _NUM_ITEM_FIELDS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    if field == "worker_id":
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None
    return str(value)


def _apply_keeps(items: list[dict], keeps: dict | None) -> tuple[list[dict], int]:
    """把人工改过的字段盖回识别结果上（按行序号 + 字段名）。

    重新识别的目的是修正识别错的地方，不是推翻人刚刚核对过的值；
    没被人碰过的字段照旧用新结果，改过的字段保留人工值。
    """
    manual = (keeps or {}).get("items") or {}
    if not manual:
        return items, 0
    applied = 0
    for idx, it in enumerate(items, start=1):
        patch = manual.get(idx) or manual.get(str(idx))
        if not patch:
            continue
        for field, value in patch.items():
            if field not in it:
                continue
            cast = _cast_field(field, value)
            if cast is None:
                continue
            it[field] = cast
            applied += 1
        # 人工改过数/工时的行，原单手写合计已不可信，别再拿它比对
        if "qualified_qty" in patch or "unit_hours" in patch:
            it["declared_total"] = None
        it["_manual"] = True
    return items, applied


def _apply_header_keeps(header: dict, keep: dict | None) -> dict:
    """表头同理：只保留人改过的那几个键。"""
    if not keep:
        return header
    for key, value in keep.items():
        if value is None or str(value).strip() == "":
            continue
        header[key] = value
    return header


def _item_snapshot(it: WorkItem) -> dict:
    """重新识别要覆盖人工修改，先把整行存成快照留痕（行删掉后就查不到人员/工序了）。"""
    return {
        "seq": it.seq,
        "product_name": it.product_name,
        "batch_no": it.batch_no,
        "part_name": it.part_name,
        "material": it.material,
        "process": it.process,
        "qualified_qty": it.qualified_qty,
        "rework_qty": it.rework_qty,
        "scrap_qty": it.scrap_qty,
        "unit_hours": it.unit_hours,
        "total_hours": it.total_hours,
        "finish_date": it.finish_date,
        "inspector": it.inspector,
        "handover": it.handover,
        "worker_raw": it.worker_raw,
        "worker_name": (it.worker.name if it.worker else "") or "",
        "remark": it.remark,
    }


def enqueue(image_id: int, keeps: dict | None = None) -> None:
    """把一张图放进识别队列（= 把 image 行标成 pending）。

    **这里不再起线程**。队列已经从「某个进程的内存」搬到数据库：
    Web 进程只负责写状态，独立 OCR 进程负责领取执行。这样 Web 才能开多进程
    （否则每加一个 worker 就多一份谁也看不见的私有队列，任务落进哪个进程全看运气）。

    顺带解决了原来「Future 异常没人取就被静默吞掉、图片永远停在 processing」的问题：
    状态在库里，进程死了由 worker 启动时的 resume_orphaned 兜回来。
    """
    db = SessionLocal()
    try:
        img = db.get(Image, image_id)
        if img is None:
            log.warning("入队失败：图片 %s 不存在", image_id)
            return
        if img.status == "done" and keeps is None and not (img.keeps_json or "").strip():
            # 已经识别完且不是「重新识别」，不必再跑一遍
            return
        img.status = "pending"
        img.error = ""
        img.claimed_at = None
        if keeps is not None:
            # 人主动点了重新识别：重置重试计数，否则一张失败过 3 次的图
            # 即使文件已修好也永远进不了队列（这是「点了没反应」的经典成因）。
            img.attempts = 0
            img.keeps_json = json.dumps(keeps, ensure_ascii=False, default=str)
        db.commit()
    finally:
        db.close()


def _load_keeps(img: Image) -> dict | None:
    """取出上一次重识别要保留的人工值。读不懂就按首次识别处理，不阻断识别。"""
    raw = (img.keeps_json or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("image=%s 的 keeps_json 无法解析，按首次识别处理", img.id)
        return None
    return data if isinstance(data, dict) else None


def claim_next(db: Session, *, max_attempts: int | None = None) -> int | None:
    """原子领取一条待识别图片，返回 image_id；队列空了返回 None。

    分两步，但**第二步才是抢占凭据**：

    1. 挑候选 id（只读，不加锁）；
    2. `UPDATE image SET status='processing' WHERE id=? AND status='pending'`，
       只有 rowcount==1 才算领到；rowcount==0 说明被别的进程抢先，回头重新挑。

    为什么不做成一条 `UPDATE ... WHERE id=(SELECT ...)`：SQLite 对「同一张表的
    子查询用于 UPDATE」虽然允许，但语义依赖执行计划，一旦换库就变。两步写法在
    任何数据库上都成立，代价只是多一次索引命中。
    """
    limit_attempts = MAX_ATTEMPTS if max_attempts is None else max_attempts
    while True:
        cand = db.scalar(
            select(Image.id)
            .where(Image.status == "pending", Image.attempts < limit_attempts)
            .order_by(Image.id)
            .limit(1)
        )
        if cand is None:
            return None
        got = db.execute(
            update(Image)
            .where(Image.id == cand, Image.status == "pending")
            .values(
                status="processing",
                attempts=Image.attempts + 1,
                claimed_at=dt.datetime.now(),
                error="",
            )
        )
        db.commit()
        if got.rowcount == 1:
            return cand


def resume_orphaned(db: Session, *, max_attempts: int | None = None) -> list[int]:
    """把上一个 worker 没跑完的任务退回队列；超过重试上限的判死，不再捞。

    队列在进程内存里时，进程一重启排队中的任务全部消失，图片永远停在
    「识别中」，前端一直等 done（用户看到的就是「提交后卡死」），而且重试上传
    会被 sha256 判成重复直接跳过 —— 没有任何出口。所以启动时必须自愈。

    **但现在多了上限**：一张毒图（超大图、引擎段错误）每次被捞回来都可能把
    进程带走，没上限就是「启动 → 崩 → 启动」，服务永远起不来。连续
    `max_attempts` 次没跑完的，标 failed 交给人看。
    """
    limit_attempts = MAX_ATTEMPTS if max_attempts is None else max_attempts
    rows = list(db.scalars(select(Image).where(Image.status.in_(("pending", "processing")))))
    if not rows:
        return []
    requeued: list[int] = []
    dead: list[int] = []
    for r in rows:
        used = int(r.attempts or 0)
        if used >= limit_attempts:
            r.status = "failed"
            r.claimed_at = None
            r.error = f"已尝试 {used} 次仍未完成，停止自动重试：{(r.error or '')[:200]}"
            dead.append(r.id)
        else:
            r.status = "pending"
            r.claimed_at = None
            r.error = ""
            requeued.append(r.id)
    db.commit()
    if requeued:
        log.warning("启动自愈：%d 张图片退回队列 %s", len(requeued), requeued)
    if dead:
        log.error("启动自愈：%d 张图片超过重试上限，已停止自动重试 %s", len(dead), dead)
    return requeued


def queue_stats(db: Session) -> dict:
    """队列现状：健康检查与界面「还要等多久」都看这个。"""
    rows = db.execute(
        select(Image.status, func.count()).group_by(Image.status)
    ).all()
    counts = {str(s): int(n) for s, n in rows}
    oldest = db.scalar(
        select(func.min(Image.uploaded_at)).where(Image.status == "pending")
    )
    oldest_wait = None
    if isinstance(oldest, dt.datetime):
        oldest_wait = round((dt.datetime.now() - oldest).total_seconds(), 1)
    return {
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "failed": counts.get("failed", 0),
        "done": counts.get("done", 0),
        # 最久等待的 pending 有多久没动 —— 比「积压几张」更能说明卡没卡
        "oldest_pending_seconds": oldest_wait,
    }


def _register_new_combos(db: Session) -> None:
    """识别落库后登记新出现的「产品 × 件名」。

    组合台账的「顺手同步」已经从读接口里删掉了（那次全量扫描既慢又抢写锁），
    新数据的入口改到这里：真有新组合才写，且只发生在单实例的 OCR 进程里。
    """
    try:
        from .combo_registry import sync_combos

        res = sync_combos(db, actor="ocr")
        if res.get("created"):
            db.commit()
            log.info("识别带出新组合 %s 条（默认草稿，待工艺员确认发布）", res["created"])
    except Exception:  # pragma: no cover - 登记失败不能影响这张单的识别结果
        log.warning("识别后登记组合失败（不影响本张单据）", exc_info=True)
        db.rollback()
