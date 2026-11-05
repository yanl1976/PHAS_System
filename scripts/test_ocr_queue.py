"""识别队列测试：外置到数据库之后，队列还对不对。

C1 把队列从「某个进程的内存」搬到了 `image.status`。这一步换掉的是整条识别链
的地基，风险不在「能不能识别」，而在**同一张图会不会被跑两遍**：

* 跑两遍 → 同名明细写两份 → 多发一份工资；
* 或者反过来，一张毒图被反复捞回来 → 服务陷入「启动 → 崩溃 → 启动」永远起不来。

所以这一组测的全是队列的边界：并发领取不重复、重试有上限、重启自愈只捞该捞的。

原则同其它脚本：只用自己造的数据（批次名前缀 ZZQ），跑完清干净。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

import datetime as dt  # noqa: E402

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import Batch, Image, Period  # noqa: E402
from app.services import pipeline as P  # noqa: E402

TAG = "ZZQ"
YM = "2099-03"

PASSED: list[str] = []
FAILED: list[str] = []


def check(ok: bool, label: str) -> None:
    if ok:
        PASSED.append(label)
        print(f"  [OK] {label}")
    else:
        FAILED.append(label)
        print(f"  [!!] {label}")


def section(title: str) -> None:
    print(f"\n--- {title}")


def make_batch(db) -> Batch:
    p = db.scalar(select(Period).where(Period.ym == YM))
    if p is None:
        p = Period(ym=YM, title=f"{TAG}队列测试", status="open")
        db.add(p)
        db.flush()
    b = Batch(period_id=p.id, name=f"{TAG}批次", source="test", created_by="tester")
    db.add(b)
    db.flush()
    return b


def make_image(db, batch: Batch, n: int, *, status: str = "pending", attempts: int = 0) -> Image:
    img = Image(
        batch_id=batch.id,
        filename=f"{TAG}-{n}.png",
        path=f"/dev/null/{TAG}-{n}.png",
        sha256=f"{TAG}{n:04d}" + "0" * 50,
        status=status,
        attempts=attempts,
    )
    db.add(img)
    db.flush()
    return img


def cleanup(db) -> None:
    """只删自己造的行。按文件名前缀认，绝不整表删。"""
    ids = list(db.scalars(select(Image.id).where(Image.filename.like(f"{TAG}-%"))))
    if ids:
        # 入队不写明细，这里只需删 image 行本身
        for img in db.scalars(select(Image).where(Image.id.in_(ids))):
            db.delete(img)
    bids = list(db.scalars(select(Batch.id).where(Batch.name.like(f"{TAG}%"))))
    if bids:
        for b in db.scalars(select(Batch).where(Batch.id.in_(bids))):
            db.delete(b)
    db.commit()
    p = db.scalar(select(Period).where(Period.ym == YM))
    if p is not None:
        db.delete(p)
        db.commit()


def main() -> int:
    db = SessionLocal()
    cleanup(db)
    batch = make_batch(db)
    db.commit()

    # ------------------------------------------------------------------ 1 入队
    section("1 入队 = 只写 pending，不起线程")
    a = make_image(db, batch, 1)
    db.commit()
    P.enqueue(a.id)
    db.expire_all()
    a2 = db.get(Image, a.id)
    check(a2.status == "pending", f"enqueue 后状态为 pending（{a2.status}）")
    check(a2.claimed_at is None, "入队时清掉 claimed_at")
    check(a2.attempts == 0, "首次入队 attempts 仍为 0")

    # 已完成的图不该被重新排队（否则每次刷新都重跑一遍识别）
    b = make_image(db, batch, 2, status="done")
    db.commit()
    P.enqueue(b.id)
    db.expire_all()
    check(db.get(Image, b.id).status == "done", "已 done 的图 enqueue 不会退回去重跑")

    # 但「人主动点重新识别」必须能重跑，而且要把重试次数清零
    db.get(Image, b.id).attempts = 5
    db.commit()
    P.enqueue(b.id, keeps={"header": {"product": f"{TAG}保留值"}})
    db.expire_all()
    b2 = db.get(Image, b.id)
    check(b2.status == "pending", "显式重识别会把 done 的图重新排入队列")
    check(b2.attempts == 0, f"重识别重置重试计数（{b2.attempts}）")
    check("保留值" in (b2.keeps_json or ""), "人工修改存进 keeps_json（不再靠线程参数传递）")

    # ------------------------------------------------------------------ 2 领取
    section("2 领取：同一次只交给一个人")
    # 先清空环境，只留一张待领取
    for img in db.scalars(select(Image).where(Image.filename.like(f"{TAG}-%"))):
        img.status = "done"
    db.commit()
    t = make_image(db, batch, 10)
    db.commit()

    got = P.claim_next(db, max_attempts=P.MAX_ATTEMPTS)
    db.expire_all()
    t2 = db.get(Image, t.id)
    check(got == t.id, f"claim_next 返回这张图（{got}）")
    check(t2.status == "processing", f"领取后状态 processing（{t2.status}）")
    check(t2.attempts == 1, f"领取计数 +1（{t2.attempts}）")
    check(t2.claimed_at is not None, "记下领取时间（用于判断是不是卡死）")
    check(P.claim_next(db) is None, "队列空了返回 None，不返回别人的图")

    # ------------------------------------------------------------------ 3 并发
    section("3 并发领取：20 个线程抢 20 张，一张都不能重复")
    for img in db.scalars(select(Image).where(Image.filename.like(f"{TAG}-%"))):
        img.status = "done"
    db.commit()
    ids = []
    for i in range(20):
        ids.append(make_image(db, batch, 100 + i).id)
    db.commit()

    taken: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(10)

    def worker() -> None:
        # 每个线程用自己的会话，模拟真实的多进程/多线程
        s = SessionLocal()
        try:
            start.wait(timeout=10)
            while True:
                got_id = P.claim_next(s, max_attempts=P.MAX_ATTEMPTS)
                if got_id is None:
                    return
                with lock:
                    taken.append(got_id)
        except threading.BrokenBarrierError:
            return
        finally:
            s.close()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)

    check(len(taken) == 20, f"20 张全部被领走（实领 {len(taken)}）")
    check(len(set(taken)) == len(taken), f"没有一张被领两次（去重后 {len(set(taken))}）")
    check(set(taken) == set(ids), "领到的正好是这 20 张，没有串到别的单")

    db.expire_all()
    rows = list(db.scalars(select(Image).where(Image.id.in_(ids))))
    check(all(r.status == "processing" for r in rows), "20 张全部停在 processing，等 worker 回写结果")

    # ------------------------------------------------------------------ 4 上限
    section("4 毒图：到上限就不再捞，否则服务永远起不来")
    for img in db.scalars(select(Image).where(Image.filename.like(f"{TAG}-%"))):
        img.status = "done"
    db.commit()
    poison = make_image(db, batch, 900, status="pending", attempts=P.MAX_ATTEMPTS)
    ok_one = make_image(db, batch, 901, status="pending", attempts=P.MAX_ATTEMPTS - 1)
    db.commit()

    got = P.claim_next(db, max_attempts=P.MAX_ATTEMPTS)
    check(got == ok_one.id, f"只领取还没到上限的那张（领到 {got}）")
    db.expire_all()
    check(db.get(Image, poison.id).status == "pending", "到上限的图留在 pending，不再被领取")
    check(P.claim_next(db) is None, "剩下的都到上限时队列视为空")

    # 人可以救回来：点一次重新识别就清零，允许再试
    P.enqueue(poison.id, keeps={"force": True})
    db.expire_all()
    check(db.get(Image, poison.id).attempts == 0, "人工重识别可以把毒图的重试计数清零")

    # ------------------------------------------------------------------ 5 自愈
    section("5 重启自愈：捞回中断的，判死超限的")
    for img in db.scalars(select(Image).where(Image.filename.like(f"{TAG}-%"))):
        img.status = "done"
    db.commit()
    interrupted = make_image(db, batch, 500, status="processing", attempts=1)
    queued = make_image(db, batch, 501, status="pending", attempts=0)
    dead = make_image(db, batch, 502, status="processing", attempts=P.MAX_ATTEMPTS)
    done_img = make_image(db, batch, 503, status="done", attempts=2)
    db.commit()

    before = time.time()
    requeued = P.resume_orphaned(db, max_attempts=P.MAX_ATTEMPTS)
    db.expire_all()
    check(set(requeued) == {interrupted.id, queued.id}, f"只捞回该重跑的两张（{sorted(requeued)}）")
    check(db.get(Image, interrupted.id).status == "pending", "中断的 processing 退回 pending")
    check(db.get(Image, queued.id).status == "pending", "排队中的保持 pending")
    d2 = db.get(Image, dead.id)
    check(d2.status == "failed", f"超限的判 failed（{d2.status}）")
    check("停止自动重试" in (d2.error or ""), f"判死原因写清楚（{d2.error[:40]}）")
    check(db.get(Image, done_img.id).status == "done", "已完成的图不受自愈影响")
    check(time.time() - before < 5, "自愈本身不跑识别（只改状态）")

    # 自愈捞回来的一定能被领走，且不会超过上限
    got = P.claim_next(db, max_attempts=P.MAX_ATTEMPTS)
    check(got in (interrupted.id, queued.id), f"捞回来的能被正常领取（{got}）")

    # ------------------------------------------------------------------ 6 统计
    section("6 队列统计：能看出「卡没卡」")
    for img in db.scalars(select(Image).where(Image.filename.like(f"{TAG}-%"))):
        img.status = "done"
    db.commit()
    for i in range(3):
        make_image(db, batch, 700 + i, status="pending")
    make_image(db, batch, 720, status="processing")
    make_image(db, batch, 721, status="failed")
    db.commit()
    st = P.queue_stats(db)
    mine = {k: st[k] for k in ("pending", "processing", "failed")}
    check(mine["pending"] >= 3, f"统计出待处理数量（{mine['pending']}）")
    check(mine["processing"] >= 1, f"统计出处理中数量（{mine['processing']}）")
    check(mine["failed"] >= 1, f"统计出失败数量（{mine['failed']}）")
    check("oldest_pending_seconds" in st, "带「最久等待多久」，比积压张数更能说明卡没卡")

    cleanup(db)
    db.close()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("失败：")
        for f in FAILED:
            print("  -", f)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
