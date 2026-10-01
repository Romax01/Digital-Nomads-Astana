"""Шина событий: публикация только после фиксации ВНЕШНЕЙ транзакции (регрессия самоблокировки
при сбросе мира: SAVEPOINT внутри сброса рассылал события, подписчик ждал блокировок того же потока)."""
from sqlalchemy import text

from app.core import bus
from app.db import SessionLocal


def test_events_wait_for_outer_commit_and_survive_savepoint_rollback():
    got = []
    bus.subscribe("test_topic", lambda t, p: got.append(p["n"]))
    with SessionLocal() as db:
        db.execute(text("select 1"))
        bus.publish_after_commit(db, "test_topic", {"n": 1})
        with db.begin_nested():          # SAVEPOINT фиксируется — событие ещё не публикуется
            db.execute(text("select 2"))
        assert got == []
        try:
            with db.begin_nested():      # откат SAVEPOINT не стирает события внешней транзакции
                raise RuntimeError
        except RuntimeError:
            pass
        assert got == []
        db.commit()                      # фиксация внешней транзакции — публикация
    assert got == [1]
    with SessionLocal() as db:
        db.execute(text("select 1"))
        bus.publish_after_commit(db, "test_topic", {"n": 2})
        db.rollback()                    # откат внешней транзакции — событие отменено
        db.commit()
    assert got == [1]
