"""Адаптер внешней языковой модели (необязательный).

По умолчанию выключен: MVP работает на шаблонных объяснениях. При включении модель получает
только вычисленные сервером факты и инструкцию не добавлять новых чисел и ограничений.
Ответ модели показывается как дополнительная формулировка; факты, действия и решения
остаются из серверного расчёта. Любая ошибка адаптера — тихий возврат к шаблону.
"""
from __future__ import annotations

import json
import logging

from app.config import get_settings

log = logging.getLogger("llm")

SYSTEM = ("Ты помогаешь диспетчеру железнодорожной станции (демонстрационная модель). Перескажи результат расчёта "
          "кратко и понятно по-русски. Используй ТОЛЬКО переданные факты. Не добавляй чисел, времени, ограничений и "
          "рекомендаций, которых нет в фактах. Если фактов недостаточно — так и скажи.")


def rephrase(question: str, result: dict) -> str | None:
    s = get_settings()
    if s.llm_provider != "anthropic" or not s.llm_api_key:
        return None
    try:
        import httpx
        payload = {"text": result.get("text"), "facts": result.get("facts", [])[:12],
                   "missing_data": result.get("missing_data", [])}
        r = httpx.post("https://api.anthropic.com/v1/messages", timeout=8.0,
                       headers={"x-api-key": s.llm_api_key, "anthropic-version": "2023-06-01",
                                "content-type": "application/json"},
                       json={"model": s.llm_model, "max_tokens": 400, "system": SYSTEM,
                             "messages": [{"role": "user", "content": f"Вопрос: {question}\nРезультат расчёта (JSON): "
                                                                      f"{json.dumps(payload, ensure_ascii=False)}"}]})
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text") or None
    except Exception as e:  # адаптер не должен ломать ответ
        log.warning("LLM-адаптер недоступен, используется шаблон: %s", e)
        return None
