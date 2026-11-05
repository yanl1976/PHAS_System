"""工时图片识别 · 汇总核算系统 —— 应用入口。

**岗位分工（C1 之后）**

| 进程 | 干什么 | 不干什么 |
| --- | --- | --- |
| Web（uvicorn，可多 worker） | 接口、页面、上传落库、把图标成 pending | **不加载 PaddleOptical、不跑识别** |
| OCR worker（`scripts/ocr_worker.py`，单实例） | 领取 pending → 识别 → 落库；启动自愈；组合同步 | 不对外提供服务 |

队列就是 `image.status`。所以 Web 开几个 worker 都只有一条队列，
也不存在「同一个任务被 N 个进程各跑一遍」——那种事会导致一张纸写 N 份明细。
"""
from __future__ import annotations

import json
import logging
import os
import platform
import socket
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from .api import (
    admin,
    auth,
    report,
    cards,
    combos,
    dicts,
    intake,
    routes,
    sheets,
    summary,
    tasks,
    wages,
)
from .config import (
    AUTH_REQUIRED,
    BASE_DIR,
    DATA_DIR,
    EXPORT_DIR,
    PROCESSED_DIR,
    THUMB_DIR,
    TRUST_LOCAL,
    UPLOAD_DIR,
)
from .db import SessionLocal, init_db
from .domain import sys_roles
from .models import AppUser, Worker
from .security import (
    OPEN_PATHS,
    WRITE_ALLOW_READONLY,
    WRITE_METHODS,
    bearer_of,
    is_loopback,
    parse_token,
    set_operator,
)
from .seed import seed_all

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("worktime")


