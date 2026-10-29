from pathlib import Path

from openai import OpenAI

import config


MODEL = "gpt-5-mini"
BASE_DIR = Path(__file__).resolve().parent
PRODUCTS_FILE = BASE_DIR / "products.json"


def profile_first_name(value):
    """Use only the first name-like token; never include profile advertising."""
    import re
    text = str(value or '').strip()
    text = re.split(r'[|/•·\n\r,;:@()]|https?://', text, maxsplit=1)[0]
    names = re.findall(r"[A-Za-zА-Яа-яЁё]+(?:[-’'][A-Za-zА-Яа-яЁё]+)*", text)
    for name in names:
        if name.casefold() not in {'двк','dvk','право','финанс','ооо','ип','менеджер'} and len(name)>1:
            return name
    raise ValueError('Не удалось определить имя отправителя. Укажите имя в профиле Telegram.')


def get_catalog():
    import os
    from product_catalog import ProductCatalog
    from context_adapters import ADAPTERS
    configured = os.getenv("BUSINESS_PRODUCTS_FILE", "").strip()
    path = Path(configured).expanduser() if configured else PRODUCTS_FILE
    if not path.is_absolute():
        path = BASE_DIR / path
    prompts_dir = path.parent / "prompts" if configured else Path(config.PROMPTS_DIR)
    if not prompts_dir.is_absolute():
        prompts_dir = BASE_DIR / prompts_dir
    return ProductCatalog(path, prompts_dir, adapters=ADAPTERS)


def load_products():
    return get_catalog().products


def normalize_product(product):
    return get_catalog().normalize(product)


def get_prompt_file(product):
    return get_catalog().prompt_path(product)


def load_prompt(product, inn):
    catalog = get_catalog()
    # Legacy interface renders without full input validation; full validation
    # is performed by the analysis entry point with all available fields.
    settings = catalog.settings(product)
    fields = {"inn": str(inn).strip()}
    return settings["prompt"], catalog.render(product, fields, validate=False)


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
    fields=None,
    sender_name=None,
):
    from context_adapters import apply_context_adapters
    catalog = get_catalog()
    normalized_product = catalog.normalize(product)
    settings = catalog.settings(normalized_product)
    inputs = dict(fields or {})
    inputs.update(inn=str(inn or "").strip(), full_name=str(full_name or "").strip(),
                  username=str(username or "").strip())
    prompt_file = settings["prompt"]
    prompt_text = catalog.render(normalized_product, inputs)
    if normalized_product in {'Право_ООО','Право_ИП'}:
        import re
        if not sender_name:
            raise ValueError('Не получено имя отправителя из Telegram-профиля.')
        name = profile_first_name(sender_name)
        prompt_text = re.sub(r'Сергей|\[ИМЯ ОТПРАВИТЕЛЯ\]', lambda _: name, prompt_text)
        prompt_text += ('\n\nПРОВЕРЕННОЕ ИМЯ ОТПРАВИТЕЛЯ: '+name+
                        '\nПредставляй отправителя только этим именем. Не добавляй фамилию, '
                        'отчество, должность или рекламу из профиля Telegram. '
                        'ФИО клиента не является именем отправителя.\n')
        from legal_research import run
        return run(dict(inn=inputs['inn'],full_name=inputs['full_name'],username=inputs['username'],
                        product=normalized_product,fields=inputs),name,settings,prompt_text,OpenAI)
    prompt_text = apply_context_adapters(settings["context_adapters"], prompt_text, inputs)

    if not config.OPENAI_API_KEY:
        raise RuntimeError(
            "Не задан OPENAI_API_KEY в настройках окружения."
        )

    client = OpenAI(api_key=config.OPENAI_API_KEY)

    # Полный выбранный prompt передаётся через input.
    # Дополнительный параметр instructions не используется.
    response = client.responses.create(
        model=settings["model"],
        tools=[{"type": tool} for tool in settings["tools"]],
        input=prompt_text,
    )

    try:
        from service_reports_runtime import record_usage
        record_usage(Path.cwd(),phase='initial_research',model=settings['model'],result=response,status='received',tools=settings['tools'],product=normalized_product)
    except Exception:
        pass

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
        "model": settings["model"],
        "product_version": settings["version"],
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
