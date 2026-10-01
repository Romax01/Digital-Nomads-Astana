"""Внутренняя шина доменных событий (in-process pub/sub).

Назначение: развязать модули backend. Изменяющие команды, приём телеметрии и движок
симуляции публикуют события («изменилось плановое состояние», «применена телеметрия»),
а сборщик состояния, детектор конфликтов и планировщик на них подписаны.

События публикуются только ПОСЛЕ фиксации транзакции (session after_commit), поэтому
подписчики никогда не видят незафиксированное состояние. Шина не гарантирует доставку
между процессами — источником истины остаётся БД; после перезапуска состояние
восстанавливается чтением БД (см. docs/realtime.md, ADR-0002).
"""
import logging
import threading
from collections import defaultdict
from typing import Callable

from sqlalchemy import event
from sqlalchemy.orm import Session

log = logging.getLogger("bus")
_subs: dict[str, list[Callable]] = defaultdict(list)
_lock = threading.Lock()


def subscribe(topic: str, fn: Callable):
    with _lock:
        _subs[topic].append(fn)


def clear():
    with _lock:
        _subs.clear()


def publish(topic: str, payload: dict | None = None):
    with _lock:
        fns = list(_subs.get(topic, [])) + list(_subs.get("*", []))
    for fn in fns:
        try:
            fn(topic, payload or {})
        except Exception:  # подписчик не должен ломать издателя
            log.exception("Ошибка подписчика шины на %s", topic)


def publish_after_commit(db: Session, topic: str, payload: dict | None = None):
    db.info.setdefault("pending_events", []).append((topic, payload or {}))


@event.listens_for(Session, "after_commit")
def _flush_events(session: Session):
    events = session.info.pop("pending_events", [])
    for topic, payload in events:
        publish(topic, payload)


@event.listens_for(Session, "after_rollback")
def _drop_events(session: Session):
    session.info.pop("pending_events", None)
