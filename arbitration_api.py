import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation


API_URL = "https://parser-api.com/parser/arbitr_api/search"
DETAILS_URL = "https://parser-api.com/parser/arbitr_api/details_by_id"

REQUEST_TIMEOUT = 60


def _normalize_inn(inn: str) -> str:
    """Оставляет в ИНН только цифры."""
    return "".join(char for char in str(inn or "") if char.isdigit())


def _normalize_text(value) -> str:
    """Нормализует текст для сравнения названий и адресов."""
    return " ".join(str(value or "").casefold().split())


def _api_get(url: str, params: dict, api_key: str) -> dict:
    """Выполняет GET-запрос к parser-api.com."""
    if not api_key:
        raise RuntimeError("Не задан ключ PARSER_API_KEY.")

    request_params = dict(params)
    request_params["key"] = api_key

    full_url = url + "?" + urllib.parse.urlencode(request_params)

    request = urllib.request.Request(
        full_url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(
        request,
        timeout=REQUEST_TIMEOUT
    ) as response:
        body = response.read().decode("utf-8")

    data = json.loads(body)

    if not isinstance(data, dict):
        raise RuntimeError("API вернул ответ неожиданного формата.")

    return data


def _get_party_names(parties) -> list[str]:
    """Возвращает названия участников дела."""
    if not isinstance(parties, list):
        return []

    result = []

    for party in parties:
        if not isinstance(party, dict):
            continue

        name = str(party.get("Name") or "").strip()

        if name:
            result.append(name)

    return result


def _party_has_inn(parties, target_inn: str) -> bool:
    """Проверяет ИНН среди участников поисковой выдачи."""
    if not isinstance(parties, list):
        return False

    for party in parties:
        if not isinstance(party, dict):
            continue

        party_inn = _normalize_inn(party.get("Inn", ""))

        if party_inn and party_inn == target_inn:
            return True

    return False


def _find_party_by_identity(search_parties, detailed_parties, target_inn):
    """
    Сопоставляет участника из поиска с участником подробной карточки.

    ИНН подтверждается по поисковой выдаче. В подробной карточке
    участник может не содержать ИНН, поэтому используются название
    и адрес из обоих ответов API.
    """
    if not isinstance(search_parties, list):
        return None

    if not isinstance(detailed_parties, list):
        return None

    for search_party in search_parties:
        if not isinstance(search_party, dict):
            continue

        search_inn = _normalize_inn(search_party.get("Inn", ""))

        if search_inn != target_inn:
            continue

        search_name = _normalize_text(search_party.get("Name"))
        search_address = _normalize_text(search_party.get("Address"))

        for detailed_party in detailed_parties:
            if not isinstance(detailed_party, dict):
                continue

            detailed_name = _normalize_text(detailed_party.get("Name"))
            detailed_address = _normalize_text(detailed_party.get("Address"))

            if not search_name or not detailed_name:
                continue

            if search_name != detailed_name:
                continue

            # Если адрес есть в обеих карточках, он также должен совпасть.
            if search_address and detailed_address:
                if search_address != detailed_address:
                    continue

            return detailed_party

    return None


def _parse_date(value) -> datetime:
    """Преобразует дату в значение для сортировки."""
    if not value:
        return datetime.min

    text = str(value).strip()

    formats = (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%d.%m.%Y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue

    try:
        return datetime.fromisoformat(
            text.replace("Z", "+00:00")
        ).replace(tzinfo=None)
    except ValueError:
        return datetime.min


def _get_claim_sum_from_details(case: dict) -> str | None:
    """
    Получает сумму требований из событий дела.

    Приоритет: последнее по дате событие с положительной суммой.
    """
    claims = []

    for instance in case.get("CaseInstances") or []:
        if not isinstance(instance, dict):
            continue

        for event in instance.get("InstanceEvents") or []:
            if not isinstance(event, dict):
                continue

            amount = event.get("ClaimSum")

            if amount is None or amount == "":
                continue

            try:
                amount_decimal = Decimal(str(amount))
            except (InvalidOperation, ValueError):
                continue

            if amount_decimal <= 0:
                continue

            event_date = _parse_date(
                event.get("Date") or event.get("PublishDate")
            )

            claims.append((event_date, amount_decimal))

    if not claims:
        return None

    claims.sort(key=lambda item: item[0], reverse=True)

    return format(claims[0][1], "f")


def _get_case_details(case_id: str, api_key: str) -> dict | None:
    """Загружает подробную карточку дела."""
    data = _api_get(
        DETAILS_URL,
        {"CaseId": case_id},
        api_key
    )

    if data.get("Success") != 1:
        return None

    cases = data.get("Cases") or []

    if not cases or not isinstance(cases[0], dict):
        return None

    case = cases[0]

    returned_id = str(case.get("CaseId") or "").strip()

    if returned_id and returned_id != str(case_id).strip():
        return None

    return case


def find_latest_defendant_case(inn: str) -> dict:
    """
    Ищет самое новое дело, в котором ИНН подтверждён как ИНН ответчика.

    Поддерживает:
    - 10 цифр — ИНН юридического лица;
    - 12 цифр — ИНН индивидуального предпринимателя.

    CONFIRMED означает, что ИНН ответчика найден в выдаче,
    участник сопоставлен с подробной карточкой и получен номер дела.

    Важно: CONFIRMED не гарантирует, что найдена сумма иска.
    Если claim_sum равен None, использовать дело в готовом сообщении
    нельзя — необходима ручная проверка.

    NOT_FOUND — подходящее дело не найдено либо не удалось
    сопоставить участника с подробной карточкой.

    ERROR — поиск или запрос к API завершился ошибкой.

    Функция не отправляет сообщения клиентам.
    """
    from config import PARSER_API_KEY

    target_inn = _normalize_inn(inn)

    if len(target_inn) not in (10, 12):
        return {
            "status": "ERROR",
            "case": None,
            "reason": (
                "ИНН должен содержать 10 цифр для юридического лица "
                "или 12 цифр для ИП."
            )
        }

    if not PARSER_API_KEY:
        return {
            "status": "ERROR",
            "case": None,
            "reason": "Не задан ключ PARSER_API_KEY."
        }

    try:
        search_data = _api_get(
            API_URL,
            {
                "Inn": target_inn,
                "InnType": "Respondent",
                "page": 1,
            },
            PARSER_API_KEY
        )

        if search_data.get("Success") != 1:
            return {
                "status": "ERROR",
                "case": None,
                "reason": "API не подтвердил успешный поиск."
            }

        cases = search_data.get("Cases") or []

        if not isinstance(cases, list):
            return {
                "status": "ERROR",
                "case": None,
                "reason": "API вернул список дел в неожиданном формате."
            }

        defendant_cases = []

        for case in cases:
            if not isinstance(case, dict):
                continue

            respondents = case.get("Respondents") or []

            if _party_has_inn(respondents, target_inn):
                defendant_cases.append(case)

        if not defendant_cases:
            return {
                "status": "NOT_FOUND",
                "case": None,
                "reason": (
                    "В результатах API не найдено дело "
                    "с подтверждённым ИНН ответчика."
                )
            }

        defendant_cases.sort(
            key=lambda item: _parse_date(item.get("StartDate")),
            reverse=True
        )

        for search_case in defendant_cases:
            case_id = search_case.get("CaseId")

            if not case_id:
                continue

            details = _get_case_details(
                str(case_id),
                PARSER_API_KEY
            )

            if not details:
                continue

            search_respondents = search_case.get("Respondents") or []
            detailed_respondents = details.get("Respondents") or []

            matched_respondent = _find_party_by_identity(
                search_respondents,
                detailed_respondents,
                target_inn
            )

            if not matched_respondent:
                continue

            case_number = (
                details.get("CaseNumber")
                or search_case.get("CaseNumber")
            )

            if not case_number:
                continue

            case_result = {
                "case_id": str(case_id),
                "case_number": str(case_number),
                "start_date": (
                    details.get("StartDate")
                    or search_case.get("StartDate")
                ),
                "defendant_inn": target_inn,
                "defendant_name": matched_respondent.get("Name"),
                "defendant_address": matched_respondent.get("Address"),
                "plaintiffs": (
                    _get_party_names(details.get("Plaintiffs"))
                    or _get_party_names(search_case.get("Plaintiffs"))
                ),
                "respondents": _get_party_names(detailed_respondents),
                "claim_sum": _get_claim_sum_from_details(details),
                "case_type": (
                    details.get("CaseType")
                    or search_case.get("CaseType")
                ),
                "court": (
                    details.get("Court")
                    or search_case.get("Court")
                ),
                "state": (
                    details.get("State")
                    or search_case.get("State")
                ),
                "finished": details.get("Finished"),
            }

            return {
                "status": "CONFIRMED",
                "case": case_result,
                "reason": None
            }

        return {
            "status": "NOT_FOUND",
            "case": None,
            "reason": (
                "Дело найдено в поисковой выдаче, но не удалось "
                "сопоставить ответчика с подробной карточкой."
            )
        }

    except urllib.error.HTTPError as error:
        return {
            "status": "ERROR",
            "case": None,
            "reason": f"HTTP-ошибка API: {error.code}"
        }

    except urllib.error.URLError:
        return {
            "status": "ERROR",
            "case": None,
            "reason": "Не удалось подключиться к API."
        }

    except (TimeoutError, json.JSONDecodeError):
        return {
            "status": "ERROR",
            "case": None,
            "reason": "Тайм-аут или некорректный ответ API."
        }

    except Exception as error:
        return {
            "status": "ERROR",
            "case": None,
            "reason": (
                "Непредвиденная ошибка при поиске "
                "или проверке дела: "
                f"{type(error).__name__}: {error}"
            )
        }