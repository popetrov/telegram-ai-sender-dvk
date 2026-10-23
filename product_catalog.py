"""Declarative business packages; no Telegram, database or network dependencies."""
import json
import re
from collections.abc import Mapping
from pathlib import Path, PureWindowsPath

DEFAULT_MODEL = "gpt-5-mini"
DEFAULT_COLUMNS = {"inn": "ИНН", "full_name": "ФИО"}
DEFAULT_REQUIRED = ["inn", "full_name"]


class ProductCatalog:
    def __init__(self, path, prompts_dir, adapters=()):
        self.path = Path(path)
        self.prompts_dir = Path(prompts_dir).resolve()
        self.adapters = set(adapters)
        adapter_fields = ({name: adapter.required_fields for name, adapter in adapters.items()}
                          if isinstance(adapters, Mapping) else {})
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Product registry must be an object")
        # Existing flat products.json remains supported.
        if "schema_version" in data:
            if type(data["schema_version"]) is not int or data["schema_version"] != 1:
                raise ValueError("Unsupported business package schema_version")
            data = data.get("products")
        if not isinstance(data, dict) or not data:
            raise ValueError("Product registry must be a nonempty object")
        self.products = {}
        self.aliases = {}
        for name, definition in data.items():
            if not isinstance(name, str) or not name.strip() or not isinstance(definition, dict):
                raise ValueError("Invalid product definition")
            unknown = set(definition) - {"prompt", "aliases", "version", "model", "tools", "context_adapters", "columns", "required_fields"}
            if unknown:
                raise ValueError(f"Unknown settings for {name}: {sorted(unknown)}")
            settings = dict(definition)
            prompt = settings.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError(f"Missing prompt for {name}")
            if Path(prompt).is_absolute() or PureWindowsPath(prompt).drive:
                raise ValueError(f"Prompt path must be relative: {name}")
            prompt_path = (self.prompts_dir / prompt).resolve()
            if not prompt_path.is_relative_to(self.prompts_dir) or not prompt_path.is_file():
                raise ValueError(f"Prompt outside package or missing: {name}")
            if not prompt_path.read_text(encoding="utf-8").strip():
                raise ValueError(f"Empty prompt: {name}")
            for field, default in (("version", "1"), ("model", DEFAULT_MODEL)):
                settings.setdefault(field, default)
                if not isinstance(settings[field], str) or not settings[field].strip():
                    raise ValueError(f"Invalid {field}: {name}")
            for field, default in (("aliases", []), ("tools", ["web_search"]), ("context_adapters", [])):
                settings.setdefault(field, default)
                value = settings[field]
                if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
                    raise ValueError(f"Invalid {field}: {name}")
                if len(value) != len(set(value)):
                    raise ValueError(f"Repeated {field}: {name}")
            if set(settings["tools"]) - {"web_search"}:
                raise ValueError(f"Unsupported AI tool: {name}")
            if set(settings["context_adapters"]) - self.adapters:
                raise ValueError(f"Unknown context adapter: {name}")
            settings.setdefault("columns", dict(DEFAULT_COLUMNS))
            columns = settings["columns"]
            if (not isinstance(columns, dict) or any(
                    not isinstance(k, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", k)
                    or k in {"username", "product"} or not isinstance(v, str) or not v.strip()
                    for k, v in columns.items())):
                raise ValueError(f"Invalid input columns: {name}")
            if len(set(columns.values())) != len(columns):
                raise ValueError(f"Ambiguous input columns: {name}")
            settings.setdefault("required_fields", list(DEFAULT_REQUIRED))
            required = settings["required_fields"]
            if (not isinstance(required, list) or any(not isinstance(x, str) or x not in columns for x in required)
                    or len(set(required)) != len(required)):
                raise ValueError(f"Invalid required_fields: {name}")
            for adapter in settings["context_adapters"]:
                if set(adapter_fields.get(adapter, ())) - set(required):
                    raise ValueError(f"Adapter required fields must be mandatory: {adapter} / {name}")
            self.products[name] = settings
            for alias in [name] + settings["aliases"]:
                key = alias.strip().casefold()
                if key in self.aliases and self.aliases[key] != name:
                    raise ValueError(f"Ambiguous product alias: {alias}")
                self.aliases[key] = name

    def normalize(self, product):
        try:
            return self.aliases[str(product).strip().casefold()]
        except KeyError as exc:
            raise ValueError(f"Неизвестный продукт: {product}") from exc

    def settings(self, product):
        return self.products[self.normalize(product)]

    def prompt_path(self, product):
        return (self.prompts_dir / self.settings(product)["prompt"]).resolve()

    def validate_fields(self, product, fields):
        settings = self.settings(product)
        missing = [settings["columns"][key] for key in settings["required_fields"]
                   if not str(fields.get(key) or "").strip()]
        if missing:
            raise ValueError("Не заполнены поля продукта: " + ", ".join(missing))

    def render(self, product, fields, *, validate=True):
        if validate:
            self.validate_fields(product, fields)
        text = self.prompt_path(product).read_text(encoding="utf-8")
        inn = str(fields.get("inn") or "").strip()
        # Retain all three legacy INN placeholders.
        legacy = re.compile(r"Конкретный\s+ИНН|\{\{\s*ИНН\s*\}\}|\{\s*ИНН\s*\}", re.I)
        if legacy.search(text) and not inn:
            raise ValueError("Prompt requires ИНН")
        text, count = legacy.subn(lambda _: inn, text)
        if inn and not count:
            text = f"ИНН КОМПАНИИ ДЛЯ ИССЛЕДОВАНИЯ: {inn}\n\n{text}"
        # Only a deliberately small placeholder grammar is interpreted;
        # ordinary JSON and braces in prompts remain untouched.
        def replace(match):
            key = match.group(1)
            if key not in fields or not str(fields[key]).strip():
                raise ValueError(f"Missing prompt field: {key}")
            return str(fields[key])
        return re.sub(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}", replace, text)
