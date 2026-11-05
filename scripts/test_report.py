"""扫码报工测试：预印单、扫码解析、提交、撤销、幂等。

原则同其它脚本：只用自己造的数据（前缀 ZZQR、核算期 2099-02），跑完清干净。

这一组要守住的红线只有一条：**同一批工时绝不能进两次账**。
重复入账 = 多发一份工资，而多发出去的钱基本追不回来。所以测的重点不是
「功能能不能用」，而是「重复、错单、作废、撤销这些异常路径是不是都被挡住」。
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import datetime as dt  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.domain import qr  # noqa: E402
from app.domain.aggregation import monthly_summary  # noqa: E402
from app.models import (  # noqa: E402
    Period,
    PrintSheet,
    PrintSheetLine,
    WorkItem,
    Worker,
)
from app.services import print_sheet as PS  # noqa: E402

TAG = "ZZQR"
YM = "2099-02"
WORKER = f"{TAG}甲"
WORKER2 = f"{TAG}乙"

PASSED: list[str] = []
FAILED: list[str] = []


def check(ok: bool, label: str) -> None:
    if ok:
        PASSED.append(label)
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [FAIL] {label}")


def section(title: str) -> None:
    print(f"\n{title}")


def cleanup() -> None:
    db = SessionLocal()
    # 先删明细（外键指向 print_sheet 的只有 lines，work_item 走 print_form_no 字符串）
    forms = list(db.scalars(select(PrintSheet)))
    for f in forms:
        db.query(WorkItem).filter(WorkItem.print_form_no == f.form_no).delete()
        db.delete(f)
    db.query(WorkItem).filter(WorkItem.source == "scan").delete()
    for w in db.scalars(select(Worker).where(Worker.name.like(f"{TAG}%"))).all():
        db.delete(w)
    p = db.scalar(select(Period).where(Period.ym == YM))
    if p:
        db.delete(p)
    db.commit()
    db.close()


def make_period(db) -> Period:
    p = db.scalar(select(Period).where(Period.ym == YM))
    if p is None:
        p = Period(ym=YM, title=f"{TAG}测试期", status="open")
        db.add(p)
        db.flush()
    return p


def make_form(db, *, form_no: str, token: str = "abcd1234", status: str = "printed",
              part: str = "整机装配", lines: int = 3, qty: float = 10.0) -> PrintSheet:
    fs = PrintSheet(
        form_no=form_no,
        task_id=999001,
        product_name=f"{TAG}产品",
        batch_no=f"{TAG}批次",
        part_name=part,
        part_no="01",
        qty_plan=qty,
        part_qty=qty,
        total_hours=100.0,
        token=token,
        status=status,
        printed_by="tester",
    )
    db.add(fs)
    db.flush()
    units = [2.0, 3.0, 1.5]
    for i in range(1, lines + 1):
        db.add(
            PrintSheetLine(
                form_id=fs.id,
                row_no=i,
                seq=i * 10,
                process_name=f"工序{i}",
                process_code=f"P{i}",
                category="钳工",
                unit_hours=units[i - 1] if i <= len(units) else 1.0,
                qty_plan=qty,
                total_hours=(units[i - 1] if i <= len(units) else 1.0) * qty,
            )
        )
    db.commit()
    db.refresh(fs)
    return fs


def main() -> int:
    cleanup()
    db = SessionLocal()
    period = make_period(db)
    w1 = Worker(name=WORKER, default_process="钳工", team="测试班组", active=True)
    w2 = Worker(name=WORKER2, default_process="车工", team="测试班组", active=True)
    db.add_all([w1, w2])
    db.commit()

    # ------------------------------------------------------------------ 1 码
    section("1 二维码载荷：只装定位信息，解析要容错扫码枪的脏输入")
    code = qr.form_payload("RG2601-00007", "deadbeef")
    check(code == "HY1:RG2601-00007:deadbeef", f"单码格式（{code}）")
    p = qr.parse(code)
    check(p["ok"] and p["kind"] == "form" and p["form_no"] == "RG2601-00007", "解析单码")
    line_code = qr.line_payload("RG2601-00007", 3, "deadbeef")
    p2 = qr.parse(line_code)
    check(p2["ok"] and p2["kind"] == "line" and p2["row_no"] == 3, "解析行码带行号")
    check(qr.parse("  " + code + " \r\n")["ok"], "扫码枪带回车和空格也能解析")
    check(not qr.parse("随便一串字")["ok"], "非本系统码被拒")
    check(not qr.parse("HY1::token")["ok"], "缺单号被拒")
    check(qr.parse("http://127.0.0.1:8000/r/HY1:RG1-1:t")["ok"], "URL 形式的码也能取出来")

    # ------------------------------------------------------------------ 2 生成
    section("2 生成预印单：一个件名一张纸，已印过的复用而不是重印")
    fake_card = {
        "part": "整机装配",
        "code": "01",
        "product": f"{TAG}产品",
        "print_qty": 5.0,
        "qty_per_unit": 1,
        "batch_hours": 30.0,
        "route_id": None,
        "version": 1,
        "published_by": "tester",
        "task": {"batch_no": f"{TAG}批次", "qty": 5.0},
        "steps": [
            {"process_name": "装配", "unit_hours": 2.0, "seq": 10},
            {"process_name": "总装", "unit_hours": 4.0, "seq": 20},
        ],
    }
    orig_build = PS.PC.build_cards
    PS.PC.build_cards = lambda db, **kw: {"cards": [dict(fake_card)]}
    try:
        r1 = PS.generate(db, 999001, by="tester")
        check(len(r1["created"]) == 1, f"新建 1 张（{r1['message']}）")
        no1 = r1["created"][0]["form_no"]
        check(no1.startswith("RG") and "-" in no1, f"单号是人能读的格式（{no1}）")
        check(r1["created"][0]["lines"] == 2, "工序行数正确")
        r2 = PS.generate(db, 999001, by="tester")
        check(len(r2["created"]) == 0 and len(r2["reused"]) == 1, "再生成一次复用旧单，不重复印")
        r3 = PS.generate(db, 999001, force=True, by="tester")
        check(len(r3["created"]) == 1, "force 才重印并换新号")
        old = db.scalar(select(PrintSheet).where(PrintSheet.form_no == no1))
        check(old.status == "void", "重印会把旧纸作废（否则两张纸都能报工）")
    finally:
        PS.PC.build_cards = orig_build

    # ------------------------------------------------------------------ 3 扫码
    section("3 扫码：错码/作废/已报工都要说清原因，不能笼统报错")
    fs = make_form(db, form_no=f"{TAG}-F001", token="t1")
    res = PS.scan(db, qr.form_payload(fs.form_no, "t1"))
    check(res["ok"] and len(res["lines"]) == 3, "正常单可载入全部工序行")
    check(res["form"]["part_qty"] == 10.0, "件数取自快照")
    bad = PS.scan(db, qr.form_payload(fs.form_no, "wrong"))
    check(not bad["ok"] and "不匹配" in bad["reason"], "令牌不对被拒（防复印）")
    missing = PS.scan(db, qr.form_payload("RG0000-00000", "t1"))
    check(not missing["ok"] and "找不到" in missing["reason"], "不存在的单号被拒")

    void_form = make_form(db, form_no=f"{TAG}-F002", token="t2", status="void")
    vres = PS.scan(db, qr.form_payload(void_form.form_no, "t2"))
    check(not vres["ok"] and "作废" in vres["reason"], "作废单扫不进来")

    # ------------------------------------------------------------------ 4 提交
    section("4 提交：工时 = 合格 × 定额，核算期与任务要落在明细上")
    out = PS.submit(
        db,
        fs.form_no,
        [
            {"row_no": 1, "qty_done": 10, "qualified": 10, "worker_name": WORKER},
            {"row_no": 2, "qty_done": 11, "qualified": 9, "scrap": 2,
             "worker_name": WORKER2, "adjust_reason": "尺寸超差"},
            {"row_no": 3, "qty_done": 0, "qualified": 0},  # 没干的工序：跳过
        ],
        by="tester",
        sign_verified=True,
        report_date=dt.date(2099, 2, 15),  # 落在测试期内，不污染真实月份
    )
    check(out["ok"], f"提交成功（{out.get('reason') or out['items']}）")
    check(out["items"] == 2, "空行被跳过，只入 2 行")
    check(abs(out["hours"] - 47.0) < 1e-6, f"工时 = 10×2 + 9×3 = 47（{out['hours']}）")
    check(out["period"] == YM, f"落到核算期 {YM}")

    items = list(db.scalars(select(WorkItem).where(WorkItem.print_form_no == fs.form_no)))
    check(len(items) == 2 and all(i.source == "scan" for i in items), "明细来源标记为 scan")
    check(all(i.sheet_id is None for i in items), "扫码明细不挂纸质单据")
    check(all(i.period_id == period.id for i in items), "明细自带核算期")
    check(all(i.task_id == fs.task_id for i in items), "明细带任务归属")
    check(all(i.sign_verified for i in items), "签字核对标记落到明细")
    by_no = {i.seq: i for i in items}
    check(by_no[1].worker_id == w1.id, "操作者按名字对上人员")
    check(by_no[2].scrap_qty == 2 and by_no[2].adjust_reason == "尺寸超差", "废品与调整原因都记下")
    check(by_no[1].qty_done == 10, "完成数量单独落库（纸面「数量」栏以前是空的）")

    # ------------------------------------------------------------------ 5 幂等
    section("5 幂等：同一张单不能报两次（重复就是多发工资）")
    dup = PS.submit(db, fs.form_no, [{"row_no": 1, "qualified": 5, "worker_name": WORKER}], by="tester")
    check(not dup["ok"] and "已报过工" in dup["reason"], "重复提交被拦")
    again = PS.scan(db, qr.form_payload(fs.form_no, "t1"))
    check(not again["ok"] and again.get("code") == "ALREADY_REPORTED", "再扫提示已报工并给出码")

    # ------------------------------------------------------------------ 6 汇总
    section("6 汇总与工资：扫码工时必须进得去（outer join 那条路）")
    summary = monthly_summary(db, YM)
    check(abs(summary["totals"]["hours"] - 47.0) < 1e-6, f"月度工时含扫码报工（{summary['totals']['hours']}）")
    check(summary["totals"]["sheets"] == 0, "扫码报工不算纸质单据数")
    names = {p["worker_name"]: p["hours"] for p in summary["persons"]}
    check(abs(names.get(WORKER, 0) - 20.0) < 1e-6, f"{WORKER} 20h（{names.get(WORKER)}）")
    check(abs(names.get(WORKER2, 0) - 27.0) < 1e-6, f"{WORKER2} 27h（{names.get(WORKER2)}）")

    # ------------------------------------------------------------------ 7 撤销
    section("7 撤销：点错数量要能退回来，不然只能去改数据库")
    # 撤销是删钱的动作：不写原因就等于没留痕，一个月后没人说得清工资为什么少了
    blind = PS.undo(db, fs.form_no, by="tester")
    check(not blind["ok"] and "原因" in blind["reason"], "撤销不写原因被拒")
    undo = PS.undo(db, fs.form_no, by="tester", reason="合格数敲错了")
    check(undo["ok"] and undo["removed"] == 2, f"撤销删掉 2 行（{undo.get('removed')}）")
    check(monthly_summary(db, YM)["totals"]["hours"] == 0.0, "撤销后工时归零")
    fs2 = db.scalar(select(PrintSheet).where(PrintSheet.form_no == fs.form_no))
    check(fs2.status == "printed", "单据退回待报工，可以重报")
    requeue = PS.scan(db, qr.form_payload(fs.form_no, "t1"))
    check(requeue["ok"], "撤销后同一张纸能重新报工")

    # ------------------------------------------------------------------ 8 异常提示
    section("8 异常提示：能入账的要说明白，算不通的直接拦")
    w = make_form(db, form_no=f"{TAG}-F003", token="t3", lines=2)
    r = PS.submit(
        db,
        w.form_no,
        [
            # 不填操作者
            {"row_no": 1, "qty_done": 5, "qualified": 5},
            # 操作者不在名册
            {"row_no": 2, "qty_done": 2, "qualified": 2,
             "worker_name": f"{TAG}不存在的人"},
        ],
        by="tester",
        sign_verified=True,
    )
    joined = " / ".join(r.get("warnings") or [])
    check("没填操作者" in joined, f"没填操作者会提示（{joined[:40]}）")
    check("不在人员名单" in joined, "操作者不在名单会提示")
    PS.undo(db, w.form_no, by="tester", reason="测试清理")

    # 合格 + 废品 > 完成数：这不是「提示一下」就放行的账，
    # 一笔写进去就把废品记成了比干出来的还多，必须整单拒绝
    bad = make_form(db, form_no=f"{TAG}-F005", token="t5", lines=1)
    rej = PS.submit(
        db,
        bad.form_no,
        [{"row_no": 1, "qty_done": 3, "qualified": 3, "scrap": 2,
          "worker_name": WORKER, "adjust_reason": "操作失误"}],
        by="tester",
        sign_verified=True,
    )
    check(not rej["ok"] and "超过完成数" in rej["reason"], "合格+废品超过完成数被拦")
    # 返修/报废没有原因同样不能入账：质量分析唯一的事实来源
    no_reason = PS.submit(
        db,
        bad.form_no,
        [{"row_no": 1, "qty_done": 3, "qualified": 2, "scrap": 1,
          "worker_name": WORKER}],
        by="tester",
        sign_verified=True,
    )
    check(not no_reason["ok"] and "调整原因" in no_reason["reason"], "返修/报废缺原因被拦")
    db.rollback()

    # ------------------------------------------------------------------ 9 渲染
    section("9 打印：二维码贴在表格之外，不压标题")
    fs3 = make_form(db, form_no=f"{TAG}-F004", token="t4", lines=3)
    pdf = PS.render_pdf(db, [fs3], dpi=150)
    check(pdf[:4] == b"%PDF" and len(pdf) > 8000, f"PDF 生成（{len(pdf)} 字节）")
    # 码带位置必须落在表格下方/标题上方之外的空白：表格从 32mm 到 178mm
    check(PS.QR_Y + PS.QR_SIZE < 32.0, f"码在表格上方空白处（{PS.QR_Y}~{PS.QR_Y + PS.QR_SIZE}mm）")
    check(PS.QR_X + PS.QR_SIZE < 297.0, "码在纸张范围内")

    # ------------------------------------------------------------------ 10 锁止与幂等
    section("10 锁止与幂等：没签字不入账、锁账不收、报过的件换不了新号")
    locked_form = make_form(db, form_no=f"{TAG}-F006", token="t6", lines=1)
    no_sign = PS.submit(
        db, locked_form.form_no,
        [{"row_no": 1, "qty_done": 1, "qualified": 1, "worker_name": WORKER}],
        by="tester", sign_verified=False,
    )
    check(not no_sign["ok"] and "签字" in no_sign["reason"], "没勾签字核对不能入账")

    period = db.scalar(select(Period).where(Period.ym == YM))
    period.status = "locked"
    db.commit()
    blocked = PS.submit(
        db, locked_form.form_no,
        [{"row_no": 1, "qty_done": 1, "qualified": 1, "worker_name": WORKER}],
        by="tester", sign_verified=True, report_date=dt.date(2099, 2, 16),
    )
    check(not blocked["ok"] and "锁账" in blocked["reason"], "锁账的月份收不了报工")
    period.status = "open"
    db.commit()

    # 已报工之后重印：必须沿用原单号，绝不能发新号（新号能再报一遍 = 重复发工资）
    done_form = make_form(db, form_no=f"{TAG}-F007", token="t7", lines=1)
    ok = PS.submit(
        db, done_form.form_no,
        [{"row_no": 1, "qty_done": 2, "qualified": 2, "worker_name": WORKER}],
        by="tester", sign_verified=True, report_date=dt.date(2099, 2, 17),
    )
    check(ok["ok"], "已报工的单据留作样本")
    again = PS.create_from_cards(db, 999001, [
        {"part": done_form.part_name, "steps": [{"process_name": "装配", "unit_hours": 1}]}
    ])
    check(not again["created"], "已报工的件不再生成新单")
    check(
        any(x["form_no"] == done_form.form_no for x in again["already_reported"]),
        "重印沿用原单号",
    )

    # 顶栏统计必须含扫码工时：不含就会让人以为「报工没记账」
    from app.api.dicts import stats as _stats

    st = _stats(ym=YM, db=db)
    check(st["hours"]["verified"] >= 4.0, f"顶栏已确认工时含扫码（{st['hours']['verified']}h）")
    check(st["counts"]["scan_items"] >= 1, "顶栏能看到扫码报工行数")

    # ------------------------------------------------------------------ 11 通道互认
    section("11 两条通道互认：纸面上的单号能反查预印单，报过的纸不再入账")
    # 用真实格式的单号（RG + 年月 + 流水）：识别侧正则只认这个前缀，
    # 用 ZZQR-xxx 这种测试号测不出真实现场的行为
    linked = make_form(db, form_no="RG9902-00010", token="t10", lines=1)
    got = PS.submit(
        db, linked.form_no,
        [{"row_no": 1, "qty_done": 1, "qualified": 1, "worker_name": WORKER}],
        by="tester", sign_verified=True, report_date=dt.date(2099, 2, 18),
    )
    check(got["ok"], "样本单已扫码报工")
    b1 = PS.bind_print_form(
        db, None, [{"text": f"加工路线单 {linked.form_no} 计划员扫码报工"}]
    )
    check(
        b1["state"] == "reported" and b1["form_no"] == linked.form_no,
        f"识别侧从纸面读到单号并认出已报工（{b1['state']}）",
    )
    b2 = PS.bind_print_form(db, None, [{"text": "RG0000-99999"}])
    check(b2["state"] == "unknown" and not b2["form_no"], "库里没有的单号不当真（宁可不认）")
    b3 = PS.bind_print_form(db, None, [{"text": "一张没有预印单号的老纸"}])
    check(b3["state"] == "none", "读不到单号就保持现状，不猜")
    other = make_form(db, form_no="RG9902-00011", token="t11", lines=1)
    b4 = PS.bind_print_form(
        db, None, [{"text": f"{linked.form_no} {other.form_no}"}]
    )
    check(
        b4["state"] == "ambiguous" and not b4["form_no"],
        "一页读出两个有效单号时交人工，不替人挑",
    )

    # ------------------------------------------------------------------ 12 纠错
    section("12 纠错：作废只能对未报工的单，且码立刻失效")
    vf = make_form(db, form_no=f"{TAG}-F012", token="t12", lines=1)
    v1 = PS.void_form(db, vf.form_no, by="tester", reason="零件报废不做了")
    check(v1["ok"], "待报工的单可以作废")
    check(
        not PS.scan(db, qr.form_payload(vf.form_no, "t12"))["ok"],
        "作废后纸上的码立刻失效",
    )
    v2 = PS.void_form(db, linked.form_no, by="tester", reason="想直接作废")
    check(
        not v2["ok"] and "撤销" in v2["reason"],
        "已报工的单不能一步作废（要先撤销）",
    )
    check(not PS.void_form(db, vf.form_no, by="tester", reason="再来一次")["ok"], "重复作废被拒")

    # ------------------------------------------------------------------ 13 台账
    section("13 报工台账：扫码单以预印单为主键列出来，明细可查")
    lg = PS.ledger(db, ym=YM, size=200)
    nos = {x["form_no"] for x in lg["items"]}
    check(linked.form_no in nos, "已报工的扫码单出现在台账")
    row = next((x for x in lg["items"] if x["form_no"] == linked.form_no), {})
    check(row.get("item_count") == 1 and row.get("hours", 0) > 0, "台账带明细行数与实报工时")
    det = PS.form_detail(db, linked.form_no)
    check(
        det["ok"] and len(det["items"]) == 1 and len(det["lines"]) == 1,
        "只读明细能同时看到纸上工序与实际报工明细",
    )

    # ------------------------------------------------------------------ 14 并发
    section("14 单号并发：撞号换号重试，不是把 500 甩给计划员")
    a1 = PS._next_form_no(db)
    a2 = PS._next_form_no(db, skip=1)
    check(a1 != a2, f"退让一位就是另一个号（{a1} / {a2}）")
    taken = linked.form_no
    calls = {"n": 0}
    real_next = PS._next_form_no

    def fake_next(db_, today=None, skip=0):
        calls["n"] += 1
        return taken if calls["n"] == 1 else real_next(db_, today, skip)

    PS._next_form_no = fake_next
    try:
        retry_form = PS._insert_form(
            db,
            task_id=999001,
            card={
                "part": f"{TAG}重试件",
                "code": "02",
                "product": f"{TAG}产品",
                "print_qty": 1.0,
                "steps": [{"process_name": "装配", "unit_hours": 1.0}],
            },
            qty=1.0,
            by="tester",
        )
        db.commit()
        check(
            retry_form.form_no != taken and retry_form.id > 0,
            f"撞号后自动换号建成（{retry_form.form_no}）",
        )
    finally:
        PS._next_form_no = real_next

    cleanup()
    db.close()

    print("\n" + "=" * 60)
    if FAILED:
        print(f"失败 {len(FAILED)} 项 / 共 {len(PASSED) + len(FAILED)} 项")
        for f in FAILED:
            print("  -", f)
        return 1
    print(f"全部通过（{len(PASSED)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
