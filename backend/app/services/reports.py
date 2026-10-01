"""Мини-отчёт за период: метрики, конфликты, инциденты, принятые изменения, эффект
перепланирования, проблемы IoT. Форматы: CSV (UTF-8 с BOM, разделитель «;» — корректно
открывается в Excel с кириллицей) и PDF (шрифт DejaVu Sans с кириллицей)."""
from __future__ import annotations

import csv
import io
import os
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.timeutil import aware, local_full, local_hm, utcnow
from app.models import AuditEvent, DomainEvent, Incident, IndexSnapshot, PlanVersion, TelemetryReject
from app.services.conflicts import detect
from app.services.model import StationModel

REJECT_LABELS = {
    "BAD_JSON": "Некорректный JSON", "BAD_STRUCTURE": "Нарушена структура", "UNKNOWN_SCHEMA": "Неизвестная версия схемы",
    "UNKNOWN_DEVICE": "Неизвестное устройство", "DEVICE_DISABLED": "Устройство отключено", "WRONG_STATION": "Чужая станция",
    "EVENT_NOT_ALLOWED": "Тип события не разрешён", "OBJECT_MISMATCH": "Объект не связан с устройством",
    "BAD_UNIT": "Недопустимая единица", "OUT_OF_RANGE": "Значение вне диапазона", "BAD_PAYLOAD": "Некорректные данные",
    "TIME_IN_FUTURE": "Время из будущего", "TOO_OLD": "Слишком старое измерение", "TOPIC_MISMATCH": "Топик не соответствует",
    "QUEUE_OVERFLOW": "Переполнение очереди", "BAD_TIME": "Некорректное время",
}


def collect(db: Session, minutes: int) -> dict:
    now = utcnow()
    since = now - timedelta(minutes=minutes)
    model = StationModel(db)
    det = detect(model)
    idx = list(db.execute(select(IndexSnapshot).where(IndexSnapshot.real_time >= since).order_by(IndexSnapshot.id)).scalars())
    vals = [i.value for i in idx if i.value is not None]
    incidents = list(db.execute(select(Incident).where(Incident.created_at >= since).order_by(Incident.created_at)).scalars())
    changes = list(db.execute(select(AuditEvent).where(AuditEvent.ts >= since, AuditEvent.action.in_(
        ["plan.apply", "request.confirm", "request.cancel", "request.reject", "request.reschedule", "operation.reschedule",
         "incident.create", "incident.resolve", "observation.override", "config.index"])).order_by(AuditEvent.id)).scalars())
    plans = list(db.execute(select(PlanVersion).where(PlanVersion.created_at >= since, PlanVersion.status == "applied")).scalars())
    rejects = db.execute(select(TelemetryReject.reason_code, func.count()).where(TelemetryReject.received_at >= since)
                         .group_by(TelemetryReject.reason_code)).all()
    dq = list(db.execute(select(DomainEvent).where(DomainEvent.ts >= since, DomainEvent.type.in_(
        ["data.quality", "device.rebooted"])).order_by(DomainEvent.id)).scalars())
    return {"station": model.station.name, "since": since, "until": now, "model_now": model.now, "minutes": minutes,
            "index_now": vals[-1] if vals else None, "index_min": min(vals) if vals else None,
            "index_max": max(vals) if vals else None, "index_points": len(vals),
            "conflicts": det["conflicts"], "delays": det["delays"], "incidents": incidents, "changes": changes,
            "plans": plans, "rejects": rejects, "dq": dq}


