"""
用户认证模块 — 多用户支持。

功能：
- 用户注册、登录
- 密码哈希（SHA-256 + salt）
- JWT 风格 token 签发与验证（HMAC-SHA256）
- 用户数据持久化（JSON 文件）

零外部依赖，只用 Python 标准库。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from pathlib import Path

# Token 密钥（持久化到文件，重启后一致）
_SECRET_KEY: str | None = None
_SECRET_KEY_PATH: str | None = None

# Token 过期时间（秒）
TOKEN_EXPIRE_SECONDS = 7 * 24 * 3600  # 7 天


def _get_secret_key(key_path: str = "") -> str:
    global _SECRET_KEY, _SECRET_KEY_PATH
    if _SECRET_KEY is not None and _SECRET_KEY_PATH == key_path:
        return _SECRET_KEY
    path = Path(key_path) if key_path else Path(os.path.dirname(__file__)) / ".." / "runs" / ".auth_secret"
    if path.exists():
        _SECRET_KEY = path.read_text(encoding="utf-8").strip()
    else:
        _SECRET_KEY = secrets.token_hex(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_SECRET_KEY, encoding="utf-8")
    _SECRET_KEY_PATH = key_path
    return _SECRET_KEY


# ---------------------------------------------------------------------------
# 用户存储
# ---------------------------------------------------------------------------

def _users_path(runs_base_dir: str = "") -> Path:
    base = Path(runs_base_dir) if runs_base_dir else Path(os.path.dirname(__file__)) / ".." / "runs"
    return base / "users.json"


def load_users(runs_base_dir: str = "") -> dict:
    """从 JSON 文件加载所有用户。"""
    path = _users_path(runs_base_dir)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_users(users: dict, runs_base_dir: str = "") -> None:
    """保存所有用户到 JSON 文件。"""
    path = _users_path(runs_base_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(users, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# 密码处理
# ---------------------------------------------------------------------------

def _hash_password(password: str) -> str:
    """使用 SHA-256 + 随机 salt 哈希密码。"""
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${h}"


def _verify_password(password: str, hashed: str) -> bool:
    """验证密码。"""
    try:
        salt, h = hashed.split("$", 1)
        return hmac.compare_digest(
            hashlib.sha256((salt + password).encode("utf-8")).hexdigest(),
            h,
        )
    except (ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Token 处理（HMAC-SHA256 JWT 风格）
# ---------------------------------------------------------------------------

def _b64encode(data: bytes) -> str:
    """URL-safe base64 编码（无 padding）。"""
    return base64_url_encode(data)


def _b64decode(s: str) -> bytes:
    """URL-safe base64 解码。"""
    return base64_url_decode(s)


def base64_url_encode(data: bytes) -> str:
    return secrets.token_urlsafe(len(data)).translate(str.maketrans("", "", "="))  # noqa
    # Actually use standard base64url
    import base64 as _b64
    return _b64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def base64_url_decode(s: str) -> bytes:
    import base64 as _b64
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return _b64.urlsafe_b64decode(s)


def _create_token(payload: dict, secret: str) -> str:
    """创建 HMAC-SHA256 签名的 token。"""
    import base64 as _b64
    header = _b64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    body = _b64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False).encode()).rstrip(b"=").decode()
    signature = hmac.new(
        secret.encode("utf-8"),
        f"{header}.{body}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    sig_b64 = _b64.urlsafe_b64encode(signature).rstrip(b"=").decode()
    return f"{header}.{body}.{sig_b64}"


def _verify_token(token: str, secret: str) -> dict | None:
    """验证 token，返回 payload 或 None。"""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header_b64, body_b64, sig_b64 = parts

    # 验证签名
    expected_sig = hmac.new(
        secret.encode("utf-8"),
        f"{header_b64}.{body_b64}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    import base64 as _b64
    try:
        actual_sig = _b64.urlsafe_b64decode(sig_b64 + "=" * (4 - len(sig_b64) % 4 or 4))
    except Exception:
        return None
    if not hmac.compare_digest(expected_sig, actual_sig):
        return None

    # 解析 payload
    try:
        body_padded = body_b64 + "=" * (4 - len(body_b64) % 4 or 4)
        payload = json.loads(_b64.urlsafe_b64decode(body_padded))
    except (json.JSONDecodeError, Exception):
        return None

    # 检查过期
    if payload.get("exp", 0) < time.time():
        return None

    return payload


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def register_user(username: str, password: str, runs_base_dir: str = "") -> dict:
    """注册新用户。返回用户信息或抛出异常。"""
    username = username.strip()
    if not username or len(username) < 2:
        raise ValueError("用户名至少 2 个字符")
    if len(password) < 4:
        raise ValueError("密码至少 4 个字符")

    users = load_users(runs_base_dir)
    if username in users:
        raise ValueError("用户名已存在")

    user = {
        "id": str(uuid.uuid4())[:12],
        "username": username,
        "password_hash": _hash_password(password),
        "created_at": int(time.time() * 1000),
    }
    users[username] = user
    save_users(users, runs_base_dir)
    return {"id": user["id"], "username": user["username"]}


def login_user(username: str, password: str, runs_base_dir: str = "", secret_key: str = "") -> dict | None:
    """用户登录。成功返回带 token 的用户信息，失败返回 None。"""
    username = username.strip()
    users = load_users(runs_base_dir)
    user = users.get(username)
    if not user:
        return None
    if not _verify_password(password, user["password_hash"]):
        return None

    secret = secret_key or _get_secret_key(str(_users_path(runs_base_dir).parent / ".auth_secret"))
    now = int(time.time())
    payload = {
        "user_id": user["id"],
        "username": user["username"],
        "iat": now,
        "exp": now + TOKEN_EXPIRE_SECONDS,
    }
    token = _create_token(payload, secret)
    return {
        "token": token,
        "user": {"id": user["id"], "username": user["username"]},
        "expires_at": payload["exp"] * 1000,
    }


def verify_token(token: str, runs_base_dir: str = "", secret_key: str = "") -> dict | None:
    """验证 token，返回 payload 或 None。"""
    secret = secret_key or _get_secret_key(str(_users_path(runs_base_dir).parent / ".auth_secret"))
    return _verify_token(token, secret)
