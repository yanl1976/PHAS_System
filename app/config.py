"""全局配置。所有口径参数集中在此，便于后期调整。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
PROCESSED_DIR = DATA_DIR / "processed"
THUMB_DIR = DATA_DIR / "thumbs"
EXPORT_DIR = DATA_DIR / "exports"
BACKUP_DIR = Path(os.getenv("WT_BACKUP_DIR", str(DATA_DIR / "backups")))

for _d in (DATA_DIR, UPLOAD_DIR, PROCESSED_DIR, THUMB_DIR, EXPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _copy_db(src: Path, dst: Path) -> None:
    """用 SQLite 在线备份接口拷库。

    不能直接 `cp`：库跑在 WAL 模式，未 checkpoint 的已提交事务还在 -wal 里，
    直接拷主文件会得到一个「表都没有」的空壳（真踩过一次）。
    """
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(dst)
    try:
        s.backup(d)
    finally:
        d.close()
        s.close()


def _isolate_test_db() -> str | None:
    """测试脚本默认跑在库副本上，生产库只读。

    以前所有 `scripts/test_*.py` 都直连 data/worktime.db：一次跑全套就多出几张
    测试单据、把真实单据的确认人改成 tester。靠「大家记得先备份」是管不住的，
    所以把隔离做成默认行为——脚本名以 test_ 开头就自动拷一份副本给它用。
    显式传了 WT_DB_URL（如 scripts/run_tests.py）时以环境变量为准。
    """
    if os.getenv("WT_DB_URL"):
        return None
    prog = Path(sys.argv[0] or "").name
    if not prog.startswith("test_"):
        return None
    src = DATA_DIR / "worktime.db"
    if not src.exists():
        return None
    dst = Path(tempfile.gettempdir()) / f"hy_test_{os.getpid()}_{prog}.db"
    try:
        _copy_db(src, dst)
    except Exception as exc:  # pragma: no cover - 副本都拷不出来时别硬闯生产库
        print(f"[test] 库副本创建失败（{exc}），改用只读提示后继续")
        return None
    import atexit

    def _cleanup() -> None:
        for suffix in ("", "-wal", "-shm"):
            q = Path(str(dst) + suffix)
            try:
                if q.exists():
                    q.unlink()
            except OSError:
                pass

    atexit.register(_cleanup)
    print(f"[test] 使用库副本 {dst.name}（生产库只读，不改动）")
    return f"sqlite:///{dst.as_posix()}"


_TEST_DB_URL = _isolate_test_db()

DB_URL = os.getenv("WT_DB_URL", _TEST_DB_URL or f"sqlite:///{(DATA_DIR / 'worktime.db').as_posix()}")

# ---------------------------------------------------------------- 核算口径
# 口径 1：只有单面文字明确写了「天/日」时才做折算（实测真实单写「25H」直接带小时单位，
# 不走此分支）。1 个工作日按 8 小时计，符合车间实际排班。
HOURS_PER_DAY = float(os.getenv("WT_HOURS_PER_DAY", "8"))
# 口径 3：返修件折算系数（默认 0，即不计工时；可按需改为 0.5）
REWORK_FACTOR = float(os.getenv("WT_REWORK_FACTOR", "0.0"))
SCRAP_FACTOR = float(os.getenv("WT_SCRAP_FACTOR", "0.0"))
# 口径 5：是否启用金额核算（工时 x 工价）
ENABLE_AMOUNT = os.getenv("WT_ENABLE_AMOUNT", "0") == "1"
# 勾稽容差（小时）
RECONCILE_TOLERANCE = float(os.getenv("WT_RECONCILE_TOLERANCE", "0.5"))
# ------------------------------------------------ 单件（定额）工时合理性阈值
# 定额工时是工艺给定的标准值，一件活可以跨多个工作日，**不能用「一天几小时」封顶**。
# 实测 9 月真实数据：单件工时中位数 0.67h、p95 为 10h、最大 96h（对辊镗 4 个班），
# 且真实单据手写为「25H」这类直接带小时单位的写法。据此设两级：
#   > UNIT_HOURS_WARN  → 提示：跨天件，人工确认即可放行
#   > UNIT_HOURS_LIMIT → 阻断：超出单人单月可用工时，几乎必然是漏了小数点或多认一位
UNIT_HOURS_WARN = float(os.getenv("WT_UNIT_HOURS_WARN", "8"))       # 1 个工作日
UNIT_HOURS_LIMIT = float(os.getenv("WT_UNIT_HOURS_LIMIT", "200"))   # 约 25 个工作日

# ---------------------------------------------------------------- 识别参数
OCR_LANG = os.getenv("WT_OCR_LANG", "ch")
# 模型选择：v6_small 在 CPU（关闭 oneDNN）上单张约 5s，精度与 v6_medium 接近；
# 换成 PP-OCRv6_medium_* 精度略高但慢 8 倍，按需通过环境变量切换。
OCR_DET_MODEL = os.getenv("WT_OCR_DET_MODEL", "PP-OCRv6_small_det")
OCR_REC_MODEL = os.getenv("WT_OCR_REC_MODEL", "PP-OCRv6_small_rec")
# 检测端最长边限制：手写单 960 足够，过大只增加耗时
OCR_DET_LIMIT_SIDE = int(os.getenv("WT_OCR_DET_LIMIT", "960"))
MAX_LONG_SIDE = 2000
# 红笔抑制阈值：R - max(G,B) 超过该值且 R 足够亮，判定为红色笔迹
RED_DELTA = 40
RED_MIN = 90
# 置信度分级阈值
CONF_HIGH = 0.95
CONF_MID = 0.70

# ---------------------------------------------------------------- 识别队列（C1）
# OCR 已从 Web 进程拆成独立进程，队列是 `image.status`。这几个参数决定 worker 行为。
# 重试上限：一张毒图（超大图/引擎段错误）每次被捞回来都可能把进程带走，
# 没有上限就会「启动 → 崩溃 → 启动」死循环，整个服务永远起不来。
OCR_MAX_ATTEMPTS = int(os.getenv("WT_OCR_MAX_ATTEMPTS", "3"))
# 空闲时的轮询间隔。队列在数据库里，只能轮询；0.5s 对人是无感的，
# 再短就是白白让 SQLite 每秒醒来几十次。
OCR_POLL_SECONDS = float(os.getenv("WT_OCR_POLL", "0.5"))
# 单实例锁 + 心跳：多开一个 worker 不会「更快」，只会抢 CPU 并各自多背 400MB
# （实测两进程并行识别吞吐不涨）。所以用文件锁明确拒绝第二个。
OCR_WORKER_LOCK = DATA_DIR / "ocr_worker.lock"
OCR_WORKER_HEARTBEAT = DATA_DIR / "ocr_worker.heartbeat"
# Web 进程数。默认 4：12 核实测 4 worker 已有 5~7 倍收益，再多内存先不够。
WEB_WORKERS = int(os.getenv("WT_WEB_WORKERS", "4"))

# 允许的图片后缀
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff"}

# ---------------------------------------------------------------- 身份与留痕
# 审计里的「操作人」必须来自登录会话，不能是前端随手传一个字符串 ——
# 否则谁都能把一笔操作写成别人的名字，出事时留痕等于白记。
# WT_AUTH_REQUIRED=0 只用于单机演示/临时排障，会在界面上明确提示「未启用登录」。
AUTH_REQUIRED = os.getenv("WT_AUTH_REQUIRED", "1") == "1"
# 本机（127.0.0.1）直连时允许用 X-Operator 头匿名操作：单工位部署不该被登录卡死。
# 需要严格身份时置 WT_TRUST_LOCAL=0。
TRUST_LOCAL = os.getenv("WT_TRUST_LOCAL", "1") == "1"
TOKEN_TTL_HOURS = int(os.getenv("WT_TOKEN_TTL_HOURS", "12"))


def _load_secret() -> str:
    """签名密钥：优先环境变量，其次落盘文件（首次自动生成）。

    写文件而不是每次随机生成——否则服务一重启，所有登录态失效，
    人正在核对一张单就被踢回登录页。
    """
    env = os.getenv("WT_SECRET", "")
    if env:
        return env
    path = DATA_DIR / ".secret"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    import secrets

    value = secrets.token_hex(32)
    path.write_text(value, encoding="utf-8")
    return value


SECRET = _load_secret()
DEFAULT_ADMIN = os.getenv("WT_DEFAULT_ADMIN", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("WT_DEFAULT_ADMIN_PASSWORD", "admin123")

