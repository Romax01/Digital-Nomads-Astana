"""Интеллектуальный помощник диспетчера.

Расчёты и разрешения выполняют серверные правила (проверка заявок, детектор конфликтов,
планировщик). Помощник только определяет вопрос, запускает нужный расчёт и объясняет
результат по шаблонам. Внешняя языковая модель (если подключена адаптером) получает только
вычисленные факты и может лишь переформулировать текст; факты и действия остаются из расчёта.
Если данных не хватает — помощник прямо называет, чего не хватает, и не придумывает значения.
"""
from __future__ import annotations

import re
import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.timeutil import iso, local_hm, utcnow
from app.models import Incident, TransferRequest
from app.services.checker import check_request, train_length_for_request
from app.services.conflicts import detect
from app.services.model import KIND_LABEL, StationModel

SAMPLES = [
    "Почему нельзя принять этот состав?",
    "Когда появится ближайшее окно?",
    "Что произойдёт, если путь 3 закроется на час?",
    "Какие операции создают перегрузку?",
    "Как уменьшить задержку?",
]


def classify(q: str) -> str:
    t = q.lower()
    if re.search(r"закро|закрыт|если\s+путь|выйдет из строя", t):
        return "what_if_close"
    if re.search(r"окн|когда", t):
        return "nearest_window"
    if re.search(r"почему|нельзя|не\s*мож|отказ|недоступ", t):
        return "why_reject"
    if re.search(r"перегруз|загруз|узк|не\s*хватает", t):
        return "overload"
    if re.search(r"задержк|уменьш|сократ|ускор|опаздыва", t):
        return "reduce_delay"
    return "help"


def _request(db: Session, ctx: dict) -> TransferRequest | None:
    if ctx.get("request_id"):
        return db.get(TransferRequest, ctx["request_id"])
    return db.execute(select(TransferRequest).where(TransferRequest.status.in_(["new", "checked", "confirmed"]))
                      .order_by(TransferRequest.updated_at.desc())).scalars().first()


def answer(db: Session, question: str, ctx: dict | None = None) -> dict:
    ctx = ctx or {}
    model = StationModel(db)
    intent = classify(question)
    fn = {"why_reject": _why, "nearest_window": _window, "what_if_close": _what_if, "overload": _overload,
          "reduce_delay": _reduce, "help": _help}[intent]
    out = fn(db, model, question, ctx)
    out.update({"id": uuid.uuid4().hex[:10], "intent": intent, "question": question, "computed_at": iso(model.now),
                "computed_real_at": iso(utcnow()), "based_on_version": model.version})
    out.setdefault("missing_data", [])
    out.setdefault("actions", [])
    out["provider"] = "template"
    from app.services.llm import rephrase
    llm_text = rephrase(question, out)
    if llm_text:
        out["text_llm"] = llm_text
        out["provider"] = "llm+template"
    return out


def _why(db, model, q, ctx):
    r = _request(db, ctx)
    if not r:
        return {"text": "Не найдена заявка для проверки. Откройте заявку или укажите её в вопросе.",
                "facts": [], "missing_data": ["заявка на приём"]}
    res = check_request(model, r)
    fails = [i for i in res["items"] if i["status"] == "fail"]
    warns = [i for i in res["items"] if i["status"] == "warning"]
    miss = res["missing_data"]
    if res["decision"].startswith("available"):
        text = f"Заявку {r.number} принять можно. {res['summary']}"
    elif res["decision"] == "insufficient_data":
        text = f"По заявке {r.number} недостаточно данных для решения: " + "; ".join(miss)
    else:
        text = f"Заявку {r.number} принять нельзя. {res['summary']}"
    facts = [f"{i['title']}: {i['message']}" for i in fails + warns]
    for tr in res["tracks"][:6]:
        facts.append(tr["message"])
    actions = []
    for a in res["alternatives"]:
        facts.append(f"Альтернатива — {a['title']}: задержка {a['effect'].get('delay_min', 0)} мин; {a['effect'].get('load_change', '')}")
        actions.append({"type": "open_request", "label": a["title"], "request_id": r.id})
    return {"text": text, "facts": facts, "missing_data": miss, "actions": actions[:3],
            "source": "Проверка ограничений заявки (сервер)", "objects": [{"type": "request", "id": r.id}]}


