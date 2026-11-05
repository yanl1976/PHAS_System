r"""独立 OCR 识别进程（C1）。

为什么要有这个进程，而不是让 Web 自己识别：

* Web 要开多进程才能用满多核（12 核实测 4 进程对 8 并发有 5~7 倍收益）。
* 但 PaddleOCR 每加载一份常驻约 331MB、峰值 401MB，而本机可用内存只有 7GB。
  4 个 Web worker 各背一份 = 1.6GB，还没算别的。
* 更要命的是「队列在进程内存里」：4 个 worker 就有 4 条互相看不见的队列，
  一张图落到哪个进程全看运气；若再把 `resume_orphaned()` 也各跑一遍，
  同一张图会被识别 4 次，**写 4 份明细 = 多发 4 倍工资**。

所以：Web 只把 `image.status` 标成 pending，本进程用 `claim_next()` 原子领取。
队列只有一条，识别只有一份引擎。

用法：
    .venv/Scripts/python.exe scripts/ocr_worker.py            # 常驻，Ctrl+C 退出
    .venv/Scripts/python.exe scripts/ocr_worker.py --once     # 处理完当前队列就退出（测试/补跑）
    .venv/Scripts/python.exe scripts/ocr_worker.py --status   # 只看队列和心跳，不启动

单实例：文件锁保证第二个进程直接退出。实测两个 OCR 进程并行**吞吐不涨**
（单进程 3 张 17.1s；两进程同时各 3 张 → 34.6/35.2s），Paddle 在 CPU 上
已经吃满多核，多开只是抢核 + 多占 400MB。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import signal
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

from app.config import (  # noqa: E402
    DATA_DIR,
    OCR_MAX_ATTEMPTS,
    OCR_POLL_SECONDS,
    OCR_WORKER_HEARTBEAT,
    OCR_WORKER_LOCK,
)
from app.db import SessionLocal, init_db  # noqa: E402
from app.services.pipeline import claim_next, queue_stats, recognize_image, resume_orphaned  # noqa: E402

LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("worktime.ocr_worker")

STARTED_AT = dt.datetime.now().isoformat(timespec="seconds")
STATS = {"processed": 0, "failed": 0, "last_image_id": None}
_stop = threading.Event()
_engine_ready = threading.Event()


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 落盘 + 轮转：worker 是后台进程，控制台窗口一关就什么都没有了。
    # 识别失败、重启自愈这些事必须事后能查（谁的第几张图、什么错）。
    fh = RotatingFileHandler(LOG_DIR / "ocr_worker.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


# ---------------------------------------------------------------- 单实例锁


def _acquire_single_instance():
    """拿单实例锁。返回 fd（拿到）或 None（已有实例在跑）。

    用操作系统级文件锁而不是「写个 pid 文件再检查」：后者在进程被强杀时
    会留下一个永远过不去的假 pid，人就只能去手工删文件。
    OS 锁在进程消失时自动释放，残留文件无害。
    """
    OCR_WORKER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(OCR_WORKER_LOCK), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if os.name == "nt":
            import msvcrt

            # msvcrt 锁的是「从当前文件位置开始的 N 字节」，所以先确保有 1 字节再定位回去
            if os.fstat(fd).st_size == 0:
                os.write(fd, b".")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - 生产是 Windows，顺手兼容 Linux 部署
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_single_instance(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------- 心跳


def _write_heartbeat(db=None) -> None:
    """写心跳。`/api/health` 靠它判断「OCR 进程还活着吗、引擎就绪吗」。

    用「写临时文件再原子替换」而不是直接覆盖：Web 进程随时可能在读，
    读到半个 JSON 会让健康检查报出假的引擎异常。
    """
    from app.ocr import engine as E

    payload = {
        "pid": os.getpid(),
        "started_at": STARTED_AT,
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "processed": STATS["processed"],
        "failed": STATS["failed"],
        "last_image_id": STATS["last_image_id"],
        "engine": E.engine_status(),
    }
    try:
        tmp = OCR_WORKER_HEARTBEAT.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, OCR_WORKER_HEARTBEAT)
    except OSError as exc:  # pragma: no cover - 磁盘满/权限问题
        log.warning("心跳写入失败：%s", exc)


def _heartbeat_loop() -> None:
    """后台每 10 秒刷新心跳。

    不能只在每轮循环写：一张大图识别要几十秒，期间主循环卡在 recognize 里，
    心跳就会「过期」，健康检查会误报 worker 已死。
    """
    while not _stop.is_set():
        _write_heartbeat()
        _stop.wait(10)


# ---------------------------------------------------------------- 启动


def _bootstrap(db) -> None:
    """启动自愈 + 组合登记。**只在这里做，Web 进程不做**。

    Web 开了多进程，这两件事若各跑一遍就是 N 倍写锁争抢（以及 N 份重复任务）。
    OCR worker 是单实例，天然只有一份。
    """
    resumed = resume_orphaned(db, max_attempts=OCR_MAX_ATTEMPTS)
    if resumed:
        log.warning("上次未跑完的 %d 张图已退回队列：%s", len(resumed), resumed)

    try:
        from app.services import combo_registry as CR

        synced = CR.sync_combos(db, actor="ocr-worker")
        if synced.get("created"):
            log.info("组合台账新登记 %s 条（默认草稿）", synced["created"])
        CR.backfill_product_ids(db)
        db.commit()
    except Exception:  # pragma: no cover - 登记失败不该阻止识别
        log.warning("启动时同步组合台账失败（不影响识别）", exc_info=True)
        db.rollback()


def _warmup() -> bool:
    """预加载引擎，让健康检查在第一批图到达前就能显示「就绪」。

    失败不退出：可能是 Paddle 没装好，但 Web 还能用（看单、报工都不需要引擎）。
    这里只把错误写进心跳，然后每 30 秒再试 —— 把服务直接弄死只会让人更懵。
    """
    from app.ocr import engine as E

    try:
        E._init_engine()
        _engine_ready.set()
        log.info("OCR 引擎就绪：%s", E.engine_status())
        return True
    except Exception as exc:
        log.error("OCR 引擎加载失败，暂不领取任务（每 30 秒重试）：%s", exc)
        return False


# ---------------------------------------------------------------- 主循环


def _run_once(db) -> bool:
    """处理一张图。返回 True 表示处理了，False 表示队列空。"""
    image_id = claim_next(db, max_attempts=OCR_MAX_ATTEMPTS)
    if image_id is None:
        return False
    log.info("领取 image=%s，开始识别", image_id)
    STATS["last_image_id"] = image_id
    db.close()  # recognize_image 自带会话；先放掉连接，别占着 SQLite 不放
    try:
        recognize_image(image_id)
        STATS["processed"] += 1
    except Exception:  # pragma: no cover - recognize_image 内部已兜底
        log.exception("识别异常 image=%s", image_id)
        STATS["failed"] += 1
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="独立 OCR 识别进程")
    ap.add_argument("--once", action="store_true", help="处理完当前队列就退出")
    ap.add_argument("--status", action="store_true", help="打印队列与心跳后退出")
    ap.add_argument("--max", type=int, default=0, help="最多处理 N 张后退出（0=不限）")
    args = ap.parse_args()

    _setup_logging()

    if args.status:
        db = SessionLocal()
        try:
            init_db()
            print(json.dumps(_status_snapshot(db), ensure_ascii=False, indent=2))
        finally:
            db.close()
        return 0

    lock_fd = _acquire_single_instance()
    if lock_fd is None:
        log.error("已有一个 OCR 进程在跑（锁文件 %s）。多开不会更快，只会抢 CPU。", OCR_WORKER_LOCK)
        return 2

    def _on_signal(signum, _frame):
        log.warning("收到信号 %s，处理完当前这张就退出", signum)
        _stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", signal.SIGTERM)):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # pragma: no cover - 非主线程/平台不支持
            pass

    db = SessionLocal()
    try:
        init_db()
        engine_ok = _warmup()
        _bootstrap(db)
        log.info("队列现状：%s", queue_stats(db))

        hb = threading.Thread(target=_heartbeat_loop, name="heartbeat", daemon=True)
        hb.start()

        handled = 0
        while not _stop.is_set():
            if not engine_ok:
                # 引擎不可用时不消费队列：否则每张图都会被判 failed，
                # 三次之后整批报废 —— 那是最糟的失败方式（数据没了，还得人工重传）。
                _stop.wait(30)
                engine_ok = _warmup()
                continue

            busy = _run_once(db)
            if busy:
                handled += 1
                if args.max and handled >= args.max:
                    log.info("已处理 %d 张，达到 --max，退出", handled)
                    break
                db = SessionLocal()  # _run_once 会关掉传入的会话
                continue
            if args.once:
                log.info("队列已空（--once），退出")
                break
            _stop.wait(OCR_POLL_SECONDS)

        log.info("OCR 进程退出，本次处理 %d 张", STATS["processed"])
    finally:
        _write_heartbeat()
        db.close()
        _release_single_instance(lock_fd)
    return 0


def _status_snapshot(db) -> dict:
    beat: dict = {}
    age = None
    if OCR_WORKER_HEARTBEAT.exists():
        age = round(time.time() - OCR_WORKER_HEARTBEAT.stat().st_mtime, 1)
        try:
            beat = json.loads(OCR_WORKER_HEARTBEAT.read_text(encoding="utf-8") or "{}")
        except Exception:
            beat = {}
    return {
        "queue": queue_stats(db),
        "worker": {"alive": age is not None and age < 60, "heartbeat_age_seconds": age, **beat},
        "data_dir": str(DATA_DIR),
    }


if __name__ == "__main__":
    raise SystemExit(main())
