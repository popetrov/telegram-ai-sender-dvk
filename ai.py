import json
import re
from pathlib import Path

from openai import OpenAI

import config


MODEL = "gpt-5-mini"
BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "products.json"

# Судебный API разрешён только для этих двух продуктов.
ARBITRATION_PRODUCTS = {"Право_ООО", "Право_ИП"}


def load_products():
    if not PRODUCTS_FILE.exists():
        raise FileNotFoundError(
            f"Не найден реестр продуктов: {PRODUCTS_FILE}"
        )

    with PRODUCTS_FILE.open("r", encoding="utf-8") as file:
        products = json.load(file)

    if not isinstance(products, dict) or not products:
        raise RuntimeError(
            "Реестр products.json пустой или имеет неверный формат."
        )

    return products


def normalize_product(product):
    value = str(product).strip().casefold()
    products = load_products()

    for product_name, settings in products.items():
        if value == product_name.casefold():
            return product_name

        aliases = settings.get("aliases", [])

        for alias in aliases:
            if value == str(alias).strip().casefold():
                return product_name

    raise ValueError(
        f"Неизвестный продукт: {product}. "
        f"Проверьте реестр {PRODUCTS_FILE.name}."
    )


def get_prompt_file(product):
    normalized_product = normalize_product(product)
    settings = load_products()[normalized_product]
    prompt_name = settings.get("prompt")

    if not isinstance(prompt_name, str) or not prompt_name.strip():
        raise RuntimeError(
            f"Для продукта {normalized_product} "
            "не указан файл prompt в products.json."
        )

    prompts_dir = Path(config.PROMPTS_DIR)

    if not prompts_dir.is_absolute():
        prompts_dir = BASE_DIR / prompts_dir

    prompts_dir = prompts_dir.resolve()
    prompt_path = (prompts_dir / prompt_name).resolve()

    # Запрещаем выход из каталога промптов через путь в реестре.
    if not prompt_path.is_relative_to(prompts_dir):
        raise RuntimeError(
            f"Недопустимый путь к prompt: {prompt_name}"
        )

    return prompt_path


def load_prompt(product, inn):
    prompt_path = get_prompt_file(product)

    if not prompt_path.exists():
        raise FileNotFoundError(
            f"Файл prompt не найден: {prompt_path}"
        )

    prompt_text = prompt_path.read_text(encoding="utf-8")

    if not prompt_text.strip():
        raise RuntimeError(
            f"Файл prompt пустой: {prompt_path}"
        )

    inn_value = str(inn).strip()

    if not inn_value:
        raise ValueError("ИНН компании не указан.")

    # Поддерживаем заполнители «Конкретный ИНН», {ИНН} и {{ИНН}}.
    placeholder_pattern = re.compile(
        r"Конкретный\s+ИНН|\{\{\s*ИНН\s*\}\}|\{\s*ИНН\s*\}",
        flags=re.IGNORECASE,
    )

    prompt_text, replacements = placeholder_pattern.subn(
        lambda _: inn_value,
        prompt_text,
    )

    # Если заполнителя нет, передаём ИНН в начале промпта.
    if replacements == 0:
        prompt_text = (
            f"ИНН КОМПАНИИ ДЛЯ ИССЛЕДОВАНИЯ: {inn_value}\n\n"
            f"{prompt_text}"
        )

    return prompt_path.name, prompt_text


def get_arbitration_context(inn):
    """
    Получает судебные данные для юридического продукта.
    Вызывается только для Право_ООО и Право_ИП.

    Ошибка API не останавливает подготовку предложения
    юридических услуг. Результат передаётся в prompt как данные.
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
    )


def extract_message_from_markers(raw_result):
    """
    Извлекает сообщение между маркерами:
    ГОТОВОЕ СООБЩЕНИЕ и КОНЕЦ СООБЩЕНИЯ.
    Возвращает исходный текст сообщения без лишних переводов
    строк по краям.
    """
    if "ТРЕБУЕТСЯ РУЧНАЯ ПРОВЕРКА" in raw_result.upper():
        raise RuntimeError(
            "Промпт запросил ручную проверку. "
            "Результат не является готовым сообщением."
        )

    start_marker = "ГОТОВОЕ СООБЩЕНИЕ"
    end_marker = "КОНЕЦ СООБЩЕНИЯ"

    start_position = raw_result.rfind(start_marker)

    if start_position == -1:
        raise RuntimeError(
            "Отсутствует маркер 'ГОТОВОЕ СООБЩЕНИЕ'. "
            "Результат нельзя считать готовым сообщением."
        )

    message_start = start_position + len(start_marker)

    end_position = raw_result.find(
        end_marker,
        message_start,
    )

    if end_position == -1:
        raise RuntimeError(
            "Отсутствует маркер 'КОНЕЦ СООБЩЕНИЯ'. "
            "Результат нельзя считать готовым сообщением."
        )

    message = raw_result[message_start:end_position].strip()

    if not message:
        raise RuntimeError(
            "Между маркерами отсутствует текст сообщения."
        )

    return message


def generate_company_analysis(
    inn,
    full_name,
    username,
    product,
):
    normalized_product = normalize_product(product)

    prompt_file, prompt_text = load_prompt(
        normalized_product,
        inn,
    )

    # Судебный API используется только для Право_ООО и Право_ИП.
    # Для БГ, кредита, закупок и остальных продуктов вызова нет.
    if normalized_product in ARBITRATION_PRODUCTS:
        prompt_text = append_arbitration_context(
            prompt_text=prompt_text,
            inn=inn,
        )

    if not config.OPENAI_API_KEY:
        raise RuntimeError(
            "Не задан OPENAI_API_KEY в настройках окружения."
        )

    client = OpenAI(api_key=config.OPENAI_API_KEY)

    # Полный выбранный prompt передаётся через input.
    # Дополнительный параметр instructions не используется.
    response = client.responses.create(
        model=MODEL,
        tools=[{"type": "web_search"}],
        input=prompt_text,
    )

    raw_result = response.output_text

    if not raw_result or not raw_result.strip():
        raise RuntimeError("Первый ИИ не вернул результат.")

    # Извлекаем готовое сообщение сразу после получения ответа.
    final_message = extract_message_from_markers(raw_result)

    return {
        "raw_result": raw_result,
        "final_message": final_message,
        "product": normalized_product,
        "prompt_file": prompt_file,
        "prompt_text": prompt_text,
        "model": MODEL,
    }


def process_client(
    inn,
    full_name,
    username,
    product,
):
    from database import save_processed_client

    # Сначала исследование и извлечение текста,
    # затем сохранение результата в базу.
    result = generate_company_analysis(
        inn=inn,
        full_name=full_name,
        username=username,
        product=product,
    )

    raw_result = result["raw_result"]
    final_message = result["final_message"]

    saved = save_processed_client(
        inn=inn,
        full_name=full_name,
        username=username,
        product=result["product"],
        prompt_file=result["prompt_file"],
        prompt_text=result["prompt_text"],
        model=result["model"],
        raw_result=raw_result,
        final_message=final_message,
    )

    return {
        "client_id": saved["client_id"],
        "research_run_id": saved["research_run_id"],
        "message_id": saved["message_id"],
        "product": result["product"],
        "prompt_file": result["prompt_file"],
        "raw_result": raw_result,
        "final_message": final_message,
    }


if __name__ == "__main__":
    print("AI-модуль готов.")
    print("Реальная отправка сообщений этим модулем не выполняется.")