def _window(db, model, q, ctx):
    from app.services.alternatives import nearest_window
    r = _request(db, ctx)
    if not r:
        return {"text": "Укажите заявку — окно рассчитывается для конкретного состава.", "facts": [],
                "missing_data": ["заявка (число вагонов, длина, станция отправления)"]}
    res = check_request(model, r, with_alternatives=False)
    length, _ = train_length_for_request(model, r)
    if length is None:
        return {"text": "Невозможно рассчитать окно: неизвестна длина состава.", "facts": [],
                "missing_data": ["длина состава или род вагонов"]}
    arr = res["arrival"]
    from datetime import datetime
    arr_dt = datetime.fromisoformat(arr.replace("Z", "+00:00"))
    if res["decision"].startswith("available"):
        return {"text": f"Запрошенное время уже допустимо: прибытие в {local_hm(arr_dt)}. {res['summary']}",
                "facts": [], "source": "Проверка ограничений заявки"}
    t, dep, pr = nearest_window(model, model.build_book(), r, arr_dt, length, r.wagons_count)
    if not t:
        return {"text": "В ближайшие 12 ч допустимого окна нет: все подходящие пути, маршруты или ресурсы заняты.",
                "facts": [x["message"] for x in res["tracks"][:5]], "source": "Поиск окна с шагом 5 мин"}
    tr = pr.ops[0].track_id
    return {"text": f"Ближайшее допустимое окно — прибытие в {local_hm(t)} (отправление со станции отправления в "
                    f"{local_hm(dep)}), {model.track_label_lc(tr)}. Задержка относительно запроса — "
                    f"{int((t - arr_dt).total_seconds() // 60)} мин.",
            "facts": [f"Проверен весь интервал: {KIND_LABEL[o.kind].lower()} {local_hm(o.start)}–{local_hm(o.end)} "
                      f"({model.track_label_lc(o.track_id)})" for o in pr.ops],
            "actions": [{"type": "postpone", "label": f"Перенести отправление на {local_hm(dep)}", "request_id": r.id,
                         "departure": iso(dep)}],
            "source": "Поиск окна с шагом 5 мин, полная проверка ограничений"}


def _parse_track(model, q):
    m = re.search(r"пут[ьиюя]\s*(?:№\s*)?([0-9]+|I{1,2})", q, re.I)
    if not m:
        return None
    num = m.group(1).upper()
    return next((tid for tid, t in model.track_rows.items() if t.number == num), None)


def _parse_minutes(q):
    t = q.lower()
    m = re.search(r"(\d+)\s*мин", t)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*ч", t)
    if m:
        return int(float(m.group(1).replace(",", ".")) * 60)
    if "полчаса" in t:
        return 30
    if "час" in t:
        return 60
    return None


def _what_if(db, model, q, ctx):
    tid = _parse_track(model, q) or ctx.get("track_id")
    mins = _parse_minutes(q)
    missing = []
    if not tid:
        missing.append("номер пути (например, «путь 3»)")
    if not mins:
        missing.append("продолжительность закрытия (например, «на час» или «на 40 мин»)")
    if missing:
        return {"text": "Для расчёта сценария «что если» не хватает данных.", "facts": [], "missing_data": missing}
    before = detect(model)
    hypo = Incident(id="WHATIF", station_id=model.sid, kind="track_closure", title=f"Гипотетическое закрытие: {model.track_label_lc(tid)}",
                    object_type="track", object_id=tid, start_at=model.now, end_at=model.now + timedelta(minutes=mins),
                    status="active", severity="high", params={}, created_at=utcnow(), description="")
    model.incidents = list(model.incidents) + [hypo]
    after = detect(model)
    new = [c for c in after["conflicts"] if c["id"] not in {x["id"] for x in before["conflicts"]}]
    affected_ops = sorted({o for c in new for o in c["operations"] if o in model.ops})
    facts = [c["explanation"] for c in new[:6]]
    text = (f"Если {model.track_label_lc(tid)} закроется на {mins} мин (с {local_hm(model.now)} до "
            f"{local_hm(model.now + timedelta(minutes=mins))}), появится конфликтов: {len(new)}; "
            f"затронуто операций: {len(affected_ops)}.")
    plan_info = None
    if new:
        from app.services.planner import run_planner
        db.commit()  # не держать транзакцию на время расчёта (объекты модели остаются доступны)
        res = run_planner(model, time_limit=2.0, trigger="what_if")
        sa, sb = res["summary"]["after"], res["summary"]["before"]
        plan_info = {"status": res["status_label"], "changes": len(res["changes"]), "delay_after": sa["total_delay_min"],
                     "delay_before": sb["total_delay_min"]}
        text += (f" Планировщик ({res['status_label'].lower()}, {res['solve_ms']:.0f} мс) находит план с изменением "
                 f"{len(res['changes'])} операций; суммарная задержка отправлений — {sa['total_delay_min']:.0f} мин "
                 f"(до инцидента по прогнозу {sb['total_delay_min']:.0f} мин). Изменение в систему не вносится.")
    else:
        text += " Запланированных операций на этом пути в указанный период нет."
    return {"text": text, "facts": facts, "plan": plan_info, "objects": [{"type": "track", "id": tid}],
            "source": "Детектор конфликтов на гипотетическом состоянии + планировщик (без сохранения)"}


