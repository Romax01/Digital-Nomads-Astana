"""Аутентификация: PBKDF2-хеши паролей и подписанные HMAC-токены (без внешних зависимостей)."""
import base64
import hashlib
import hmac
import json
import os
import time

from fastapi import Depends, Header, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.errors import Unauthorized
from app.db import get_db
from app.models import User


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 120_000)
    return f"pbkdf2${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 120_000)
    return hmac.compare_digest(dk.hex(), dk_hex)


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue_token(user: User) -> str:
    payload = {"sub": user.id, "role": user.role, "exp": int(time.time()) + get_settings().token_ttl_hours * 3600}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(get_settings().secret_key.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def decode_token(token: str) -> dict:
    try:
        body, sig = token.split(".")
    except ValueError:
        raise Unauthorized("TOKEN_INVALID", "Недействительный токен доступа. Войдите заново.")
    expected = _b64(hmac.new(get_settings().secret_key.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        raise Unauthorized("TOKEN_INVALID", "Недействительный токен доступа. Войдите заново.")
    payload = json.loads(_unb64(body))
    if payload.get("exp", 0) < time.time():
        raise Unauthorized("TOKEN_EXPIRED", "Срок действия сеанса истёк. Войдите заново.")
    return payload


def user_from_token(db: Session, token: str | None) -> User:
    if not token:
        raise Unauthorized("AUTH_REQUIRED", "Требуется вход в систему.")
    payload = decode_token(token)
    user = db.get(User, payload["sub"])
    if not user or not user.active:
        raise Unauthorized("USER_INACTIVE", "Пользователь не найден или отключён.")
    return user


def current_user(authorization: str | None = Header(default=None),
                 token: str | None = Query(default=None, include_in_schema=False),
                 db: Session = Depends(get_db)) -> User:
    raw = None
    if authorization and authorization.lower().startswith("bearer "):
        raw = authorization[7:]
    raw = raw or token
    return user_from_token(db, raw)
