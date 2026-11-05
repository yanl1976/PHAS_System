"""登录、令牌与操作人身份。

设计要点：**身份只由服务端签发，不由请求体决定**。
以前的做法是各接口读 `data.user`，前端传什么就记什么 —— 谁都能把一笔驳回
写成别人的名字，月底追责时留痕等于没有。现在改成：

1. `/api/auth/login` 校验口令，签发一枚带过期时间的 HMAC 令牌；
2. 写请求必须带 `Authorization: Bearer <token>`，中间件解出操作人；
3. 审计写入时统一用中间件解出来的身份，覆盖调用方传进来的 user。

单工位部署（本机直连）允许 `X-Operator` 头匿名操作，避免一台机器上还要登两次；
需要严格身份时置 `WT_AUTH_REQUIRED=1, WT_TRUST_LOCAL=0`。
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
from contextvars import ContextVar

from .config import SECRET, TOKEN_TTL_HOURS

# 当前请求的操作人。中间件写入，审计落库时读取（见 models.AuditLog 的监听器）。
_operator: ContextVar[str] = ContextVar("operator", default="")
_operator_source: ContextVar[str] = ContextVar("operator_source", default="")

WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 登录接口本身当然不能要求已登录；健康检查给探活用。
OPEN_PATHS = {"/api/auth/login", "/api/health"}
# 只读账号也能写的极少数路径：改自己的密码。
# 不放开这一条，只读账号在密码过期后就没法自救了。
WRITE_ALLOW_READONLY = {"/api/auth/password"}


def set_operator(name: str, source: str = "token") -> None:
    _operator.set(name or "")
    _operator_source.set(source or "")


def current_operator(fallback: str = "operator") -> str:
    return _operator.get() or fallback


def operator_source() -> str:
    return _operator_source.get()


# ------------------------------------------------------------------ 口令
def hash_password(password: str, salt: str | None = None) -> str:
    """PBKDF2-HMAC-SHA256。存的是 `算法$盐$摘要`，不存明文。"""
    salt = salt or os.urandom(16).hex()
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt, digest = (stored or "").split("$", 2)
    except ValueError:
        return False
    if algo != "pbkdf2":
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return hmac.compare_digest(calc.hex(), digest)


# ------------------------------------------------------------------ 令牌
def _sign(body: str) -> str:
    return hmac.new(SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()


def make_token(username: str, epoch: int = 0, ttl_hours: int | None = None) -> dict:
    """签发令牌。epoch 用于「改密/停用后旧令牌立即失效」。"""
    ttl = TOKEN_TTL_HOURS if ttl_hours is None else ttl_hours
    exp = int((dt.datetime.now() + dt.timedelta(hours=ttl)).timestamp())
    payload = {"u": username, "e": epoch, "x": exp}
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    return {"token": f"{body}.{_sign(body)}", "expires_at": exp, "username": username}


def parse_token(token: str) -> dict | None:
    """验签 + 验过期。返回 payload，任何一步不过都返回 None。"""
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(body), sig):
        return None
    try:
        pad = "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(body + pad).decode())
    except (ValueError, TypeError):
        return None
    if int(payload.get("x") or 0) < int(dt.datetime.now().timestamp()):
        return None
    return payload


def bearer_of(header_value: str) -> str:
    value = (header_value or "").strip()
    if value.lower().startswith("bearer "):
        return value[7:].strip()
    return value


def is_loopback(host: str) -> bool:
    host = (host or "").split(":")[0]
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}