def to_csv(d: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([f"Мини-отчёт: {d['station']} — демонстрационные данные"])
    w.writerow(["Период (реальное время)", f"{local_full(d['since'])} — {local_full(d['until'])}"])
    w.writerow(["Модельное время на момент выгрузки", local_full(d["model_now"])])
    w.writerow([])
    w.writerow(["Метрики"])
    w.writerow(["Показатель", "Значение"])
    w.writerow(["Индекс эффективности (текущий)", _n(d["index_now"])])
    w.writerow(["Индекс: минимум за период", _n(d["index_min"])])
    w.writerow(["Индекс: максимум за период", _n(d["index_max"])])
    w.writerow(["Активных конфликтов", len(d["conflicts"])])
    w.writerow(["Поездов с задержкой ≥ 5 мин", len(d["delays"])])
    w.writerow([])
    w.writerow(["Конфликты (текущие)"])
    w.writerow(["Серьёзность", "Тип", "Описание"])
    for c in d["conflicts"]:
        w.writerow([c["severity_label"], c["title"], c["explanation"]])
    w.writerow([])
    w.writerow(["Инциденты за период"])
    w.writerow(["Начало (модельное)", "Инцидент", "Статус"])
    for i in d["incidents"]:
        w.writerow([local_full(i.start_at), i.title, "Устранён" if i.status == "resolved" else "Действует"])
    w.writerow([])
    w.writerow(["Принятые изменения (аудит)"])
    w.writerow(["Время", "Пользователь", "Действие", "Описание"])
    for a in d["changes"]:
        w.writerow([local_full(a.ts), a.username, a.action, a.summary])
    w.writerow([])
    w.writerow(["Эффект перепланирования"])
    w.writerow(["План", "Решатель", "Статус", "Задержка до, мин", "Задержка после, мин", "Индекс до", "Индекс после"])
    for p in d["plans"]:
        s = p.summary or {}
        w.writerow([p.id, p.solver, s.get("status_label"), s.get("before", {}).get("total_delay_min"),
                    s.get("after", {}).get("total_delay_min"), (s.get("index_before") or {}).get("value"),
                    (s.get("index_after") or {}).get("value")])
    w.writerow([])
    w.writerow(["Проблемы IoT"])
    w.writerow(["Причина отклонения", "Количество"])
    for code, n in d["rejects"]:
        w.writerow([REJECT_LABELS.get(code, code), n])
    for e in d["dq"]:
        w.writerow([local_full(e.ts), e.message])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


def _n(v):
    return "нет данных" if v is None else f"{v:.1f}"


def _font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/arial.ttf"):
        if os.path.exists(p):
            try:
                pdfmetrics.registerFont(TTFont("Cyr", p))
                return "Cyr"
            except Exception:
                continue
    return "Helvetica"


def to_pdf(d: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    f = _font()
    h = ParagraphStyle("h", fontName=f, fontSize=15, leading=19, spaceAfter=6)
    h2 = ParagraphStyle("h2", fontName=f, fontSize=11.5, leading=15, spaceBefore=8, spaceAfter=4)
    p = ParagraphStyle("p", fontName=f, fontSize=9, leading=12)
    small = ParagraphStyle("s", fontName=f, fontSize=8, leading=10, textColor=colors.HexColor("#555555"))
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=14 * mm,
                            title="Мини-отчёт — Цифровая станция")
    st = TableStyle([("FONTNAME", (0, 0), (-1, -1), f), ("FONTSIZE", (0, 0), (-1, -1), 8),
                     ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8edf3")),
                     ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#b8c2cc")), ("VALIGN", (0, 0), (-1, -1), "TOP")])

    def table(rows, widths):
        return Table([[Paragraph(str(c), p) for c in r] for r in rows], colWidths=[w * mm for w in widths], style=st, repeatRows=1)

    els = [Paragraph(f"Мини-отчёт: {d['station']}", h),
           Paragraph(f"Период (реальное время): {local_full(d['since'])} — {local_full(d['until'])}. Модельное время: "
                     f"{local_full(d['model_now'])}. Демонстрационные синтетические данные; не является документом "
                     f"системы обеспечения безопасности движения.", small), Spacer(1, 4)]
    els += [Paragraph("Метрики", h2), table([["Показатель", "Значение"],
                                             ["Индекс эффективности (текущий)", _n(d["index_now"])],
                                             ["Индекс: минимум / максимум за период", f"{_n(d['index_min'])} / {_n(d['index_max'])}"],
                                             ["Активных конфликтов", len(d["conflicts"])],
                                             ["Поездов с задержкой ≥ 5 мин", len(d["delays"])]], [110, 70])]
    els += [Paragraph("Конфликты", h2), table([["Серьёзность", "Описание"]] +
                                              [[c["severity_label"], c["explanation"]] for c in d["conflicts"][:25]] or
                                              [["—", "Конфликтов нет"]], [28, 152])]
    els += [Paragraph("Инциденты", h2), table([["Начало", "Инцидент", "Статус"]] +
                                              [[local_hm(i.start_at), i.title, "Устранён" if i.status == "resolved" else "Действует"]
                                               for i in d["incidents"]], [22, 128, 30])]
    els += [Paragraph("Принятые изменения", h2), table([["Время", "Пользователь", "Описание"]] +
                                                       [[local_hm(a.ts), a.username, a.summary] for a in d["changes"][:40]], [18, 30, 132])]
    els += [Paragraph("Эффект перепланирования", h2), table(
        [["План", "Статус решения", "Задержка, мин (до → после)", "Индекс (до → после)"]] +
        [[str(pl.id), (pl.summary or {}).get("status_label", ""),
          f"{(pl.summary or {}).get('before', {}).get('total_delay_min')} → {(pl.summary or {}).get('after', {}).get('total_delay_min')}",
          f"{((pl.summary or {}).get('index_before') or {}).get('value')} → {((pl.summary or {}).get('index_after') or {}).get('value')}"]
         for pl in d["plans"]], [16, 74, 45, 45])]
    els += [Paragraph("Проблемы IoT", h2), table([["Причина отклонения сообщений", "Количество"]] +
                                                 [[REJECT_LABELS.get(c, c), n] for c, n in d["rejects"]], [130, 50])]
    for e in d["dq"][:20]:
        els.append(Paragraph(f"{local_hm(e.ts)} — {e.message}", p))
    doc.build(els)
    return buf.getvalue()