def _try_lock(path, timeout: float = 15.0) -> int | None:
    """尝试拿一个跨进程文件锁，等到 timeout 秒。返回 fd（拿到）或 None。

    OS 级文件锁在进程消失时自动释放，不会像「pid 文件」那样被强杀后留下
    一个永远过不去的假 pid（那种坑只能靠人去手工删文件）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b".")
    deadline = time.time() + timeout
    while True:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:  # pragma: no cover - 生产是 Windows
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.time() >= deadline:
                os.close(fd)
                return None
            time.sleep(0.1)


def _seed_once() -> None:
    """基础数据只让一个进程铺，其余进程等它铺完。

    为什么不能每个 worker 各铺一遍：`seed_all` 是「先查有没有、没有就建」，
    查与建之间没有唯一约束兜底（Role.code 就没有）。4 个 worker 同时进来会
    各插一份，角色下拉里就出现 4 个「管理员」，而且谁都说不清哪个是真的。
    """
    lock = _try_lock(DATA_DIR / "bootstrap.lock")
    if lock is None:
        # 拿到锁的进程正在铺；等不到就带病启动，总比整个 worker 起不来好
        log.warning("等待基础数据初始化超时，本进程跳过（另一个 worker 正在处理）")
        return
    db = SessionLocal()
    try:
        created = seed_all(db)
        made = auth.ensure_default_admin(db)
        db.commit()
        if any(created.values()):
            log.info("基础数据初始化：%s", created)
        if made:
            log.warning("已创建默认管理员账号（admin/admin123），请登录后立即修改密码")
    finally:
        db.close()
        try:
            os.lseek(lock, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        os.close(lock)


def _bootstrap(retries: int = 8) -> None:
    """建表 / 补列 / 建索引，容忍多进程同时启动。

    `uvicorn --workers 4` 是 4 个进程同时跑 lifespan。SQLite 只有一把写锁，
    `create_all`、`ALTER TABLE`、`CREATE INDEX` 都需要写 —— 不让它们排队的结果
    就是某个 worker 启动直接崩，表现成「服务开一半」。
    重试间隔递增，最坏约 10 秒，正常情况第一次就过。
    """
    last: Exception | None = None
    for i in range(retries):
        try:
            init_db()
            break
        except Exception as exc:  # pragma: no cover - 并发抢锁才走到这里
            last = exc
            log.warning("建表/补列第 %d 次失败（多为并发抢写锁）：%s", i + 1, exc)
            time.sleep(0.3 * (i + 1))
    else:
        if last is not None:
            raise last
    _seed_once()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 这里**不做**两件事，两者都移到了 OCR worker（它是单实例，天然只有一份）：
    #   * resume_orphaned()：多 worker 各跑一遍会把同一批图重复入队；
    #   * sync_combos()：会写库，4 个进程同时扫全量数据纯属抢锁。
    # 识别任务的队列在数据库里，Web 只负责往队列里放，不放也不捞。
    _bootstrap()
    yield


APP_NAME = "工时识别核算系统"
APP_SHORT = "工时识别核算"
APP_VERSION = "1.0.0"
# 进程启动时刻：顶栏/状态栏要显示「跑了多久」，重启后归零。放模块级而不是
# lifespan 里，是因为 `uvicorn --workers 4` 每个进程各算各的，写库反而要同步。
_STARTED_AT = time.time()

app = FastAPI(title=APP_NAME, version=APP_VERSION, lifespan=lifespan)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """解析身份。读开放、写要求登录（或本机免登录）。

    只做三件事：把「当前操作人」放进上下文、拦掉没身份的写请求、
    拦住只读账号的写请求。
    账号是否仍有效只在写请求时查一次库——读请求每秒几十次，
    每次都查库等于把 SQLite 当远程数据库用。
    """
    set_operator("", "")
    path = request.url.path
    if not path.startswith("/api/") or path in OPEN_PATHS:
        return await call_next(request)

    method = request.method.upper()
    is_write = method in WRITE_METHODS
    token = bearer_of(request.headers.get("authorization", ""))
    payload = parse_token(token) if token else None

    if payload is None:
        if token and is_write:
            return JSONResponse(
                {"detail": {"code": "TOKEN_INVALID", "message": "登录已过期，请重新登录"}},
                status_code=401,
            )
        if is_write:
            local_ok = TRUST_LOCAL and is_loopback(request.client.host if request.client else "")
            if AUTH_REQUIRED and not local_ok:
                return JSONResponse(
                    {"detail": {"code": "LOGIN_REQUIRED", "message": "请先登录后再操作"}},
                    status_code=401,
                )
            # 单工位部署：本机直连免登录，但身份来自请求头而不是请求体，
            # 且审计里会记成这个头里的人，前端不再能"顺手写成别人"。
            set_operator(request.headers.get("x-operator") or "本机操作员", "local")
        return await call_next(request)

    role = ""
    if is_write:
        db = SessionLocal()
        try:
            user = db.scalar(select(AppUser).where(AppUser.username == payload.get("u")))
            ok = bool(user and user.active and int(user.token_epoch or 0) == int(payload.get("e") or 0))
            role = (user.role if user else "") or ""
        finally:
            db.close()
        if not ok:
            return JSONResponse(
                {"detail": {"code": "ACCOUNT_INVALID", "message": "账号已停用或登录已失效，请重新登录"}},
                status_code=401,
            )
        # 只读账号**真的拦在这里**，不是靠界面把按钮变灰。
        # 唯一的例外是改自己的密码 —— 否则密码过期/要改密时这个账号就死锁了。
        if path not in WRITE_ALLOW_READONLY and not sys_roles.can(role, sys_roles.PERM_BUSINESS):
            return JSONResponse(
                {
                    "detail": {
                        "code": "READ_ONLY",
                        "message": (
                            f"当前账号的系统角色是「{sys_roles.system_role_name(role)}」，"
                            "只能查看，不能修改数据。需要录入或核对时，请让管理员调整系统角色。"
                        ),
                    }
                },
                status_code=403,
            )
    set_operator(str(payload.get("u") or ""), "token")
    return await call_next(request)


app.include_router(auth.router)
# CORS 最后注册 = 最外层：401 也要带上跨域头，否则浏览器只报 CORS 错误、
# 看不到「请先登录」这句话（本地 Vite 开发端口需要）。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(dicts.router)
app.include_router(admin.router)
app.include_router(intake.router)
app.include_router(sheets.router)
app.include_router(summary.router)
app.include_router(routes.router)
app.include_router(combos.router)
app.include_router(cards.router)
app.include_router(tasks.router)
app.include_router(wages.router)
app.include_router(report.router)

app.mount("/media/raw", StaticFiles(directory=str(UPLOAD_DIR)), name="raw")
app.mount("/media/processed", StaticFiles(directory=str(PROCESSED_DIR)), name="processed")
app.mount("/media/thumbs", StaticFiles(directory=str(THUMB_DIR)), name="thumbs")
app.mount("/media/exports", StaticFiles(directory=str(EXPORT_DIR)), name="exports")


@app.get("/api/health")
def health():
    """真探活：库读得动吗、队列堵没堵、OCR 进程还活着吗。

    以前这里只返回 `{"ok": true}` —— 库文件坏了、OCR 进程死了它照样说 ok。
    这种健康检查比没有更糟：它让人以为一切正常，等发现时已经积了一周的图。
    """
    from .config import OCR_WORKER_HEARTBEAT
    from .services.pipeline import queue_stats

    db_ok, err = True, ""
    stats: dict = {}
    teams: list[str] = []
    db = SessionLocal()
    try:
        db.execute(select(1)).all()
        stats = queue_stats(db)
        # 车间名不写死在代码里：组织信息的唯一真源是人员名册的 team 字段。
        # 取人数最多的那个当「本车间」——一人一品的小作坊也不会漏。
        rows = db.execute(
            select(Worker.team, func.count())
            .where(Worker.active.is_(True))
            .group_by(Worker.team)
            .order_by(func.count().desc())
        ).all()
        teams = [str(r[0]) for r in rows if r[0]]
    except Exception as exc:  # pragma: no cover - 库坏了才走到这里
        db_ok, err, stats = False, str(exc)[:200], {}
    finally:
        db.close()

    # OCR worker 每轮循环都会刷新心跳文件；60 秒没动静 = 挂了或压根没启动。
    beat: dict = {}
    beat_age: float | None = None
    if OCR_WORKER_HEARTBEAT.exists():
        beat_age = round(time.time() - OCR_WORKER_HEARTBEAT.stat().st_mtime, 1)
        try:
            beat = json.loads(OCR_WORKER_HEARTBEAT.read_text(encoding="utf-8") or "{}")
        except Exception:
            beat = {}
    worker_alive = beat_age is not None and beat_age < 60

    # 引擎状态只能由 worker 提供：Web 进程不加载 Paddle（那是 C1 的整个目的）。
    # 字段名保持 mode/loaded/version/error 不变，前端顶栏的状态灯不用改一行。
    ocr = beat.get("engine") or {
        "mode": "paddle",
        "loaded": False,
        "version": "",
        "error": "OCR 进程未启动（scripts/start_ocr_worker.bat）",
    }

    # 有活要干却没人干，才算不健康；队列空着只是没启动 worker，不算故障
    backlogged = int(stats.get("pending", 0)) > 0
    ok = db_ok and (worker_alive or not backlogged)

    return {
        "ok": ok,
        # 系统信息：顶栏与状态栏都要显示「这是什么系统、哪个版本、跑在哪台机器上」。
        # 从 health 一起下发而不是单开接口：这盏灯每 30 秒本来就在刷新，
        # 版本号跟着它走，就不会出现「页面写 v1.0、后端其实已经 v1.2」。
        "app": {
            "name": APP_NAME,
            "short": APP_SHORT,
            "version": APP_VERSION,
            "started_at": int(_STARTED_AT),
            "uptime_seconds": int(time.time() - _STARTED_AT),
            "host": socket.gethostname(),
            "python": platform.python_version(),
            # 登录方式决定顶栏写「本机免登录」还是「账号登录」
            "auth_mode": "password" if (AUTH_REQUIRED or not TRUST_LOCAL) else "local",
        },
        "org": {
            "workshop": teams[0] if teams else "2车间",
            "teams": teams,
        },
        "db": {"ok": db_ok, "error": err},
        "ocr": ocr,
        "worker": {
            "alive": worker_alive,
            "heartbeat_age_seconds": beat_age,
            "pid": beat.get("pid"),
            "processed": beat.get("processed", 0),
        },
        "queue": stats,
        "hint": "" if ok else (
            "数据库不可读" if not db_ok
            else f"有 {stats.get('pending')} 张图在排队但 OCR 进程没在跑，请启动 scripts/start_ocr_worker.bat"
        ),
    }


# 前端构建产物（npm run build 后可单端口部署）
DIST = BASE_DIR / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(DIST / "assets")), name="assets")

    @app.get("/")
    def index():
        return FileResponse(str(DIST / "index.html"))

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        # 未命中的 /api 路径不能回落到 index.html：否则前端拿到 200 的 HTML，
        # 解析成空数组，接口写错也看不见错误（排查成本极高）。
        if full_path.startswith("api/"):
            raise HTTPException(404, f"接口不存在：/{full_path}")
        candidate = DIST / full_path
        if candidate.is_file():
            return FileResponse(str(candidate))
        return FileResponse(str(DIST / "index.html"))
