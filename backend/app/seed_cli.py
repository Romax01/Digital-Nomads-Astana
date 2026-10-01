"""Заполнение демо-данными: python -m app.seed_cli [--if-empty] [--scenario normal] [--seed 42] [--config large]."""
import argparse
import logging

from sqlalchemy import select

from app.config import get_settings
from app.db import session_scope
from app.models import SimState, Station
from app.sim.seed import reset_world


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--if-empty", action="store_true")
    ap.add_argument("--scenario", default="normal")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--config", default=None)
    a = ap.parse_args()
    s = get_settings()
    logging.basicConfig(level=logging.INFO)
    with session_scope() as db:
        if a.if_empty and db.execute(select(Station)).first():
            print("Демо-данные уже есть — пропуск.")
            return
        sim = reset_world(db, a.config or s.station_config, a.scenario, a.seed if a.seed is not None else s.sim_seed)
        if s.sim_autostart:
            sim.running = True
            sim.speed = 5.0
        print(f"Демо-данные созданы: конфигурация {sim.station_config}, сценарий {sim.scenario}, seed {sim.seed}")


if __name__ == "__main__":
    main()
