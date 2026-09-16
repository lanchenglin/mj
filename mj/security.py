"""Single-workspace authentication. Credentials never enter project manifests."""
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import HTTPException, Request
from sqlalchemy import select
from .db import LoginSession, now


def initialize_admin(data_dir: Path, password: str | None = None) -> str | None:
    path = data_dir / "admin.json"
    if path.exists():
        return None
    password = password or secrets.token_urlsafe(24)
    if len(password) < 12:
        raise ValueError("Admin password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    hashed = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump({"salt": salt.hex(), "password_hash": hashed.hex()}, file)
    return password


def password_ok(settings, password: str) -> bool:
    path = settings.data_dir / "admin.json"
    if not path.exists():
        return False
    stored = json.loads(path.read_text())
    hashed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(stored["salt"]), n=16384, r=8, p=1)
    return hmac.compare_digest(hashed.hex(), stored["password_hash"])


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def require_session(request: Request) -> str:
    cookie = request.cookies.get("mj_session", "")
    with request.app.state.sessions() as db:
        session = db.get(LoginSession, token_hash(cookie)) if cookie else None
        if session is None or session.expires <= now():
            raise HTTPException(401, detail={"code": "LOGIN_REQUIRED", "message": "请先登录。"})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("x-csrf-token", "")
            if not hmac.compare_digest(csrf, session.csrf):
                raise HTTPException(403, detail={"code": "CSRF", "message": "请求校验失败，请刷新后重试。"})
        return "admin"


class LoginLimiter:
    """Bounded per-process limiter; deploy one API process behind a rate-limited proxy."""
    def __init__(self):
        self.attempts = {}

    def check(self, key):
        current = time.monotonic()
        self.attempts = {k: [x for x in v if x > current - 300] for k, v in self.attempts.items()
                         if any(x > current - 300 for x in v)}
        recent = self.attempts.setdefault(key, [])
        if len(recent) >= 8 or len(self.attempts) > 10000:
            raise HTTPException(429, detail={"code": "RATE_LIMIT", "message": "登录过于频繁，请稍后重试。"})
        recent.append(current)


def check_origin(request):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, detail={"code": "ORIGIN", "message": "不允许跨站写入。"})


class BodyLimitMiddleware:
    """Bound request bodies even without Content-Length (including chunked uploads)."""
    def __init__(self, app, max_upload_bytes):
        self.app, self.max_upload_bytes = app, max_upload_bytes

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope['method'] in {'GET', 'HEAD', 'OPTIONS'}:
            return await self.app(scope, receive, send)
        from starlette.responses import JSONResponse
        maximum = self.max_upload_bytes + 1024*1024 if scope['path'].endswith('/assets') else 2*1024*1024
        messages, size = [], 0
        while True:
            message = await receive()
            if message['type'] == 'http.disconnect':
                return
            size += len(message.get('body', b''))
            if size > maximum:
                return await JSONResponse({'error': {'code':'BODY_LIMIT','message':'请求体超出限制。'}}, 413)(scope, receive, send)
            messages.append(message)
            if not message.get('more_body', False):
                break
        index = 0
        async def bounded_receive():
            nonlocal index
            if index < len(messages):
                result = messages[index]; index += 1
                return result
            return await receive()
        await self.app(scope, bounded_receive, send)
