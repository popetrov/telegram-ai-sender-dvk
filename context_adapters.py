"""Explicit integration adapters; packages select IDs, never Python import paths."""
import json
from dataclasses import dataclass
from typing import Callable


def get_arbitration_context(inn):
    """
    Получает судебные данные для юридического продукта.
    Вызывается только при включении адаптера в продуктовом пакете.

    Результат передаётся в prompt как данные. Ошибка останавливает
    подготовку в append_arbitration_context: старое дело не подставляется.
    """
    from arbitration_api import find_latest_defendant_case

    try:
        result = find_latest_defendant_case(inn)

        if not isinstance(result, dict):
            return {
                "status": "ERROR",
                "case": None,
                "reason": "API вернул результат неожиданного формата.",
            }

        status = result.get("status")

        if status == "CONFIRMED":
            case = result.get("case")

            if not isinstance(case, dict):
                return {
                    "status": "ERROR",
                    "case": None,
                    "reason": "В ответе API отсутствуют данные дела.",
                }

            return {
                "status": "CONFIRMED",
                "case": case,
                "reason": None,
            }

        if status == "NOT_FOUND":
            return {
                "status": "NOT_FOUND",
                "case": None,
                "reason": result.get("reason"),
            }

        return {
            "status": "ERROR",
            "case": None,
            "reason": result.get("reason") or "Ошибка судебного API.",
        }

    except Exception:
        # Не передаём технические подробности или секреты в prompt.
        return {
            "status": "ERROR",
            "case": None,
            "reason": "Не удалось получить данные судебного API.",
        }


def append_arbitration_context(prompt_text, inn):
    """
    Добавляет результат судебного поиска в prompt.
    Правила подготовки сообщения остаются в продуктовом prompt.
    """
    arbitration_result = get_arbitration_context(inn)
    if arbitration_result['status'] == 'ERROR':
        raise RuntimeError('ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА: самое новое судебное дело не подтверждено. '
                           + arbitration_result['reason'])

    context = {
        "ИНН_исследуемой_компании": str(inn).strip(),
        "статус_поиска": arbitration_result["status"],
        "данные_дела": arbitration_result["case"],
        "причина_отсутствия_данных": arbitration_result["reason"],
    }

    context_json = json.dumps(
        context,
        ensure_ascii=False,
        indent=2,
    )

    return (
        f"{prompt_text}\n\n"
        "ДАННЫЕ СУДЕБНОГО API В ФОРМАТЕ JSON:\n"
        f"{context_json}\n"
        "Выбрано самое новое дело ответчика по дате регистрации, независимо от статуса. "
        "Не заменяй его другим делом из веб-поиска, более старым или незавершённым. "
        "Если реквизиты выбранного дела не подтверждены, требуется ручная проверка.\n"
    )


def arbitration(prompt_text, fields):
    inn = str(fields.get("inn") or "").strip()
    if not inn:
        raise ValueError("Arbitration adapter requires inn")
    return append_arbitration_context(prompt_text, inn)


@dataclass(frozen=True)
class ContextAdapter:
    enrich: Callable[[str, dict], str]
    required_fields: frozenset[str] = frozenset()


ADAPTERS = {"arbitration": ContextAdapter(arbitration, frozenset({"inn"}))}


def apply_context_adapters(names, prompt_text, fields):
    for name in names:
        try:
            adapter = ADAPTERS[name]
        except KeyError as exc:
            raise ValueError(f"Unknown context adapter: {name}") from exc
        prompt_text = adapter.enrich(prompt_text, fields)
    return prompt_text
