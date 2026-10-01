"""Защита от повторной отправки команды.

Клиент передаёт заголовок Idempotency-Key. Первый запрос с ключом выполняется и его ответ
сохраняется; повтор с тем же ключом и тем же телом возвращает сохранённый ответ без
повторного действия. Повтор с другим телом — ошибка IDEMPOTENCY_KEY_REUSED.
Ключ вставляется в ту же транзакцию, что и действие: конкурентный дубль упрётся в PK.
"""
import hashlib
import json
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError, Conflict
from app.core.timeutil import utcnow
from app.db import SessionLocal
from app.models import IdempotencyKey, User


def body_hash(body: Any) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def run_idempotent(db: Session, user: User, key: str | None, endpoint: str, body: Any,
                   action: Callable[[], dict]) -> dict:
    if not key:
        result = action()
        db.commit()
        return result
    h = body_hash(body)
    existing = db.execute(select(IdempotencyKey).where(
        IdempotencyKey.key == key, IdempotencyKey.user_id == user.id)).scalar_one_or_none()
    if existing:
        if existing.request_hash != h or existing.endpoint != endpoint:
            raise Conflict("IDEMPOTENCY_KEY_REUSED",
                           "Ключ повтора уже использован для другого запроса.",
                           hint="Сформируйте новый Idempotency-Key для нового действия.")
        if existing.response is None:
            raise Conflict("REQUEST_IN_PROGRESS", "Такая же команда уже выполняется.",
                           hint="Дождитесь результата первой отправки.")
        return {**existing.response, "idempotent_replay": True}
    rec = IdempotencyKey(key=key, user_id=user.id, endpoint=endpoint, request_hash=h, created_at=utcnow())
    db.add(rec)
    try:
        db.flush()
    except IntegrityError:
        # Конкурентный дубль: первая копия уже зафиксирована — возвращаем её результат.
        db.rollback()
        done = db.execute(select(IdempotencyKey).where(
            IdempotencyKey.key == key, IdempotencyKey.user_id == user.id)).scalar_one_or_none()
        if done and done.response is not None and done.request_hash == h:
            return {**done.response, "idempotent_replay": True}
        raise Conflict("REQUEST_IN_PROGRESS", "Такая же команда уже выполняется.",
                       hint="Дождитесь результата первой отправки.")
    try:
        result = action()
    except AppError:
        db.rollback()
        raise
    rec.response = json.loads(json.dumps(result, default=str))
    rec.status_code = 200
    db.commit()
    return result


def cleanup_old(hours: int = 48):
    from datetime import timedelta
    from sqlalchemy import delete
    with SessionLocal() as s:
        s.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < utcnow() - timedelta(hours=hours)))
        s.commit()