def _overload(db, model, q, ctx):
    det = detect(model)
    load: dict[str, float] = {}
    h0, h1 = model.now, model.now + timedelta(hours=4)
    fc = det["forecast"]
    for o in model.ops.values():
        if o.status in ("done", "cancelled") or o.id not in fc:
            continue
        s, e = fc[o.id]
        a, b = max(s, h0), min(e, h1)
        if b > a:
            for rid in o.resource_ids or []:
                load[rid] = load.get(rid, 0) + (b - a).total_seconds() / 60
    top_res = sorted(load.items(), key=lambda kv: -kv[1])[:4]
    facts = [f"{model.resources[r].name}: занят {m:.0f} из 240 мин ближайших 4 ч ({m / 240:.0%})" for r, m in top_res]
    conf = det["conflicts"][:5]
    facts += [f"{c['title']}: {c['explanation']}" for c in conf]
    ops = {}
    for c in det["conflicts"]:
        for oid in c["operations"]:
            if oid in model.ops:
                ops[oid] = ops.get(oid, 0) + 1
    worst = sorted(ops.items(), key=lambda kv: -kv[1])[:5]
    lines = []
    for oid, n in worst:
        o = model.ops[oid]
        t = model.trains.get(o.train_id) if o.train_id else None
        lines.append(f"{KIND_LABEL.get(o.kind)} {('поезда № ' + t.number) if t else ''} ({model.track_label_lc(o.track_id)}) — в {n} конфликт(ах)")
    text = ("Перегрузку создают: " + "; ".join(lines) + ".") if lines else \
        "Конфликтов в прогнозе нет; наиболее загруженные ресурсы перечислены ниже."
    return {"text": text, "facts": facts, "source": "Детектор конфликтов и загрузка ресурсов на 4 ч"}


def _reduce(db, model, q, ctx):
    from app.services.planner import run_planner
    db.commit()  # не держать транзакцию на время расчёта
    res = run_planner(model, trigger="assistant")
    sb, sa = res["summary"]["before"], res["summary"]["after"]
    if not res["changes"]:
        return {"text": f"Текущий план уже не улучшается при заданных ограничениях ({res['status_label'].lower()}). "
                        f"Суммарная задержка отправлений по прогнозу — {sb['total_delay_min']:.0f} мин.",
                "facts": res["notes"], "source": "Планировщик CP-SAT"}
    top = res["changes"][:5]
    facts = [f"{c['kind_label']} поезда № {c['train']}: {c['from']['track']} {local_hm_iso(c['from']['start'])} → "
             f"{c['to']['track']} {local_hm_iso(c['to']['start'])}" for c in top if c["train"]]
    return {"text": f"Задержку можно уменьшить: суммарная задержка {sb['total_delay_min']:.0f} → {sa['total_delay_min']:.0f} мин, "
                    f"взвешенная по приоритетам {sb['weighted_delay']:.0f} → {sa['weighted_delay']:.0f}, изменение "
                    f"{len(res['changes'])} операций. Статус решения: {res['status_label'].lower()} ({res['solve_ms']:.0f} мс). "
                    f"Применить план можно в разделе «План и Гант» после проверки последствий.",
            "facts": facts + res["notes"], "actions": [{"type": "open_plan", "label": "Открыть сравнение планов"}],
            "source": "Планировщик CP-SAT (расчёт без применения)"}


def local_hm_iso(s):
    from datetime import datetime
    return local_hm(datetime.fromisoformat(s.replace("Z", "+00:00"))) if s else "—"


def _help(db, model, q, ctx):
    return {"text": "Я объясняю результаты серверных расчётов: проверки заявок, конфликтов и планировщика. "
                    "Задайте один из вопросов ниже или уточните объект (номер пути, заявку).",
            "facts": SAMPLES, "source": "Справка"}
