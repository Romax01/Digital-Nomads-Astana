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
            # данные сохраняются; добавляются только недостающие демо-учётки новых ролей и их привязки
            from app.models import Wagon
            from app.sim.seed import SPARE_WAGONS, ensure_demo_scopes, ensure_users, seed_spare_wagons
            from app.domain.topology import load_config
            ensure_users(db)
            main = db.execute(select(Station).where(Station.kind == "main")).scalar_one_or_none()
            if main:
                ensure_demo_scopes(db, main.id)
                if not db.get(Wagon, f"W{SPARE_WAGONS[0][0]}"):
                    sim = db.get(SimState, 1)
                    seed_spare_wagons(db, load_config(sim.station_config if sim else s.station_config))
            print("Демо-данные уже есть — пропуск (учётки работников проверены).")
            return
        sim = reset_world(db, a.config or s.station_config, a.scenario, a.seed if a.seed is not None else s.sim_seed,
                          real_time=s.sim_real_time)
        if s.sim_autostart and not s.sim_real_time:
            sim.running = True
            sim.speed = 5.0
        print(f"Демо-данные созданы: конфигурация {sim.station_config}, сценарий {sim.scenario}, seed {sim.seed}"
              + (", реальное время" if s.sim_real_time else ""))


if __name__ == "__main__":
    import os
    import sys
    main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Гарантированное завершение: после решения CP-SAT нативные потоки OR-Tools могли задерживать
    # выход интерпретатора на минуты, откладывая запуск backend (данные уже зафиксированы).
    os._exit(0)
